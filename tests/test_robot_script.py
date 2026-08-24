# ABOUTME: Tests the lifecycle script's app-readiness parser as a real subprocess.
# ABOUTME: Covers connected, wake-armed, error, incomplete, and malformed status payloads.
import json
import os
import subprocess
from pathlib import Path

import pytest

from reachy_openai_realtime.presence.states import PresenceState
from reachy_openai_realtime.runtime_status import RuntimeStatus

READY_STATE = Path(__file__).parents[1] / "scripts" / "robot-ready-state"


def _safety_snapshot(*, wake_mode: bool) -> dict[str, object]:
    status = RuntimeStatus()
    if wake_mode:
        status.set_presence(PresenceState.BOOTING, PresenceState.SLEEPING, "boot_complete")
    else:
        status.set_phase(
            "safety_sleep",
            "Safety sleep is active; restart the app to rearm",
            connected=False,
            detail_key="detail_safety_sleep",
        )
    status.set_wake_latch(True, "noise_bail")
    return status.snapshot()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"connected": True, "presence": None, "last_error": None}, "connected"),
        (
            {
                "connected": False,
                "presence": "sleeping",
                "wake_latched": False,
                "wake_latch_reason": None,
                "last_error": None,
            },
            "sleeping",
        ),
        (
            {
                "connected": True,
                "presence": "sleeping",
                "wake_latched": True,
                "wake_latch_reason": "noise_bail",
                "last_error": None,
            },
            "connected",
        ),
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
    ("payload", "expected"),
    [
        (_safety_snapshot(wake_mode=True), "safety_wake"),
        (_safety_snapshot(wake_mode=False), "safety_always_on"),
    ],
)
def test_ready_state_reports_exact_safety_mode(
    payload: dict[str, object], expected: str
) -> None:
    result = subprocess.run(
        [str(READY_STATE)], input=json.dumps(payload), text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "payload",
    [
        {
            "connected": False,
            "presence": "sleeping",
            "wake_latched": "true",
            "wake_latch_reason": "noise_bail",
            "last_error": None,
        },
        {
            "connected": False,
            "presence": "sleeping",
            "wake_latched": 1,
            "wake_latch_reason": "noise_bail",
            "last_error": None,
        },
        {"connected": False, "presence": "sleeping", "wake_latched": False, "last_error": None},
        {
            "connected": False,
            "presence": "sleeping",
            "wake_latched": True,
            "wake_latch_reason": "other",
            "last_error": None,
        },
        {"connected": False, "presence": "sleeping", "wake_latched": True, "last_error": None},
        {
            "connected": False,
            "presence": "sleeping",
            "wake_latched": False,
            "wake_latch_reason": "noise_bail",
            "last_error": None,
        },
        {
            "connected": False,
            "presence": "sleeping",
            "wake_latched": False,
            "wake_latch_reason": None,
            "last_error": "model failed",
        },
        {
            "connected": False,
            "presence": "waking",
            "wake_latched": False,
            "wake_latch_reason": None,
            "last_error": None,
        },
        {
            "presence": "sleeping",
            "wake_latched": False,
            "wake_latch_reason": None,
            "last_error": None,
        },
    ],
)
def test_ready_state_rejects_malformed_or_unhealthy_status(payload: dict[str, object]) -> None:
    assert READY_STATE.exists(), "robot-ready-state parser is missing"
    result = subprocess.run(
        [str(READY_STATE)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""


def test_ready_state_rejects_invalid_json() -> None:
    result = subprocess.run(
        [str(READY_STATE)], input="not json", text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert result.stdout == ""


def test_status_reports_unavailable_when_dashboard_status_curl_fails(tmp_path: Path) -> None:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
case "$*" in
    *":8042/api/status"*) exit 22 ;;
    *":8000/api/daemon/status"*) printf '%s\\n200' '{"state":"running","backend_status":{"ready":true,"motor_control_mode":"enabled"}}' ;;
    *":8000/api/state/present_head_pose"*) printf '%s\\n200' '{"z":0}' ;;
    *":8000/api/apps/current-app-status"*) printf '%s\\n200' '{"info":{"name":"reachy_openai_realtime"},"state":"running"}' ;;
    *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        [str(READY_STATE.parent / "robot"), "-H", "test", "status"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert "realtime: status unavailable" in result.stdout
    assert "realtime: not connected" not in result.stdout
    assert '{"' not in result.stdout


@pytest.mark.parametrize(
    ("status", "leaked_value"),
    [
        ({"connected": True, "phase": {"nested": "phase-secret"}}, "phase-secret"),
        ({"connected": True, "phase": ["phase-secret"]}, "phase-secret"),
        ({"connected": True, "phase": 23}, "23"),
        ({"connected": True, "phase": True}, None),
        ({"connected": True, "phase": None}, None),
        ({"connected": True}, None),
    ],
)
def test_status_hides_malformed_connected_phase_values(
    tmp_path: Path, status: dict[str, object], leaked_value: str | None
) -> None:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
case "$*" in
    *":8042/api/status"*) printf '%s' "$DASHBOARD_STATUS" ;;
    *":8000/api/daemon/status"*) printf '%s\\n200' '{"state":"running","backend_status":{"ready":true,"motor_control_mode":"enabled"}}' ;;
    *":8000/api/state/present_head_pose"*) printf '%s\\n200' '{"z":0}' ;;
    *":8000/api/apps/current-app-status"*) printf '%s\\n200' '{"info":{"name":"reachy_openai_realtime"},"state":"running"}' ;;
    *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = os.environ | {
        "DASHBOARD_STATUS": json.dumps(status),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(READY_STATE.parent / "robot"), "-H", "test", "status"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert "realtime: connected phase=?" in result.stdout
    if leaked_value is not None:
        assert leaked_value not in result.stdout


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (_safety_snapshot(wake_mode=True), "realtime: safety sleep (manual wake required)"),
        (_safety_snapshot(wake_mode=False), "realtime: safety sleep (app restart required)"),
    ],
)
def test_status_distinguishes_safety_mode_from_runtime_snapshot(
    tmp_path: Path, status: dict[str, object], expected: str
) -> None:
    curl_log = tmp_path / "curl.log"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
case "$*" in
    *":8042/api/status"*) printf 'app-status\\n' >> "$CURL_LOG"; printf '%s' "$DASHBOARD_STATUS" ;;
    *":8000/api/daemon/status"*) printf '%s\\n200' '{"state":"running","backend_status":{"ready":true,"motor_control_mode":"enabled"}}' ;;
    *":8000/api/state/present_head_pose"*) printf '%s\\n200' '{"z":0}' ;;
    *":8000/api/apps/current-app-status"*) printf '%s\\n200' '{"info":{"name":"reachy_openai_realtime"},"state":"running"}' ;;
    *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    environment = os.environ | {
        "CURL_LOG": str(curl_log),
        "DASHBOARD_STATUS": json.dumps(status),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(READY_STATE.parent / "robot"), "-H", "test", "status"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert expected in result.stdout
    assert curl_log.read_text(encoding="utf-8").splitlines() == ["app-status"]
    assert "wake_latched" not in result.stdout
    assert "Safety sleep is active" not in result.stdout
