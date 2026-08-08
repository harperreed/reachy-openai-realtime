from __future__ import annotations

import asyncio
import logging
import os
import threading

from reachy_mini import ReachyMini, ReachyMiniApp

from .config import AppConfig
from .motion import MotionController
from .realtime import RealtimeRobotSession

logger = logging.getLogger(__name__)


class ReachyJapaneseRealtime(ReachyMiniApp):
    custom_app_url: str | None = None
    request_media_backend: str | None = "gstreamer_no_video"

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required")

        config = AppConfig.from_env()
        motion = MotionController(reachy_mini)
        session = RealtimeRobotSession(reachy_mini, motion, config)

        motion.start()
        reachy_mini.media.start_recording()
        reachy_mini.media.start_playing()
        try:
            asyncio.run(session.run(stop_event))
        finally:
            try:
                reachy_mini.media.stop_recording()
            finally:
                reachy_mini.media.stop_playing()
                motion.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = ReachyJapaneseRealtime()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        logger.info("Stopping")
        app.stop()

