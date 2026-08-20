from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler

from fastapi import HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from reachy_mini import ReachyMini, ReachyMiniApp

from .audio.capture import AudioPipelineStalled
from .audio_setup import apply_wireless_conversation_audio_config
from .config import AppConfig, language_choices, language_option
from .motion import MotionManager
from .observability.events import EventRecorder, RedactingFormatter
from .realtime import RealtimeRobotSession
from .runtime_status import RuntimeStatus
from .session.recovery import SessionOutcome
from .session.supervisor import ESCALATION_PAUSE_SECONDS, RestartBudget
from .settings import (
    events_path,
    load_instance_env,
    log_path,
    prepare_config_dir,
    remove_api_key,
    save_api_key,
    save_language,
    usage_path,
)
from .usage import UsageTracker

logger = logging.getLogger(__name__)

try:
    APP_VERSION = version("reachy_openai_realtime")
except PackageNotFoundError:
    APP_VERSION = "development"


def attach_file_logging() -> RotatingFileHandler:
    """Attach the rotating application.log handler for this app process.

    A factory-fresh install has no config directory yet; create it here so the
    handler's eager open cannot kill the app before the first log line.
    """
    prepare_config_dir()
    handler = RotatingFileHandler(log_path(), maxBytes=2_000_000, backupCount=2)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler


class ApiKeyUpdate(BaseModel):
    api_key: str


class CameraUpdate(BaseModel):
    enabled: bool


class LanguageUpdate(BaseModel):
    language: str


