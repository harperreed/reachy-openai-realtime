# ABOUTME: Guards that the shipped dashboard static files carry the wake-word
# ABOUTME: panel markup, presence rendering, and English i18n rows.
from pathlib import Path

import reachy_openai_realtime

STATIC = Path(reachy_openai_realtime.__file__).resolve().parent / "static"


def test_index_html_has_wake_panel() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'class="wake-panel"' in html
    assert 'id="wake-state"' in html
    assert 'id="wake-button"' in html
    assert 'id="sleep-button"' in html
    assert 'data-i18n="wake_title"' in html


def test_main_js_wires_presence_and_endpoints() -> None:
    js = (STATIC / "main.js").read_text(encoding="utf-8")
    assert "status.presence" in js
    assert "/api/presence/wake" in js
    assert "/api/presence/sleep" in js


def test_i18n_has_english_wake_rows() -> None:
    js = (STATIC / "i18n.js").read_text(encoding="utf-8")
    for key in ("wake_title", "presence_sleeping", "wake_disabled", "wake_now", "sleep_now"):
        assert key in js
