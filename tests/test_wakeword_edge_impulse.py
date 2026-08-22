import textwrap

import numpy as np

from reachy_openai_realtime.wakeword.base import WakeWordDetection
from reachy_openai_realtime.wakeword.edge_impulse import EdgeImpulseWakeWordDetector
from reachy_openai_realtime.wakeword.eim_runner import EimRunner

FAKE_RUNNER = textwrap.dedent(
    '''
    import json, os, socket, sys
    sock_path = sys.argv[1]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock_path)
    s.listen(1)
    conn, _ = s.accept()
    def read_msg():
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            buf += chunk
            try:
                return json.loads(buf)  # fake: one whole JSON per send
            except json.JSONDecodeError:
                continue
    def send(obj):
        conn.sendall(json.dumps(obj).encode() + b"\\x00")
    while True:
        msg = read_msg()
        if msg is None:
            break
        if "hello" in msg:
            send({"id": msg["id"], "model_parameters": {
                "frequency": 24000, "input_features_count": 48000,
                "slice_size": 12000, "labels": ["hey_reachy", "noise", "other"]},
                "project": {"owner": "test", "name": "fake"}})
        elif "classify" in msg:
            n = len(msg["classify"])
            score = 0.9 if any(msg["classify"]) else 0.01
            send({"id": msg["id"], "result": {"classification": {
                "hey_reachy": score, "noise": 0.05, "other": 0.05}},
                "timing": {"dsp": 1, "classification": 2}})
    '''
).strip()


def _write_runner(tmp_path):
    runner = tmp_path / "fake_runner.eim"
    runner.write_text("#!/usr/bin/env python3\n" + FAKE_RUNNER)
    runner.chmod(0o644)  # EimRunner.start() must chmod +x itself
    return str(runner)


def test_start_returns_model_parameters(tmp_path):
    runner = EimRunner(_write_runner(tmp_path))
    try:
        params = runner.start()
    finally:
        runner.close()
    assert params.frequency == 24_000
    assert params.input_features_count == 48_000
    assert params.slice_size == 12_000
    assert params.labels == ["hey_reachy", "noise", "other"]


def test_classify_returns_label_scores(tmp_path):
    runner = EimRunner(_write_runner(tmp_path))
    try:
        runner.start()
        loud = runner.classify([10_000] * 48_000)
        quiet = runner.classify([0] * 48_000)
    finally:
        runner.close()
    assert loud["hey_reachy"] == 0.9
    assert quiet["hey_reachy"] == 0.01


def test_close_is_idempotent(tmp_path):
    runner = EimRunner(_write_runner(tmp_path))
    runner.start()
    runner.close()
    runner.close()  # must not raise


def test_wake_word_detection_is_frozen_value():
    detection = WakeWordDetection(phrase="hey reachy", score=0.91, detected_at=123.0)
    assert (detection.phrase, detection.score, detection.detected_at) == ("hey reachy", 0.91, 123.0)
    import dataclasses
    assert dataclasses.is_dataclass(detection) and detection.__dataclass_params__.frozen


def test_detector_fires_when_score_crosses_threshold(tmp_path):
    detector = EdgeImpulseWakeWordDetector(_write_runner(tmp_path), threshold=0.70)
    detector.start()
    try:
        assert detector.required_sample_rate == 24_000
        # < one window of silence: no classification yet
        silence = np.zeros(24_000, dtype=np.int16).tobytes()
        assert detector.process(silence) is None
        # push enough loud audio to fill a 48k window and advance a slice
        loud = np.full(48_000, 8_000, dtype=np.int16).tobytes()
        detection = detector.process(loud)
    finally:
        detector.close()
    assert detection is not None
    assert detection.phrase == "hey reachy"
    assert detection.score >= 0.70


def test_detector_returns_none_below_threshold(tmp_path):
    detector = EdgeImpulseWakeWordDetector(_write_runner(tmp_path), threshold=0.70)
    detector.start()
    try:
        quiet = np.zeros(60_000, dtype=np.int16).tobytes()  # score 0.01 from fake runner
        result = detector.process(quiet)
    finally:
        detector.close()
    assert result is None
