from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np
from openai import AsyncOpenAI
from openai.types.realtime import (
    RealtimeAudioConfigInputParam,
    RealtimeAudioConfigOutputParam,
    RealtimeAudioConfigParam,
    RealtimeSessionCreateRequestParam,
)

from .audio.capture import AudioPipelineStalled, AudioRecoveryLadder, CaptureWorker
from .audio.playback import PlaybackBuffer, PlaybackChunk, SpeakerWorker
from .dsp import (
    audio_level_dbfs,
    float32_to_pcm16,
    pcm16_to_float32,
    resample_linear,
    select_mono_float32,
)
from .config import (
    AppConfig,
    greeting_instructions,
    language_option,
    response_instructions,
    session_instructions,
)
from .motion import TOOL_DEFINITIONS, MotionController
from .runtime_status import RuntimeStatus, safe_message
from .session.fsm import SessionState, SessionStateMachine
from .session.recovery import BackoffPolicy, ErrorClass, SessionOutcome, classify_connection_error
from .session.watchdog import DeadlineWatchdog, WatchdogTimeout
from .vad import EnergyTurnDetector

logger = logging.getLogger(__name__)


class RecentIds:
    """Bounded remembered-ID set (spec §27: interrupted response IDs must not grow forever)."""

    def __init__(self, max_size: int = 32) -> None:
        self._order: deque[str] = deque()
        self._members: set[str] = set()
        self._max_size = max_size

    def add(self, value: str) -> None:
        if value in self._members:
            return
        if len(self._order) >= self._max_size:
            self._members.discard(self._order.popleft())
        self._order.append(value)
        self._members.add(value)

    def __contains__(self, value: object) -> bool:
        return value in self._members

    def __len__(self) -> int:
        return len(self._order)

    def clear(self) -> None:
        self._order.clear()
        self._members.clear()


class DoAPoller:
    """Read the ReSpeaker USB control endpoint without blocking audio capture."""

    def __init__(
        self,
        read_doa: Callable[[], tuple[float, bool] | None],
        stop_event: Any,
        *,
        interval_seconds: float = 0.1,
    ) -> None:
        self._read_doa = read_doa
        self._app_stop_event = stop_event
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._first_read_event = threading.Event()
        self._lock = threading.Lock()
        self._latest: tuple[float, bool] | None = None
        self._latest_at = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name="respeaker-doa-poller",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=0.2)

    def wait_for_initial_read(self, timeout_seconds: float = 0.05) -> None:
        self._first_read_event.wait(timeout_seconds)

    def latest(self, *, max_age_seconds: float = 0.6) -> tuple[float, bool] | None:
        with self._lock:
            if time.monotonic() - self._latest_at > max_age_seconds:
                return None
            return self._latest

    def _run(self) -> None:
        while not self._stop_event.is_set() and not self._app_stop_event.is_set():
            try:
                result = self._read_doa()
            except Exception:
                logger.debug("DoA speech detection failed", exc_info=True)
                result = None
            self._first_read_event.set()
            if result is not None:
                with self._lock:
                    self._latest = result
                    self._latest_at = time.monotonic()
            self._stop_event.wait(self._interval_seconds)


