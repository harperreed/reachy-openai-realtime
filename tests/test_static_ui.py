# ABOUTME: Guards that the shipped dashboard static files carry the wake-word
# ABOUTME: panel markup, presence rendering, and English i18n rows.
import json
import subprocess
from pathlib import Path

import pytest

import reachy_openai_realtime
from reachy_openai_realtime.presence.states import PresenceState
from reachy_openai_realtime.runtime_status import RuntimeStatus

STATIC = Path(reachy_openai_realtime.__file__).resolve().parent / "static"
NODE = Path("/opt/homebrew/bin/node")


def _safety_snapshot(*, wake_mode: bool) -> dict[str, object]:
    status = RuntimeStatus()
    if wake_mode:
        status.set_presence(PresenceState.BOOTING, PresenceState.SLEEPING, "boot_complete")
    else:
        status.set_phase("safety_sleep", "restart required", connected=False)
    status.set_wake_latch(True, "noise_bail")
    return status.snapshot()


def _classify_safety_status(payload: dict[str, object]) -> str:
    script = (
        "const { classifySafetySleep } = require(process.argv[1]);"
        "process.stdout.write(classifySafetySleep(JSON.parse(process.argv[2])) || '');"
    )
    result = subprocess.run(
        [str(NODE), "-e", script, str(STATIC / "status.js"), json.dumps(payload)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_index_html_has_wake_panel() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'class="wake-panel"' in html
    assert 'id="wake-state"' in html
    assert 'id="wake-button"' in html
    assert 'id="sleep-button"' in html
    assert 'data-i18n="wake_title"' in html
    assert '<script src="/static/status.js"></script>' in html


def test_main_js_wires_presence_and_endpoints() -> None:
    js = (STATIC / "main.js").read_text(encoding="utf-8")
    assert "status.presence" in js
    assert "statusClassifier.classifySafetySleep(status)" in js
    assert "/api/presence/wake" in js
    assert "/api/presence/sleep" in js


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_safety_snapshot(wake_mode=True), "wake"),
        (_safety_snapshot(wake_mode=False), "always_on"),
        (
            {
                **_safety_snapshot(wake_mode=True),
                "connected": True,
            },
            "",
        ),
        (
            {
                **_safety_snapshot(wake_mode=True),
                "last_error": "model failed",
            },
            "",
        ),
        (
            {
                **_safety_snapshot(wake_mode=True),
                "wake_latched": "true",
            },
            "",
        ),
        (
            {
                **_safety_snapshot(wake_mode=False),
                "wake_latch_reason": "other",
            },
            "",
        ),
    ],
)
def test_dashboard_safety_classifier(payload: dict[str, object], expected: str) -> None:
    assert NODE.exists(), "Node is required for dashboard classifier tests"
    assert _classify_safety_status(payload) == expected


def test_wake_button_stays_enabled_for_sleeping_safety_latch() -> None:
    js = (STATIC / "main.js").read_text(encoding="utf-8")
    assert 'document.getElementById("wake-button").disabled = !(presence === "sleeping" || presence === "error");' in js


def test_i18n_has_english_wake_rows() -> None:
    js = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert 'presence_latched: ["Safety sleep · use Wake now"]' in js
    assert 'presence_latched_always_on: ["Safety sleep · restart app to rearm"]' in js
    for key in (
        "wake_title",
        "presence_sleeping",
        "presence_latched",
        "wake_disabled",
        "wake_now",
        "sleep_now",
    ):
        assert key in js
