from __future__ import annotations

import threading
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

from .observability.events import EventRecorder, redact_secrets
from .observability.metrics import MetricsRegistry
from .usage import UsageTracker


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_message(value: object, limit: int = 400) -> str:
    text = redact_secrets(str(value)).replace("\n", " ").strip()
    return text[:limit]


class RuntimeStatus:
    def __init__(self, usage_tracker: UsageTracker | None = None) -> None:
        self._lock = threading.Lock()
        self._usage_tracker = usage_tracker or UsageTracker()
        self.metrics = MetricsRegistry()
        self._recorder: EventRecorder | None = None
        self._phase = "starting"
        self._detail = "アプリを起動しています"
        self._detail_key = "detail_starting"
        self._detail_params: dict[str, Any] = {}
        self._connected = False
        self._last_error: str | None = None
        self._last_user: str | None = None
        self._last_assistant: str | None = None
        self._last_motion: str | None = None
        self._mic_dbfs: float | None = None
        self._mic_peak_dbfs: float | None = None
        self._mic_channel_dbfs: list[float] = []
        self._selected_channel: int | None = None
        self._vad_diagnostics: dict[str, Any] = {}
        self._audio_samples = 0
        self._audio_chunks_sent = 0
        self._audio_commits = 0
        self._response_requests = 0
        self._audio_output_chunks_received = 0
        self._audio_output_chunks_played = 0
        self._interruptions = 0
        self._camera_images_sent = 0
        self._camera_bytes_sent = 0
        self._camera_send_errors = 0
        self._last_camera_image_at: str | None = None
        self._realtime_event_counts: Counter[str] = Counter()
        self._last_realtime_event: str | None = None
        self._realtime_events: deque[dict[str, str]] = deque(maxlen=100)
        self._updated_at = _now()
        self._events: deque[dict[str, Any]] = deque(maxlen=40)
        self._append_event_locked(
            "info",
            "アプリを起動しました",
            key="event_app_started",
        )

    def attach_recorder(self, recorder: EventRecorder) -> None:
        self._recorder = recorder

    def record_event(self, event: str, **fields: object) -> None:
        """Forward a structured event to the flight recorder, if one is attached."""
        if self._recorder is not None:
            self._recorder.record(event, **fields)

    def set_phase(
        self,
        phase: str,
        detail: str,
        *,
        connected: bool | None = None,
        event: bool = False,
        detail_key: str | None = None,
        detail_params: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            changed = phase != self._phase or detail != self._detail
            self._phase = phase
            self._detail = safe_message(detail)
            self._detail_key = detail_key
            self._detail_params = dict(detail_params or {})
            if connected is not None:
                self._connected = connected
            connected_snapshot = self._connected
            self._updated_at = _now()
            if event and changed:
                self._append_event_locked(
                    "info",
                    self._detail,
                    key=detail_key,
                    params=detail_params,
                )
        self.record_event("status.phase", phase=phase, connected=connected_snapshot)

    def record_error(self, error: object) -> None:
        message = safe_message(error)
        with self._lock:
            self._phase = "error"
            self._detail = "接続または音声処理でエラーが発生しました"
            self._detail_key = "detail_error"
            self._detail_params = {}
            self._connected = False
            self._last_error = message or type(error).__name__
            self._updated_at = _now()
            self._append_event_locked("error", self._last_error)
        self.record_event("status.error", message=message)

    def clear_error(self) -> None:
        with self._lock:
            self._last_error = None
            self._updated_at = _now()

    def record_transcript(self, role: str, text: str) -> None:
        message = safe_message(text, limit=1000)
        if not message:
            return
        with self._lock:
            if role == "user":
                self._last_user = message
                label = f"あなた: {message}"
                key = "event_user_transcript"
            else:
                self._last_assistant = message
                label = f"Reachy: {message}"
                key = "event_assistant_transcript"
            self._updated_at = _now()
            self._append_event_locked(
                "conversation",
                label,
                key=key,
                params={"text": message},
            )

    def record_motion(self, name: str, arguments: dict[str, Any], ok: bool) -> None:
        summary = f"{name} {safe_message(arguments, limit=120)}"
        with self._lock:
            self._last_motion = summary
            self._updated_at = _now()
            self._append_event_locked("motion" if ok else "error", summary)

    def record_mic_level(self, dbfs: float) -> None:
        with self._lock:
            self._mic_dbfs = round(max(-80.0, min(0.0, dbfs)), 1)
            self._updated_at = _now()

    def record_audio_sample(
        self,
        *,
        dbfs: float,
        channel_dbfs: list[float],
        selected_channel: int,
        noise_floor_dbfs: float,
        start_threshold_dbfs: float,
        continue_threshold_dbfs: float,
        input_enabled: bool,
        response_active: bool,
        doa_speech_detected: bool | None = None,
        doa_angle_degrees: float | None = None,
    ) -> None:
        level = round(max(-80.0, min(0.0, dbfs)), 1)
        with self._lock:
            self._audio_samples += 1
            self._mic_dbfs = level
            if (
                input_enabled
                and not response_active
                and (self._mic_peak_dbfs is None or level > self._mic_peak_dbfs)
            ):
                self._mic_peak_dbfs = level
            self._mic_channel_dbfs = [round(value, 1) for value in channel_dbfs]
            self._selected_channel = selected_channel
            self._vad_diagnostics = {
                "noise_floor_dbfs": round(noise_floor_dbfs, 1),
                "start_threshold_dbfs": round(start_threshold_dbfs, 1),
                "continue_threshold_dbfs": round(continue_threshold_dbfs, 1),
                "input_enabled": input_enabled,
                "response_active": response_active,
                "doa_speech_detected": doa_speech_detected,
                "doa_angle_degrees": (
                    round(doa_angle_degrees, 1)
                    if doa_angle_degrees is not None
                    else None
                ),
            }
            self._updated_at = _now()

    def record_audio_sent(self) -> None:
        with self._lock:
            self._audio_chunks_sent += 1
            self._updated_at = _now()

    def record_audio_commit(self) -> None:
        with self._lock:
            self._audio_commits += 1
            self._updated_at = _now()

    def record_response_request(self) -> None:
        with self._lock:
            self._response_requests += 1
            self._updated_at = _now()

    def record_usage(self, model: str, usage: object) -> None:
        recorded = self._usage_tracker.record(model, usage)
        if recorded is None:
            return
        response = recorded["response"]
        lifetime = recorded["lifetime"]
        response_cost = response.get("estimated_cost_usd")
        lifetime_cost = lifetime.get("estimated_cost_usd")
        with self._lock:
            self._updated_at = _now()
            self._append_event_locked(
                "info",
                f"API使用量: +{response['total_tokens']}トークン"
                + (
                    f"（推定 ${response_cost:.6f} / 累計 ${lifetime_cost:.6f}）"
                    if response_cost is not None and lifetime_cost is not None
                    else "（料金推定対象外のモデル）"
                ),
                key=(
                    "event_usage_recorded"
                    if response_cost is not None and lifetime_cost is not None
                    else "event_usage_unpriced"
                ),
                params={
                    "tokens": response["total_tokens"],
                    "cost": (
                        f"${response_cost:.6f}" if response_cost is not None else "—"
                    ),
                    "total": (
                        f"${lifetime_cost:.6f}" if lifetime_cost is not None else "—"
                    ),
                    "model": model,
                },
            )

    def record_audio_output_received(self) -> None:
        with self._lock:
            self._audio_output_chunks_received += 1
            self._updated_at = _now()

    def record_audio_output_played(self) -> None:
        with self._lock:
            self._audio_output_chunks_played += 1
            self._updated_at = _now()

    def record_interruption(self, audio_end_ms: int | None) -> None:
        with self._lock:
            self._interruptions += 1
            suffix = f"（再生済み {audio_end_ms}ms）" if audio_end_ms is not None else ""
            self._append_event_locked(
                "info",
                f"割り込み: Reachyの発話を停止しました{suffix}",
                key=(
                    "event_interruption_played"
                    if audio_end_ms is not None
                    else "event_interruption"
                ),
                params={"ms": audio_end_ms} if audio_end_ms is not None else None,
            )
            self._updated_at = _now()

    def record_camera_image_sent(self, byte_count: int) -> None:
        with self._lock:
            now = _now()
            self._camera_images_sent += 1
            self._camera_bytes_sent += max(0, byte_count)
            self._last_camera_image_at = now
            self._updated_at = now
            self._append_event_locked(
                "info",
                f"発話開始時のカメラ画像をOpenAIへ送信しました（{byte_count // 1024} KiB）",
                key="event_camera_sent",
                params={"size": byte_count // 1024},
            )

    def record_camera_image_sending(self) -> None:
        with self._lock:
            self._updated_at = _now()
            self._append_event_locked(
                "info",
                "発話を検知。カメラ画像をOpenAIへ送信しています",
                key="event_camera_sending",
            )

    def record_camera_send_error(self, error: object) -> None:
        message = safe_message(error)
        with self._lock:
            self._camera_send_errors += 1
            self._updated_at = _now()
            self._append_event_locked(
                "error",
                f"カメラ画像をOpenAIへ送信できませんでした: {message}",
            )

    def record_camera_cleanup_error(self, error: object) -> None:
        message = safe_message(error)
        with self._lock:
            self._updated_at = _now()
            self._append_event_locked(
                "error",
                f"古いカメラ画像を会話履歴から削除できませんでした: {message}",
            )

    def record_realtime_event(self, event_type: str) -> None:
        event_type = safe_message(event_type, limit=120)
        with self._lock:
            self._realtime_event_counts[event_type] += 1
            self._last_realtime_event = event_type
            if not event_type.endswith(".delta"):
                self._realtime_events.append({"time": _now(), "type": event_type})
            self._updated_at = _now()

    def add_event(
        self,
        message: str,
        level: str = "info",
        *,
        key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        safe = safe_message(message)
        with self._lock:
            self._append_event_locked(
                level,
                safe,
                key=key,
                params=params,
            )
            self._updated_at = _now()
        self.record_event("status.message", level=level, message=safe, key=key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = {
                "phase": self._phase,
                "detail": self._detail,
                "detail_key": self._detail_key,
                "detail_params": dict(self._detail_params),
                "connected": self._connected,
                "last_error": self._last_error,
                "last_user": self._last_user,
                "last_assistant": self._last_assistant,
                "last_motion": self._last_motion,
                "mic_dbfs": self._mic_dbfs,
                "mic_peak_dbfs": self._mic_peak_dbfs,
                "mic_channel_dbfs": list(self._mic_channel_dbfs),
                "selected_channel": self._selected_channel,
                "vad": dict(self._vad_diagnostics),
                "audio_samples": self._audio_samples,
                "audio_chunks_sent": self._audio_chunks_sent,
                "audio_commits": self._audio_commits,
                "response_requests": self._response_requests,
                "audio_output_chunks_received": self._audio_output_chunks_received,
                "audio_output_chunks_played": self._audio_output_chunks_played,
                "interruptions": self._interruptions,
                "camera_images_sent": self._camera_images_sent,
                "camera_bytes_sent": self._camera_bytes_sent,
                "camera_send_errors": self._camera_send_errors,
                "last_camera_image_at": self._last_camera_image_at,
                "last_realtime_event": self._last_realtime_event,
                "realtime_event_counts": dict(self._realtime_event_counts),
                "realtime_events": list(reversed(self._realtime_events)),
                "updated_at": self._updated_at,
                "events": list(reversed(self._events)),
            }
        snapshot["usage"] = self._usage_tracker.snapshot()
        snapshot["metrics"] = self.metrics.snapshot()
        return snapshot

    def _append_event_locked(
        self,
        level: str,
        message: str,
        *,
        key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "time": _now(),
            "level": level,
            "message": message,
        }
        if key:
            event["key"] = key
            event["params"] = dict(params or {})
        self._events.append(event)