class RealtimeRobotSession:
    def __init__(
        self,
        robot: Any,
        motion: MotionController,
        config: AppConfig,
        status: RuntimeStatus,
        language_provider: Callable[[], str] | None = None,
        camera_enabled: Callable[[], bool] | None = None,
        capture_camera_jpeg: Callable[[], bytes | None] | None = None,
    ) -> None:
        self.robot = robot
        self.motion = motion
        self.config = config
        self.status = status
        self._language_provider = language_provider
        self._camera_enabled_callback = camera_enabled
        self._capture_camera_jpeg = capture_camera_jpeg
        self.client = AsyncOpenAI()
        self.connection: Any = None
        self.connection_epoch = 0
        self._pending_tool_outputs: list[tuple[int, str, str]] = []
        self._playback = PlaybackBuffer()
        self._speaker = SpeakerWorker(self.robot.media, on_write=self._on_speaker_write)
        self._last_speaker_write_at = time.monotonic()
        self._playback_io_lock = asyncio.Lock()
        self._greeting_sent = False
        self.fsm = SessionStateMachine(on_transition=self._on_fsm_transition)
        self._response_generation_done = True
        self._speaker_busy_until = 0.0
        self._current_response_id: str | None = None
        self._current_audio_item_id: str | None = None
        self._current_audio_content_index = 0
        self._playback_started_at: float | None = None
        self._playback_pushed_ms = 0.0
        self._interrupted_response_ids: RecentIds = RecentIds()
        self._camera_capture_task: asyncio.Task[bool] | None = None
        self._last_camera_item_id: str | None = None
        self._pending_camera_items: dict[str, int] = {}
        self._camera_add_events: dict[str, str] = {}
        self._camera_delete_events: dict[str, str] = {}
        self._doa_poller: DoAPoller | None = None
        self._vad = EnergyTurnDetector()
        self.watchdog = DeadlineWatchdog()
        self._mic_ladder = AudioRecoveryLadder()
        self._capture: CaptureWorker | None = None

    def _on_fsm_transition(self, old_state: SessionState, new_state: SessionState, reason: str) -> None:
        self.status.record_event(
            "fsm.transition", from_state=old_state.name, to_state=new_state.name, reason=reason
        )

    def _on_speaker_write(self, duration_ms: float, received_at: float) -> None:
        self._last_speaker_write_at = time.monotonic()  # Task 11 adds latency metrics here

    async def run(self, stop_event: Any) -> SessionOutcome:
        self._capture = CaptureWorker(self.robot.media)
        self._capture.start()
        self._speaker.start()
        try:
            backoff = BackoffPolicy()
            while not stop_event.is_set():
                self.connection_epoch += 1
                self.fsm.transition(SessionState.CONNECTING, reason="connect_attempt")
                self.status.record_event("realtime.connecting", epoch=self.connection_epoch)
                connected_at = time.monotonic()
                error: BaseException | None = None
                try:
                    await self._run_connection(stop_event)
                except asyncio.CancelledError:
                    raise
                except AudioPipelineStalled:
                    raise  # escalation: main.py rebuilds the entire app session (mic ladder attempt 3)
                except Exception as exc:
                    error = exc
                if stop_event.is_set():
                    break
                self.fsm.transition(SessionState.RECOVERING, reason="connection_lost")
                await self.reset_connection_state()
                if error is not None:
                    self.status.record_error(f"realtime connection failed: {error}")
                    if classify_connection_error(error) is ErrorClass.FATAL_CONFIG:
                        self.status.set_phase("error", "設定エラーが発生しました", connected=False, detail_key="detail_error")
                        self.status.record_event("realtime.error", fatal=True, message=str(error))
                        self.fsm.transition(SessionState.STOPPING, reason="fatal_config_error")
                        self.fsm.transition(SessionState.DISCONNECTED, reason="shutdown_complete")
                        return SessionOutcome.FATAL_CONFIG
                backoff.note_session_duration(time.monotonic() - connected_at)
                delay = backoff.next_delay()
                self.status.record_event("realtime.reconnect", delay_seconds=round(delay, 2))
                await self._sleep_unless_stopped(stop_event, delay)
            self.fsm.transition(SessionState.STOPPING, reason="stop_requested")
            await self.reset_connection_state()
            self.fsm.transition(SessionState.DISCONNECTED, reason="shutdown_complete")
            return SessionOutcome.STOPPED
        finally:
            self._speaker.close()
            self._capture.close()
            self._capture = None

    async def _sleep_unless_stopped(self, stop_event: Any, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.2, remaining))

    async def _run_connection(self, stop_event: Any) -> None:
        async with self.client.realtime.connect(model=self.config.model) as connection:
            self.connection = connection
            self.fsm.transition(SessionState.INITIALIZING, reason="socket_open")
            self._last_camera_item_id = None
            self._pending_camera_items.clear()
            self._camera_add_events.clear()
            self._camera_delete_events.clear()
            self.watchdog.arm("session_update")
            await connection.session.update(session=self._session_config())
            logger.info("Realtime session connected: model=%s voice=%s", self.config.model, self.config.voice)

            get_doa = getattr(self.robot.media, "get_DoA", None)
            if callable(get_doa):
                self._doa_poller = DoAPoller(get_doa, stop_event)
                self._doa_poller.start()

            tasks = [
                asyncio.create_task(self._record_loop(stop_event), name="record-loop"),
                asyncio.create_task(self._playback_loop(stop_event), name="playback-loop"),
                asyncio.create_task(self._event_loop(stop_event), name="event-loop"),
                asyncio.create_task(self._watchdog_loop(), name="watchdog-loop"),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                if self._doa_poller is not None:
                    self._doa_poller.close()
                    self._doa_poller = None
                self.motion.set_listening_enabled(False)
                self.motion.set_speaking_enabled(False)
                self.motion.set_idle_enabled(False)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.connection = None
                if not stop_event.is_set():
                    self.status.set_phase(
                        "disconnected",
                        "Realtime接続が切れました",
                        connected=False,
                        event=True,
                        detail_key="detail_disconnected",
                    )

    def _session_config(self) -> RealtimeSessionCreateRequestParam:
        pcm24: Any = {"type": "audio/pcm", "rate": 24_000}
        return RealtimeSessionCreateRequestParam(
            type="realtime",
            instructions=session_instructions(self._current_language()),
            audio=RealtimeAudioConfigParam(
                input=RealtimeAudioConfigInputParam(
                    format=pcm24,
                    noise_reduction={"type": "far_field"},
                    turn_detection=None,
                ),
                output=RealtimeAudioConfigOutputParam(
                    format=pcm24,
                    voice=self.config.voice,
                ),
            ),
            tools=TOOL_DEFINITIONS,  # type: ignore[arg-type]
            tool_choice="auto",
            output_modalities=["audio"],
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
        )

    def _current_language(self) -> str:
        provider = getattr(self, "_language_provider", None)
        configured = self.config.language
        if provider is not None:
            try:
                configured = provider()
            except Exception:
                logger.debug("Failed to read target language", exc_info=True)
        try:
            return language_option(configured).code
        except ValueError:
            return self.config.language

    def _listening_detail(self, *, connected: bool = False) -> str:
        language = language_option(self._current_language())
        prefix = "接続済み。" if connected else ""
        return f"{prefix}{language.label}で話しかけてください"

    def _listening_params(self) -> dict[str, str]:
        return {"language": language_option(self._current_language()).label}

    async def _record_loop(self, stop_event: Any) -> None:
        source_rate = self.robot.media.get_input_audio_samplerate()
        microphone_ready = False
        signal_detected = False
        last_level_update = 0.0
        pre_roll: deque[tuple[str, float]] = deque()
        pre_roll_ms = 0.0
        doa_speech_detected: bool | None = None
        doa_angle_degrees: float | None = None
        last_doa_update = 0.0
        doa_poller = getattr(self, "_doa_poller", None)
        if doa_poller is None:
            get_doa = getattr(self.robot.media, "get_DoA", None)
            if callable(get_doa):
                doa_poller = DoAPoller(get_doa, stop_event)
                doa_poller.start()
                doa_poller.wait_for_initial_read()
                self._doa_poller = doa_poller
        while not stop_event.is_set():
            sample = await asyncio.to_thread(self._capture.pop, 0.25)
            if sample is None:
                action = self._mic_ladder.next_action(self._capture.frame_age_seconds())
                if action == "restart_capture":
                    self.status.record_event("audio.capture.stalled", action=action)
                    await asyncio.to_thread(self._restart_capture)
                    self.status.record_event("audio.capture.restarted", action=action)
                elif action == "restart_media":
                    self.status.record_event("audio.capture.stalled", action=action)
                    await asyncio.to_thread(self._restart_media_pipeline)
                    self.status.record_event("audio.capture.restarted", action=action)
                elif action == "restart_session":
                    self.status.record_event("audio.capture.stalled", action=action)
                    raise AudioPipelineStalled("microphone frames stopped; capture and media restarts failed")
                continue
            if not microphone_ready:
                microphone_ready = True
                self.status.add_event(
                    f"マイク入力を開始しました（{source_rate} Hz）",
                    key="event_mic_started",
                    params={"rate": source_rate},
                )
            mono, selected_channel, channel_levels = select_mono_float32(sample)
            dbfs = audio_level_dbfs(mono)
            now = time.monotonic()
            if doa_poller is not None and now - last_doa_update >= 0.1:
                doa = doa_poller.latest()
                if doa is not None:
                    doa_angle, doa_speech_detected = doa
                    doa_angle_degrees = math.degrees(doa_angle)
                else:
                    doa_speech_detected = None
                    doa_angle_degrees = None
                last_doa_update = now
            state = self.fsm.state
            if state is SessionState.ASSISTANT_SPEAKING:
                if self._response_generation_done and now >= self._speaker_busy_until:
                    self.fsm.transition(SessionState.LISTENING, reason="playback_finished")
                    state = SessionState.LISTENING
            if state in (SessionState.LISTENING, SessionState.USER_SPEAKING):
                process_turn = True
                assistant_audio_active = False
            elif state in (SessionState.ASSISTANT_SPEAKING, SessionState.INTERRUPTING):
                process_turn = True
                assistant_audio_active = True
            else:
                self._vad.reset_turn()
                pre_roll.clear()
                pre_roll_ms = 0.0
                continue

            if (
                state is SessionState.LISTENING
                and not self._vad.speech_active
                and now >= self._speaker_busy_until
            ):
                self.motion.set_speaking_enabled(False)
                self.motion.set_idle_enabled(True)
                self.status.set_phase(
                    "listening",
                    self._listening_detail(),
                    connected=True,
                    detail_key="detail_listening",
                    detail_params=self._listening_params(),
                )
            if now - last_level_update >= 0.25:
                self.status.record_audio_sample(
                    dbfs=dbfs,
                    channel_dbfs=channel_levels,
                    selected_channel=selected_channel,
                    noise_floor_dbfs=self._vad.noise_floor_dbfs,
                    start_threshold_dbfs=self._vad.start_threshold_dbfs,
                    continue_threshold_dbfs=self._vad.continue_threshold_dbfs,
                    input_enabled=process_turn,
                    response_active=self.fsm.generation_active(),
                    doa_speech_detected=doa_speech_detected,
                    doa_angle_degrees=doa_angle_degrees,
                )
                last_level_update = now
            if (
                process_turn
                and not signal_detected
                and doa_speech_detected is not False
                and dbfs >= -60.0
            ):
                signal_detected = True
                self.status.add_event(
                    f"マイクの音声信号を検出しました（ch{selected_channel + 1}: {dbfs:.1f} dBFS）",
                    key="event_signal_detected",
                    params={
                        "channel": selected_channel + 1,
                        "dbfs": f"{dbfs:.1f}",
                    },
                )
            audio = resample_linear(mono, source_rate, self.config.input_rate)
            encoded = base64.b64encode(float32_to_pcm16(audio).tobytes()).decode("ascii")
            duration_ms = audio.size * 1_000.0 / self.config.input_rate

            was_active = self._vad.speech_active
            if not was_active:
                pre_roll.append((encoded, duration_ms))
                pre_roll_ms += duration_ms
                while pre_roll and pre_roll_ms > 350.0:
                    _, removed_ms = pre_roll.popleft()
                    pre_roll_ms -= removed_ms

            decision = self._vad.process(
                dbfs,
                duration_ms,
                # During playback, only the ReSpeaker's explicit human-speech
                # signal may open the gate. This prevents Reachy's own voice
                # from interrupting itself when AEC leaves residual energy.
                speech_detected=(
                    doa_speech_detected
                    if not assistant_audio_active
                    else doa_speech_detected is True
                ),
            )
            if decision.started:
                if assistant_audio_active:
                    await self._interrupt_assistant()
                self.fsm.transition(SessionState.USER_SPEAKING, reason="vad_started")
                self.motion.set_listening_enabled(True)
                self.status.record_motion(
                    "listening_nod",
                    {"state": "start"},
                    True,
                )
                self._start_camera_capture()
                self.motion.set_idle_enabled(False)
                self.status.set_phase(
                    "user_speaking",
                    "音声を聞いています",
                    connected=True,
                    event=True,
                    detail_key="detail_user_speaking",
                )
                self.status.add_event(
                    "ローカル音声判定: 発話開始 "
                    f"（{dbfs:.1f} dBFS / 閾値 {self._vad.start_threshold_dbfs:.1f} dBFS）",
                    key="event_local_speech_started",
                    params={
                        "dbfs": f"{dbfs:.1f}",
                        "threshold": f"{self._vad.start_threshold_dbfs:.1f}",
                    },
                )
                for buffered, _ in pre_roll:
                    await self._append_input_audio(buffered)
                    self.status.record_audio_sent()
                pre_roll.clear()
                pre_roll_ms = 0.0
            elif was_active:
                await self._append_input_audio(encoded)
                self.status.record_audio_sent()

            if decision.stopped:
                self.fsm.transition(SessionState.WAITING_RESPONSE, reason="turn_committed")
                self._response_generation_done = False
                self.motion.set_listening_enabled(False)
                self.status.record_motion(
                    "listening_nod",
                    {"state": "stop"},
                    True,
                )
                reason = "無音800ms" if decision.reason == "silence" else "発話上限20秒"
                detail_key = (
                    "detail_turn_silence"
                    if decision.reason == "silence"
                    else "detail_turn_maximum"
                )
                self.status.set_phase(
                    "thinking",
                    f"発話を確定しました（{reason}）",
                    connected=True,
                    event=True,
                    detail_key=detail_key,
                )
                # Ensure the image item is in the conversation before the audio
                # message is committed and response generation starts.
                await self._finish_camera_capture()
                self.watchdog.arm("input_append")
                await self.connection.input_audio_buffer.commit()
                self.status.record_audio_commit()
                self.watchdog.arm("response_create")
                await self.connection.response.create(
                    response={
                        "instructions": response_instructions(self._current_language()),
                        "output_modalities": ["audio"],
                    }
                )
                self.watchdog.disarm("input_append")
                self.status.record_response_request()

    def _restart_capture(self) -> None:
        self.robot.media.stop_recording()
        self.robot.media.start_recording()

    def _restart_media_pipeline(self) -> None:
        self.robot.media.stop_playing()
        self.robot.media.stop_recording()
        self.robot.media.start_recording()
        self.robot.media.start_playing()

    async def _append_input_audio(self, encoded: str) -> None:
        await asyncio.wait_for(
            self.connection.input_audio_buffer.append(audio=encoded),
            timeout=5.0,
        )

    def _camera_enabled(self) -> bool:
        callback = getattr(self, "_camera_enabled_callback", None)
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            logger.debug("Failed to read camera enabled state", exc_info=True)
            return False

    def _start_camera_capture(self) -> None:
        if not self._camera_enabled():
            self._camera_capture_task = None
            return
        previous = getattr(self, "_camera_capture_task", None)
        if previous is not None and not previous.done():
            previous.cancel()
        self._camera_capture_task = asyncio.create_task(
            self._capture_and_send_camera_image(),
            name="speech-camera-capture",
        )

    async def _finish_camera_capture(self) -> bool:
        task = getattr(self, "_camera_capture_task", None)
        self._camera_capture_task = None
        if task is None:
            return False
        try:
            return await task
        except asyncio.CancelledError:
            return False

    async def _capture_and_send_camera_image(self) -> bool:
        capture = getattr(self, "_capture_camera_jpeg", None)
        if capture is None or not self._camera_enabled():
            return False
        item_id: str | None = None
        event_id: str | None = None
        try:
            jpeg = await asyncio.to_thread(capture)
            if not jpeg:
                raise RuntimeError("カメラ画像を取得できません")
            if not self._camera_enabled():
                return False

            # Realtime item IDs accept at most 32 characters.
            item_id = f"img_{uuid.uuid4().hex[:28]}"
            event_id = f"cam_add_{uuid.uuid4().hex[:24]}"
            image_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
            self._pending_camera_items[item_id] = len(jpeg)
            self._camera_add_events[event_id] = item_id
            self.status.record_camera_image_sending()
            self.watchdog.arm("camera_item")
            await self.connection.conversation.item.create(
                item={
                    "id": item_id,
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": image_url,
                        }
                    ],
                },
                event_id=event_id,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if item_id is not None:
                self._pending_camera_items.pop(item_id, None)
                self.watchdog.disarm("camera_item")
            if event_id is not None:
                self._camera_add_events.pop(event_id, None)
            logger.warning("Failed to send speech camera image", exc_info=True)
            self.status.record_camera_send_error(exc)
            return False

    async def _confirm_camera_item(self, event: Any) -> None:
        item = getattr(event, "item", None)
        item_id_value = getattr(item, "id", None)
        if item_id_value is None:
            return
        item_id = str(item_id_value)
        byte_count = self._pending_camera_items.pop(item_id, None)
        if byte_count is None:
            return
        self.watchdog.disarm("camera_item")
        for event_id, pending_item_id in list(self._camera_add_events.items()):
            if pending_item_id == item_id:
                self._camera_add_events.pop(event_id, None)

        previous_item_id = self._last_camera_item_id
        self._last_camera_item_id = item_id
        self.status.record_camera_image_sent(byte_count)
        if previous_item_id is not None and previous_item_id != item_id:
            delete_event_id = f"cam_del_{uuid.uuid4().hex[:24]}"
            self._camera_delete_events[delete_event_id] = previous_item_id
            try:
                await self.connection.conversation.item.delete(
                    item_id=previous_item_id,
                    event_id=delete_event_id,
                )
            except Exception as exc:
                self._camera_delete_events.pop(delete_event_id, None)
                logger.warning("Failed to clean up prior camera image", exc_info=True)
                self.status.record_camera_cleanup_error(exc)

    def _handle_camera_protocol_error(self, error: Any) -> bool:
        client_event_id_value = getattr(error, "event_id", None)
        if client_event_id_value is None:
            return False
        client_event_id = str(client_event_id_value)
        item_id = self._camera_add_events.pop(client_event_id, None)
        if item_id is not None:
            self._pending_camera_items.pop(item_id, None)
            self.watchdog.disarm("camera_item")
            self.status.record_camera_send_error(getattr(error, "message", error))
            return True
        deleted_item_id = self._camera_delete_events.pop(client_event_id, None)
        if deleted_item_id is not None:
            self.status.record_camera_cleanup_error(getattr(error, "message", error))
            return True
        return False

    def _prepare_output(self, pcm: np.ndarray, target_rate: int) -> np.ndarray:
        """Convert int16 PCM to float32 and resample to the output device rate."""
        return resample_linear(pcm16_to_float32(pcm), self.config.output_rate, target_rate)

    async def _playback_loop(self, stop_event: Any) -> None:
        target_rate = self.robot.media.get_output_audio_samplerate()
        while not stop_event.is_set():
            chunk = await asyncio.to_thread(self._playback.pop_wait, 0.25, self.connection_epoch)
            if chunk is None:
                if not self._assistant_audio_active():
                    self.motion.set_speaking_enabled(False)
                continue
            pcm_out = self._prepare_output(chunk.pcm, target_rate)
            self.motion.set_speaking_enabled(True)
            async with self._playback_io_lock:
                accepted = await asyncio.to_thread(
                    self._speaker.submit, pcm_out, chunk.duration_ms, chunk.received_at, 1.0
                )
                if not accepted and self._speaker.stalled(2.0):
                    self.status.record_event("audio.playback.restarted", reason="speaker_stalled")
                    self._speaker.flush()
                    await asyncio.to_thread(self._restart_media_pipeline)
                else:
                    if self._playback_started_at is None:
                        self._playback_started_at = time.monotonic()
                    self._playback_pushed_ms += pcm_out.size * 1_000.0 / target_rate
                    self.status.record_audio_output_played()

    async def _event_loop(self, stop_event: Any) -> None:
        async for event in self.connection:
            if stop_event.is_set():
                return
            event_type = event.type
            self.status.record_realtime_event(event_type)
            if event_type == "session.updated":
                self.watchdog.disarm("session_update")
                self.status.clear_error()
                self.fsm.transition(SessionState.LISTENING, reason="session_updated")
                self.status.set_phase(
                    "listening",
                    self._listening_detail(connected=True)
                    + "（ロボット側で無音800msを判定）",
                    connected=True,
                    event=True,
                    detail_key="detail_listening_connected",
                    detail_params=self._listening_params(),
                )
                if not self._greeting_sent:
                    self._greeting_sent = True
                    self.fsm.transition(SessionState.WAITING_RESPONSE, reason="greeting_requested")
                    self._response_generation_done = False
                    self.watchdog.arm("response_create")
                    await self.connection.response.create(
                        response={
                            "instructions": greeting_instructions(self._current_language()),
                            "output_modalities": ["audio"],
                            "tool_choice": "none",
                        }
                    )
                    self.status.record_response_request()
            elif event_type == "input_audio_buffer.speech_started":
                await self._clear_playback()
                self.motion.stop_current()
                self.status.set_phase(
                    "user_speaking",
                    "音声を聞いています",
                    connected=True,
                    event=True,
                    detail_key="detail_user_speaking",
                )
            elif event_type == "input_audio_buffer.speech_stopped":
                self.status.set_phase(
                    "thinking",
                    "無音を検出。発話を理解しています",
                    connected=True,
                    event=True,
                    detail_key="detail_understanding",
                )
            elif event_type in {"conversation.item.added", "conversation.item.created"}:
                await self._confirm_camera_item(event)
            elif event_type == "conversation.item.deleted":
                deleted_item_id = str(getattr(event, "item_id", ""))
                for event_id, item_id in list(self._camera_delete_events.items()):
                    if item_id == deleted_item_id:
                        self._camera_delete_events.pop(event_id, None)
            elif event_type == "response.created":
                self.watchdog.disarm("response_create")
                self.watchdog.disarm("tool_response")
                self.watchdog.arm("first_output")
                self.motion.set_listening_enabled(False)
                self.motion.set_speaking_enabled(False)
                self.motion.set_idle_enabled(False)
                response = getattr(event, "response", None)
                response_id = getattr(response, "id", None)
                self._current_response_id = str(response_id) if response_id else None
                self._current_audio_item_id = None
                self._current_audio_content_index = 0
                self._playback_started_at = None
                self._playback_pushed_ms = 0.0
                self.status.set_phase(
                    "responding",
                    "応答を生成しています",
                    connected=True,
                    event=True,
                    detail_key="detail_responding",
                )
            elif event_type == "response.output_audio.delta":
                response_id = str(event.response_id)
                if response_id in self._interrupted_response_ids:
                    continue
                self.watchdog.disarm("first_output")
                pcm = np.frombuffer(base64.b64decode(event.delta), dtype=np.int16)
                self._current_audio_item_id = str(event.item_id)
                self._current_audio_content_index = int(event.content_index)
                self.status.record_audio_output_received()
                now = time.monotonic()
                duration = pcm.size / self.config.output_rate
                self._speaker_busy_until = max(now, self._speaker_busy_until) + duration
                self.fsm.transition(SessionState.ASSISTANT_SPEAKING, reason="first_audio_received")
                self.status.set_phase(
                    "assistant_speaking",
                    "Reachyが話しています",
                    connected=True,
                    detail_key="detail_assistant_speaking",
                )
                result = self._playback.push(
                    PlaybackChunk(
                        epoch=self.connection_epoch,
                        response_id=self._current_response_id or "",
                        pcm=pcm,
                        duration_ms=duration * 1_000.0,
                        received_at=time.monotonic(),
                    )
                )
                if result.overrun:
                    await self._handle_playback_overrun(self._playback.clear())
            elif event_type == "response.output_audio_transcript.done":
                if str(event.response_id) in self._interrupted_response_ids:
                    continue
                transcript = (event.transcript or "").strip()
                logger.info("Reachy: %s", transcript)
                self.status.record_transcript("assistant", transcript)
            elif event_type == "response.output_audio.done":
                if str(event.response_id) not in self._interrupted_response_ids:
                    self._speaker_busy_until += 0.3
            elif event_type == "response.function_call_arguments.done":
                if str(event.response_id) not in self._interrupted_response_ids:
                    self.watchdog.disarm("first_output")
                    await self._handle_tool_call(event)
            elif event_type == "response.done":
                self.watchdog.disarm("first_output")
                self.watchdog.disarm("response_cancel")
                response = getattr(event, "response", None)
                response_id_value = getattr(response, "id", None)
                response_id = str(response_id_value) if response_id_value else None
                response_status = getattr(response, "status", None)
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.status.record_usage(self.config.model, usage)
                was_interrupted = bool(
                    response_id and response_id in self._interrupted_response_ids
                )
                self._response_generation_done = True
                if was_interrupted:
                    if self._vad.speech_active:
                        self.status.set_phase(
                            "user_speaking",
                            "音声を聞いています",
                            connected=True,
                            detail_key="detail_user_speaking",
                        )
                else:
                    has_tool_outputs = bool(self._pending_tool_outputs)
                    if has_tool_outputs:
                        self.fsm.transition(SessionState.TOOL_EXECUTION, reason="tool_outputs_pending")
                        await self._flush_tool_outputs()
                    else:
                        now = time.monotonic()
                        if not self._assistant_audio_active(now):
                            self.fsm.transition(SessionState.LISTENING, reason="response_completed")
                            self.motion.set_speaking_enabled(False)
                            self.status.set_phase(
                                "listening",
                                self._listening_detail(),
                                connected=True,
                                event=True,
                                detail_key="detail_listening",
                                detail_params=self._listening_params(),
                            )
                        else:
                            self.status.set_phase(
                                "assistant_speaking",
                                "Reachyが話しています（割り込み可能）",
                                connected=True,
                                detail_key="detail_assistant_interruptible",
                            )
                if response_status and response_status not in {"completed", "cancelled"}:
                    details = getattr(response, "status_details", response_status)
                    self.status.add_event(f"応答終了: {safe_message(details)}", level="error")
            elif event_type == "error":
                error = getattr(event, "error", event)
                if self._handle_camera_protocol_error(error):
                    continue
                self.motion.set_idle_enabled(False)
                logger.error("Realtime API error: %s", error)
                self.status.record_error(safe_message(getattr(error, "message", error)))

    async def _handle_tool_call(self, event: Any) -> None:
        call_id = str(event.call_id)
        arguments: dict[str, Any] = {}
        try:
            arguments = json.loads(event.arguments or "{}")
            if not isinstance(arguments, dict):
                raise TypeError("モーション引数がdictではありません")
            result = self.motion.submit(str(event.name), arguments)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        self.status.record_motion(str(event.name), arguments, bool(result.get("ok")))
        self._pending_tool_outputs.append((self.connection_epoch, call_id, json.dumps(result, ensure_ascii=False)))

    async def _flush_tool_outputs(self) -> None:
        pending, self._pending_tool_outputs = self._pending_tool_outputs, []
        for epoch, call_id, output in pending:
            if epoch != self.connection_epoch:
                continue
            await self.connection.conversation.item.create(
                item={"type": "function_call_output", "call_id": call_id, "output": output}
            )
        self.fsm.transition(SessionState.WAITING_RESPONSE, reason="tool_outputs_submitted")
        self._response_generation_done = False
        self.watchdog.arm("response_create")
        await self.connection.response.create(
            response={
                "instructions": response_instructions(self._current_language()),
                "output_modalities": ["audio"],
            }
        )
        self.watchdog.arm("tool_response")
        self.status.record_response_request()

    def _assistant_audio_active(self, now: float | None = None) -> bool:
        return self.fsm.generation_active() or (now if now is not None else time.monotonic()) < self._speaker_busy_until

    def _played_audio_end_ms(self) -> int | None:
        if self._current_audio_item_id is None:
            return None
        if self._playback_started_at is None:
            return 0
        elapsed_ms = max(0.0, (time.monotonic() - self._playback_started_at) * 1_000.0)
        return int(min(self._playback_pushed_ms, elapsed_ms))

    async def _interrupt_assistant(self) -> None:
        epoch = self.connection_epoch
        generation_active = self.fsm.generation_active()
        self.fsm.transition(SessionState.INTERRUPTING, reason="barge_in")
        response_id = self._current_response_id
        item_id = self._current_audio_item_id
        audio_end_ms = self._played_audio_end_ms()

        if response_id is not None:
            self._interrupted_response_ids.add(response_id)
        self._pending_tool_outputs.clear()
        self.motion.stop_current()

        if generation_active:
            if response_id is None:
                await self.connection.response.cancel()
            else:
                await self.connection.response.cancel(response_id=response_id)
            self.watchdog.arm("response_cancel")

        await self._clear_playback()

        if item_id is not None and audio_end_ms is not None:
            await self.connection.conversation.item.truncate(
                item_id=item_id,
                content_index=self._current_audio_content_index,
                audio_end_ms=audio_end_ms,
            )

        if epoch != self.connection_epoch:
            return  # connection turned over mid-interrupt; new epoch owns the state

        self._speaker_busy_until = time.monotonic()
        self.status.record_interruption(audio_end_ms)
        self.fsm.transition(SessionState.USER_SPEAKING, reason="user_turn_continues")

    async def _handle_playback_overrun(self, dropped_ms: float) -> None:
        self.status.record_event("audio.playback.overrun", dropped_ms=round(dropped_ms, 1))
        self.status.add_event("warning", "playback overran; dropping stale audio")
        response_id = self._current_response_id
        if response_id and self.connection is not None:
            self._interrupted_response_ids.add(response_id)
            try:
                await self.connection.response.cancel(response_id=response_id)
                self.watchdog.arm("response_cancel")
            except Exception:
                logger.exception("response.cancel after overrun failed")
        await self._clear_playback()
        self._speaker_busy_until = time.monotonic()
        self.fsm.transition(SessionState.LISTENING, reason="playback_overrun")

    async def _watchdog_loop(self) -> None:
        try:
            await self.watchdog.watch()
        except WatchdogTimeout as exc:
            self.status.record_event(
                "watchdog.triggered", operation=exc.operation, timeout_seconds=exc.timeout_seconds
            )
            self.status.add_event("warning", f"protocol watchdog: {exc.operation} timed out")
            raise

    async def reset_connection_state(self) -> None:
        """Spec §4: a reconnect must never inherit partially active response state."""
        self.watchdog.clear()
        if self._camera_capture_task is not None:
            self._camera_capture_task.cancel()
            self._camera_capture_task = None
        self._playback.clear()
        self._speaker.flush()
        try:
            self.motion.stop_current()
        except Exception:
            logger.exception("motion stop during reset failed")
        self._vad.reset_turn()
        self._pending_tool_outputs.clear()
        self._interrupted_response_ids.clear()
        self._current_response_id = None
        self._current_audio_item_id = None
        self._current_audio_content_index = 0
        self._playback_started_at = None
        self._playback_pushed_ms = 0.0
        self._speaker_busy_until = time.monotonic()
        self._pending_camera_items.clear()
        self._camera_add_events.clear()
        self._camera_delete_events.clear()
        self._last_camera_item_id = None
        self._response_generation_done = True

    async def _clear_playback(self) -> None:
        self._playback.clear()
        self._speaker.flush()
        audio = getattr(self.robot.media, "audio", None)
        if audio is not None and hasattr(audio, "clear_player"):
            try:
                async with self._playback_io_lock:
                    try:
                        await asyncio.to_thread(audio.clear_player)
                    finally:
                        # The Wireless local backend shares one GStreamer pipeline
                        # for capture and playback. Reassert PLAYING after a flush
                        # so barge-in never leaves the microphone stalled.
                        await asyncio.to_thread(self.robot.media.start_recording)
            except Exception:
                logger.debug("clear_player failed", exc_info=True)
