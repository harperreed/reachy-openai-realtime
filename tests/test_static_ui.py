# ABOUTME: Guards that the shipped dashboard static files carry the wake-word
# ABOUTME: panel markup, presence rendering, and English i18n rows.
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import reachy_openai_realtime
from reachy_openai_realtime.presence.states import PresenceState
from reachy_openai_realtime.runtime_status import RuntimeStatus

STATIC = Path(reachy_openai_realtime.__file__).resolve().parent / "static"


def _node_path() -> str:
    node = shutil.which("node")
    assert node is not None, "Node.js executable 'node' is required for dashboard classifier tests"
    return node


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
        [_node_path(), "-e", script, str(STATIC / "status.js"), json.dumps(payload)],
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


def test_sleeping_phase_is_known_and_translated_for_every_locale() -> None:
    main_js = (STATIC / "main.js").read_text(encoding="utf-8")
    i18n_js = (STATIC / "i18n.js").read_text(encoding="utf-8")

    assert '"assistant_speaking", "sleeping", "disconnected"' in main_js
    assert (
        'phase_sleeping: ["Sleeping", "スリープ中", "休眠中", "수면 중", '
        '"En reposo", "En veille", "Im Ruhemodus", "In pausa", "Em repouso"]'
    ) in i18n_js


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
    assert _classify_safety_status(payload) == expected


def test_dashboard_classifier_discovers_node_from_path(tmp_path: Path, monkeypatch) -> None:
    real_node = shutil.which("node")
    assert real_node is not None, "Node.js executable 'node' is required for dashboard classifier tests"
    marker = tmp_path / "node-used"
    wrapper = tmp_path / "node"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf 'used\\n' > {shlex.quote(str(marker))}\n"
        f"exec {shlex.quote(real_node)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert _classify_safety_status(_safety_snapshot(wake_mode=True)) == "wake"
    assert marker.read_text(encoding="utf-8") == "used\n"


def test_wake_button_stays_enabled_for_sleeping_safety_latch() -> None:
    js = (STATIC / "main.js").read_text(encoding="utf-8")
    assert 'document.getElementById("wake-button").disabled = !(presence === "sleeping" || presence === "error");' in js


def test_sleep_button_is_enabled_while_waking_or_awake() -> None:
    js = (STATIC / "main.js").read_text(encoding="utf-8")
    assert (
        'document.getElementById("sleep-button").disabled = '
        '!(presence === "waking" || presence === "awake");'
    ) in js


def test_sleeping_status_dot_does_not_pulse() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".status-dot.sleeping" in css
    assert ".status-dot.sleeping { background: #667383; box-shadow: none; animation: none; }" in css


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
