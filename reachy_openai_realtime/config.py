from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageOption:
    code: str
    label: str
    english_name: str
    greeting: str


SUPPORTED_LANGUAGES: tuple[LanguageOption, ...] = (
    LanguageOption("en", "English", "English", "Hello. Talk to me."),
    LanguageOption("ja", "日本語", "Japanese", "こんにちは。話しかけてね。"),
    LanguageOption("zh", "中文", "Simplified Chinese", "你好。请和我说话。"),
    LanguageOption("ko", "한국어", "Korean", "안녕하세요. 말해 주세요."),
    LanguageOption("es", "Español", "Spanish", "Hola. Háblame."),
    LanguageOption("fr", "Français", "French", "Bonjour. Parle-moi."),
    LanguageOption("de", "Deutsch", "German", "Hallo. Sprich mit mir."),
    LanguageOption("it", "Italiano", "Italian", "Ciao. Parlami."),
    LanguageOption("pt", "Português", "Portuguese", "Olá. Fale comigo."),
)
DEFAULT_LANGUAGE = "en"
LANGUAGE_ENV = "REACHY_OPENAI_REALTIME_LANGUAGE"
_LANGUAGES_BY_CODE = {language.code: language for language in SUPPORTED_LANGUAGES}


def language_option(code: str) -> LanguageOption:
    try:
        return _LANGUAGES_BY_CODE[code.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported language: {code}") from exc


def language_choices() -> list[dict[str, str]]:
    return [
        {"code": language.code, "label": language.label}
        for language in SUPPORTED_LANGUAGES
    ]


def session_instructions(language_code: str) -> str:
    language = language_option(language_code)
    return f"""
You are Reachy Mini, a small and expressive robot.
The configured conversation language is {language.english_name}.
Always reply naturally in {language.english_name} only, using short sentences that are easy to hear.
If speech is unclear, do not guess; ask one brief clarifying question in {language.english_name}.
Confirm names, numbers, or letters naturally when needed.

Use robot motion only when it supports the conversation:
- Use nod for agreement or affirmation.
- Use shake_head for disagreement or negation.
- Use look to show attention to a person or topic.
- Use express for a subtle emotional accent while talking.
- Do not overuse motion tools or contradict the spoken response.
""".strip()


def recorded_moves_instructions(emotions: list[str], dances: list[str]) -> str:
    if not emotions and not dances:
        return ""
    recorded_tools = [name for name, names in (("play_emotion", emotions), ("play_dance", dances)) if names]
    lines: list[str] = [
        "- When asked to show an emotion, react expressively, or dance, prefer "
        + " / ".join(recorded_tools)
        + " over express — recorded moves are full performances lasting several seconds."
        " Announce the move with one short line; do not describe motion you did not perform.",
        (
            "- When asked what emotions or dances you can perform, say a handful of appealing"
            " examples from the lists below — never read out every name."
        ),
    ]
    if emotions:
        lines.append("- play_emotion accepts exactly these names: " + ", ".join(emotions))
    if dances:
        lines.append("- play_dance accepts exactly these names: " + ", ".join(dances))
    return "\n\n" + "\n".join(lines)


def response_instructions(language_code: str) -> str:
    language = language_option(language_code)
    return (
        f"Reply only in natural {language.english_name}. "
        "Use short, easy-to-hear sentences. Continue the conversation as Reachy Mini, "
        "and use a configured motion tool only when it genuinely helps the response."
    )


def greeting_instructions(language_code: str) -> str:
    language = language_option(language_code)
    return (
        f'Say exactly this greeting in {language.english_name}: "{language.greeting}" '
        "Do not add anything else and do not use tools."
    )


@dataclass(frozen=True)
class AppConfig:
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    language: str = DEFAULT_LANGUAGE
    input_rate: int = 24_000
    output_rate: int = 24_000

    @classmethod
    def from_env(cls) -> AppConfig:
        raw_language = os.getenv(LANGUAGE_ENV, DEFAULT_LANGUAGE)
        try:
            language = language_option(raw_language).code
        except ValueError:
            language = DEFAULT_LANGUAGE
        return cls(
            model=os.getenv("OPENAI_REALTIME_MODEL", cls.model),
            voice=os.getenv("OPENAI_REALTIME_VOICE", cls.voice),
            language=language,
        )
