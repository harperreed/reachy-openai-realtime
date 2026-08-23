# ABOUTME: Tests for the single-source prompt loader (prompts/robot.md) — section parsing
# ABOUTME: and safe {placeholder} substitution that config.py composes the robot prompts from.
import pytest

from reachy_openai_realtime.config import (
    greeting_instructions,
    response_instructions,
)
from reachy_openai_realtime.prompts import prompt_section


def test_prompt_section_returns_persona_with_language_substituted() -> None:
    text = prompt_section("Persona", language="English")
    assert "You are Reachy Mini" in text
    assert "configured conversation language is English" in text
    assert "{language}" not in text


def test_prompt_section_unknown_name_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        prompt_section("Nonexistent Section")


def test_response_instructions_thread_persona_into_every_turn() -> None:
    # The per-turn response.create instructions override the session default, so the
    # persona must ride along on each turn — otherwise editing it governs nothing.
    text = response_instructions("en")
    assert "You are Reachy Mini" in text  # persona threaded in
    assert "Reply only in natural English" in text  # per-turn reply guidance


def test_greeting_instructions_thread_persona_before_greeting() -> None:
    text = greeting_instructions("en")
    assert "You are Reachy Mini" in text  # persona threaded in
    assert "Hello. Talk to me." in text  # the canned greeting
