# ABOUTME: Tests the lifecycle script's app-readiness parser as a real subprocess.
# ABOUTME: Covers connected, wake-armed, error, incomplete, and malformed status payloads.
import json
import subprocess
from pathlib import Path

import pytest

READY_STATE = Path(__file__).parents[1] / "scripts" / "robot-ready-state"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"connected": True, "presence": None, "last_error": None}, "connected"),
        ({"connected": False, "presence": "sleeping", "last_error": None}, "sleeping"),
    ],
)
def test_ready_state_accepts_healthy_app_modes(payload: dict[str, object], expected: str) -> None:
    assert READY_STATE.exists(), "robot-ready-state parser is missing"
    result = subprocess.run(
        [str(READY_STATE)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "status_json",
    [
        json.dumps({"connected": False, "presence": "sleeping", "last_error": "model failed"}),
        json.dumps({"connected": False, "presence": "sleeping"}),
        json.dumps({"connected": False, "presence": "waking", "last_error": None}),
        "not json",
    ],
)
def test_ready_state_rejects_unhealthy_or_invalid_status(status_json: str) -> None:
    assert READY_STATE.exists(), "robot-ready-state parser is missing"
    result = subprocess.run(
        [str(READY_STATE)],
        input=status_json,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
