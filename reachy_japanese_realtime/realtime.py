from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import numpy as np
from openai import AsyncOpenAI
from openai.types.realtime import (
    AudioTranscriptionParam,
    RealtimeAudioConfigInputParam,
    RealtimeAudioConfigOutputParam,
    RealtimeAudioConfigParam,
    RealtimeSessionCreateRequestParam,
)
from openai.types.realtime.realtime_audio_input_turn_detection_param import SemanticVad

from .audio import (
    float32_to_pcm16,
    pcm16_to_float32,
    resample_linear,
    to_mono_float32,
)
from .config import JAPANESE_INSTRUCTIONS, AppConfig
from .motion import TOOL_DEFINITIONS, MotionController

logger = logging.getLogger(__name__)


class RealtimeRobotSession:
    def __init__(self, robot: Any, motion: MotionController, config: AppConfig) -> None:
        self.robot = robot
        self.motion = motion
        self.config = config
        self.client = AsyncOpenAI()
        self.connection: Any = None
        self._pending_tool_outputs: list[tuple[str, str]] = []
        self._playback_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)

    async def run(self, stop_event: Any) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.config.reconnect_attempts + 1):
            if stop_event.is_set():
                return
            try:
                await self._run_connection(stop_event)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.exception("Realtime connection failed (%d/%d)", attempt, self.config.reconnect_attempts)
                if attempt < self.config.reconnect_attempts:
                    await asyncio.sleep(2 ** (attempt - 1))
        if last_error is not None:
            raise RuntimeError("Realtime API reconnect attempts exhausted") from last_error

    async def _run_connection(self, stop_event: Any) -> None:
        async with self.client.realtime.connect(model=self.config.model) as connection:
            self.connection = connection
            await connection.session.update(session=self._session_config())
            logger.info("Realtime session connected: model=%s voice=%s", self.config.model, self.config.voice)

            tasks = [
                asyncio.create_task(self._record_loop(stop_event), name="record-loop"),
                asyncio.create_task(self._playback_loop(stop_event), name="playback-loop"),
                asyncio.create_task(self._event_loop(stop_event), name="event-loop"),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.connection = None

    def _session_config(self) -> RealtimeSessionCreateRequestParam:
        pcm24: Any = {"type": "audio/pcm", "rate": 24_000}
        return RealtimeSessionCreateRequestParam(
            type="realtime",
            instructions=JAPANESE_INSTRUCTIONS,
            audio=RealtimeAudioConfigParam(
                input=RealtimeAudioConfigInputParam(
                    format=pcm24,
                    transcription=AudioTranscriptionParam(
                        model="gpt-realtime-whisper",
                        language="ja",
                    ),
                    turn_detection=SemanticVad(
                        type="semantic_vad",
                        interrupt_response=True,
                        eagerness="medium",
                    ),
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

    async def _record_loop(self, stop_event: Any) -> None:
        source_rate = self.robot.media.get_input_audio_samplerate()
        while not stop_event.is_set():
            sample = await asyncio.to_thread(self.robot.media.get_audio_sample)
            if sample is None:
                await asyncio.sleep(0.005)
                continue
            mono = to_mono_float32(sample)
            audio = resample_linear(mono, source_rate, self.config.input_rate)
            encoded = base64.b64encode(float32_to_pcm16(audio).tobytes()).decode("ascii")
            await self.connection.input_audio_buffer.append(audio=encoded)

    async def _playback_loop(self, stop_event: Any) -> None:
        target_rate = self.robot.media.get_output_audio_samplerate()
        while not stop_event.is_set():
            try:
                sample = await asyncio.wait_for(self._playback_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            audio = resample_linear(sample, self.config.output_rate, target_rate)
            await asyncio.to_thread(self.robot.media.push_audio_sample, audio)
            self._playback_queue.task_done()

    async def _event_loop(self, stop_event: Any) -> None:
        async for event in self.connection:
            if stop_event.is_set():
                return
            event_type = event.type
            if event_type == "input_audio_buffer.speech_started":
                self._clear_playback()
                self.motion.stop_current()
            elif event_type == "conversation.item.input_audio_transcription.completed":
                logger.info("User: %s", (event.transcript or "").strip())
            elif event_type == "response.output_audio.delta":
                pcm = np.frombuffer(base64.b64decode(event.delta), dtype=np.int16)
                try:
                    self._playback_queue.put_nowait(pcm16_to_float32(pcm))
                except asyncio.QueueFull:
                    logger.warning("Dropping output audio: playback queue full")
            elif event_type == "response.output_audio_transcript.done":
                logger.info("Reachy: %s", (event.transcript or "").strip())
            elif event_type == "response.function_call_arguments.done":
                await self._handle_tool_call(event)
            elif event_type == "response.done" and self._pending_tool_outputs:
                await self._flush_tool_outputs()
            elif event_type == "error":
                logger.error("Realtime API error: %s", getattr(event, "error", event))

    async def _handle_tool_call(self, event: Any) -> None:
        call_id = str(event.call_id)
        try:
            arguments = json.loads(event.arguments or "{}")
            result = self.motion.submit(str(event.name), arguments)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
        self._pending_tool_outputs.append((call_id, json.dumps(result, ensure_ascii=False)))

    async def _flush_tool_outputs(self) -> None:
        pending, self._pending_tool_outputs = self._pending_tool_outputs, []
        for call_id, output in pending:
            await self.connection.conversation.item.create(
                item={"type": "function_call_output", "call_id": call_id, "output": output}
            )
        await self.connection.response.create()

    def _clear_playback(self) -> None:
        while True:
            try:
                self._playback_queue.get_nowait()
                self._playback_queue.task_done()
            except asyncio.QueueEmpty:
                break
        audio = getattr(self.robot.media, "audio", None)
        if audio is not None and hasattr(audio, "clear_player"):
            try:
                audio.clear_player()
            except Exception:
                logger.debug("clear_player failed", exc_info=True)