class ReachyOpenaiRealtime(ReachyMiniApp):
    custom_app_url: str | None = "http://0.0.0.0:8042"
    request_media_backend: str | None = "default"

    def __init__(self, running_on_wireless: bool = False) -> None:
        super().__init__(running_on_wireless=running_on_wireless)
        self._instance_dir = self._get_instance_path().parent
        load_instance_env(self._instance_dir)
        self._session_started = False
        self._camera_enabled = False
        self._camera_available = False
        self._reachy_mini: ReachyMini | None = None
        self._camera_lock = threading.Lock()
        self._language_lock = threading.Lock()
        initial_config = AppConfig.from_env()
        self._language = initial_config.language
        self.runtime_status = RuntimeStatus(UsageTracker(usage_path()))

        assert self.settings_app is not None

        @self.settings_app.get("/api/config")
        def get_config() -> dict[str, object]:
            return {
                "configured": bool(os.getenv("OPENAI_API_KEY")),
                "model": os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
                "voice": os.getenv("OPENAI_REALTIME_VOICE", "marin"),
                "app_name": "Reachy Mini OpenAI Realtime",
                "app_version": APP_VERSION,
                "language": self._current_language(),
                "languages": language_choices(),
                "session_started": self._session_started,
                "camera_available": self._camera_available,
                "camera_enabled": self._camera_enabled,
                "camera_sent_to_openai": self._camera_enabled,
                "camera_send_mode": "speech_start_snapshot",
            }

        @self.settings_app.get("/api/status")
        def get_status() -> dict[str, object]:
            return self.runtime_status.snapshot()

        @self.settings_app.get("/api/health")
        def api_health() -> JSONResponse:
            return JSONResponse(self.status.health())

        @self.settings_app.get("/api/diagnostics")
        def get_diagnostics() -> dict[str, object]:
            return {
                "app_version": APP_VERSION,
                "model": os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1"),
                "voice": os.getenv("OPENAI_REALTIME_VOICE", "marin"),
                "language": self._current_language(),
                "api_key_configured": bool(os.getenv("OPENAI_API_KEY")),
                "session_started": self._session_started,
                "camera_available": self._camera_available,
                "camera_enabled": self._camera_enabled,
                "camera_sent_to_openai": self._camera_enabled,
                "camera_send_mode": "speech_start_snapshot",
                "runtime": self.runtime_status.snapshot(),
            }

        @self.settings_app.post("/api/config/language")
        def update_language(update: LanguageUpdate) -> dict[str, object]:
            try:
                selected = language_option(update.language)
                save_language(selected.code)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="対応していない言語です") from exc
            except OSError as exc:
                logger.exception("Failed to persist target language")
                raise HTTPException(
                    status_code=500,
                    detail="言語設定を保存できませんでした",
                ) from exc
            with self._language_lock:
                self._language = selected.code
            self.runtime_status.record_event("settings.changed", setting="language", value=selected.code)
            self.runtime_status.add_event(
                f"会話言語を{selected.label}に変更しました（次の応答から反映）",
                key="language_changed",
                params={"language": selected.label},
            )
            return {
                "language": selected.code,
                "label": selected.label,
                "restart_required": False,
            }

        @self.settings_app.post("/api/config/camera")
        def update_camera(update: CameraUpdate) -> dict[str, object]:
            with self._camera_lock:
                if update.enabled and not self._camera_available:
                    raise HTTPException(
                        status_code=409,
                        detail="カメラを利用できません",
                    )
                self._camera_enabled = update.enabled
            self.runtime_status.record_event("settings.changed", setting="camera", enabled=update.enabled)
            if update.enabled:
                self.runtime_status.add_event(
                    "AIカメラをONにしました（発話開始時に画像をOpenAIへ送信）",
                    key="event_camera_on",
                )
            else:
                self.runtime_status.add_event(
                    "AIカメラをOFFにしました",
                    key="event_camera_off",
                )
            return {
                "camera_available": self._camera_available,
                "camera_enabled": self._camera_enabled,
                "camera_sent_to_openai": self._camera_enabled,
                "camera_send_mode": "speech_start_snapshot",
            }

        @self.settings_app.get("/api/camera/snapshot")
        def get_camera_snapshot() -> Response:
            with self._camera_lock:
                enabled = self._camera_enabled
                available = self._camera_available
                robot = self._reachy_mini
            if not enabled:
                raise HTTPException(status_code=403, detail="AIカメラはOFFです")
            if robot is None or not available:
                raise HTTPException(status_code=503, detail="カメラを利用できません")
            try:
                jpeg = self._capture_camera_frame()
            except Exception as exc:
                logger.warning("Failed to read camera frame", exc_info=True)
                raise HTTPException(
                    status_code=503,
                    detail="カメラ画像を取得できません",
                ) from exc
            if jpeg is None:
                raise HTTPException(status_code=503, detail="カメラ画像を取得できません")
            return Response(
                content=jpeg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        @self.settings_app.post("/api/config/api-key")
        def update_api_key(update: ApiKeyUpdate) -> dict[str, object]:
            try:
                save_api_key(update.api_key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except OSError as exc:
                logger.exception("Failed to persist OpenAI API key")
                raise HTTPException(
                    status_code=500,
                    detail="ロボットの設定領域へ書き込めませんでした",
                ) from exc
            self.runtime_status.record_event("settings.changed", setting="api_key", configured=True)
            return {
                "configured": True,
                "restart_required": self._session_started,
            }

        @self.settings_app.delete("/api/config/api-key")
        def delete_api_key() -> dict[str, object]:
            try:
                removed = remove_api_key(self._instance_dir)
            except OSError as exc:
                logger.exception("Failed to remove OpenAI API key")
                raise HTTPException(
                    status_code=500,
                    detail="保存済みキーを削除できませんでした",
                ) from exc
            self.runtime_status.record_event("settings.changed", setting="api_key", configured=False)
            return {
                "configured": False,
                "removed": removed,
                "restart_required": self._session_started,
            }

    @property
    def status(self) -> RuntimeStatus:
        return self.runtime_status

    def _current_language(self) -> str:
        with self._language_lock:
            return self._language

    def _is_camera_enabled(self) -> bool:
        with self._camera_lock:
            return self._camera_enabled and self._camera_available and self._reachy_mini is not None

    def _capture_camera_frame(self) -> bytes | None:
        with self._camera_lock:
            if not self._camera_enabled or not self._camera_available or self._reachy_mini is None:
                return None
            return self._reachy_mini.media.get_frame_jpeg()

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        recorder = EventRecorder(events_path())
        self.runtime_status.attach_recorder(recorder)
        file_handler = attach_file_logging()
        recorder.record("app.start")
        self._recorder = recorder

        with self._camera_lock:
            self._reachy_mini = reachy_mini
            self._camera_available = reachy_mini.media.camera is not None
            self._camera_enabled = False
        if self._camera_available:
            self.runtime_status.add_event(
                "カメラを検出しました（AIカメラは初期OFF）",
                key="event_camera_detected",
            )
        else:
            self.runtime_status.add_event(
                "カメラは利用できません",
                key="event_camera_unavailable",
            )
        # Camera health: disabled→True (not in use is not unhealthy); enabled+available→True;
        # enabled+unavailable→False. At startup _camera_enabled is always False, so this
        # evaluates to True regardless of availability. expires=False: static, set once.
        self.runtime_status.set_component_health(
            "camera",
            self._camera_available if self._camera_enabled else True,
            expires=False,
        )
        if not os.getenv("OPENAI_API_KEY"):
            logger.warning("Open the app settings page and enter OPENAI_API_KEY")
            self.runtime_status.set_phase(
                "waiting_key",
                "OpenAI APIキーを設定してください",
                connected=False,
                event=True,
                detail_key="detail_waiting_key",
            )
        while not os.getenv("OPENAI_API_KEY") and not stop_event.is_set():
            time.sleep(0.25)
        if stop_event.is_set():
            self.runtime_status.set_phase(
                "stopped",
                "停止しました",
                connected=False,
                detail_key="detail_stopped",
            )
            return

        motion = MotionManager(reachy_mini)

        self._session_started = True
        self.runtime_status.set_phase(
            "starting_audio",
            "マイクとスピーカーを準備しています",
            event=True,
            detail_key="detail_starting_audio",
        )
        motion.attach_recorder(self.runtime_status.record_event)
        motion.set_heartbeat(lambda: self.runtime_status.set_component_health("motion", True))
        motion.start()
        reachy_mini.media.start_recording()
        reachy_mini.media.start_playing()
        audio_started_at = time.monotonic()
        self.runtime_status.set_phase(
            "tuning_audio",
            "Wirelessマイクを調整しています",
            event=True,
            detail_key="detail_tuning_audio",
        )
        if apply_wireless_conversation_audio_config(reachy_mini):
            self.runtime_status.add_event(
                "Reachy会話用のマイク設定を適用しました",
                key="event_mic_config_applied",
            )
        else:
            self.runtime_status.add_event(
                "現在のマイク設定で開始します",
                key="event_mic_config_current",
            )
        warmup_remaining = 1.0 - (time.monotonic() - audio_started_at)
        if warmup_remaining > 0:
            time.sleep(warmup_remaining)
        budget = RestartBudget()
        try:
            while not stop_event.is_set():
                config = AppConfig.from_env()
                session = RealtimeRobotSession(
                    reachy_mini,
                    motion,
                    config,
                    self.runtime_status,
                    language_provider=self._current_language,
                    camera_enabled=self._is_camera_enabled,
                    capture_camera_jpeg=self._capture_camera_frame,
                )
                try:
                    outcome = asyncio.run(session.run(stop_event))
                except AudioPipelineStalled:
                    self.runtime_status.add_event("audio pipeline stalled; restarting app session", level="warning")
                    escalate = budget.record_restart(time.monotonic())
                    if escalate:
                        self.runtime_status.record_event(
                            "supervisor.escalated", restarts=budget.limit, window_seconds=budget.window_seconds
                        )
                    try:
                        reachy_mini.media.stop_playing()
                        reachy_mini.media.stop_recording()
                        reachy_mini.media.start_recording()
                        reachy_mini.media.start_playing()
                    except Exception:
                        logger.exception("media re-init after stall failed")
                    if escalate:
                        stop_event.wait(ESCALATION_PAUSE_SECONDS)
                    continue
                except Exception as exc:
                    logger.exception("Realtime session stopped with an error")
                    self.runtime_status.record_error(exc)
                    escalate = budget.record_restart(time.monotonic())
                    if escalate:
                        self.runtime_status.record_event(
                            "supervisor.escalated", restarts=budget.limit, window_seconds=budget.window_seconds
                        )
                        try:
                            reachy_mini.media.stop_playing()
                            reachy_mini.media.stop_recording()
                            reachy_mini.media.start_recording()
                            reachy_mini.media.start_playing()
                        except Exception:
                            logger.exception("media re-init during escalation failed")
                        stop_event.wait(ESCALATION_PAUSE_SECONDS)
                    elif stop_event.wait(5.0):
                        break
                    self.runtime_status.set_phase(
                        "reconnecting",
                        "Realtimeセッションを再起動しています",
                        connected=False,
                        event=True,
                        detail_key="detail_reconnecting",
                    )
                else:
                    if outcome is SessionOutcome.FATAL_CONFIG:
                        stale_fingerprint = (os.getenv("OPENAI_API_KEY", ""), config)
                        while not stop_event.is_set():
                            load_instance_env()
                            current = (os.getenv("OPENAI_API_KEY", ""), AppConfig.from_env())
                            if current != stale_fingerprint:
                                break
                            stop_event.wait(2.0)
        finally:
            try:
                reachy_mini.media.stop_recording()
            finally:
                reachy_mini.media.stop_playing()
                motion.close()
                self.runtime_status.set_component_health("motion", False, expires=False)
                with self._camera_lock:
                    self._camera_enabled = False
                    self._camera_available = False
                    self._reachy_mini = None
                self._session_started = False
                self.runtime_status.set_phase(
                    "stopped",
                    "アプリを停止しました",
                    connected=False,
                    event=True,
                    detail_key="detail_stopped",
                )
                recorder.record("app.stop")
                recorder.close()
                logging.getLogger().removeHandler(file_handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = ReachyOpenaiRealtime()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        logger.info("Stopping")
        app.stop()
