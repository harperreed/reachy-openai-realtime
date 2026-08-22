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


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


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
    memory_enabled: bool = True
    memory_write_policy: str = "agent"
    memory_wake_char_budget: int = 2000
    memory_nap_model: str = "gpt-5-mini"
    memory_nap_min_interval_s: int = 900
    memory_nap_chunk_size: int = 20
    memory_nap_branching: int = 8
    memory_nap_max_nodes: int = 10
    wake_enabled: bool = True
    wake_backend: str = "edge_impulse"
    wake_phrase: str = "hey reachy"
    wake_model_path: str = "models/hey-reachy-wake-word-detection-linux-aarch64.eim"
    wake_threshold: float = 0.70
    wake_debounce_seconds: float = 2.0
    wake_history_seconds: float = 4.0
    wake_preroll_ms: int = 400
    max_wake_buffer_seconds: int = 10
    wake_motion_enabled: bool = True
    boot_motion_enabled: bool = True

    def __post_init__(self) -> None:
        threshold = min(1.0, self.wake_threshold) if self.wake_threshold > 0.0 else 0.70
        object.__setattr__(self, "wake_threshold", threshold)
        object.__setattr__(self, "wake_debounce_seconds", _clamp(self.wake_debounce_seconds, 0.5, 10.0))
        object.__setattr__(self, "wake_history_seconds", _clamp(self.wake_history_seconds, 1.0, 10.0))
        object.__setattr__(self, "wake_preroll_ms", int(_clamp(self.wake_preroll_ms, 100, 1000)))
        object.__setattr__(self, "max_wake_buffer_seconds", int(_clamp(self.max_wake_buffer_seconds, 2, 30)))

    @classmethod
    def from_env(cls) -> AppConfig:
        raw_language = os.getenv(LANGUAGE_ENV, DEFAULT_LANGUAGE)
        try:
            language = language_option(raw_language).code
        except ValueError:
            language = DEFAULT_LANGUAGE
        memory_enabled = os.getenv("REACHY_OPENAI_REALTIME_MEMORY", "1").strip().lower() not in {
            "0",
            "false",
            "off",
        }
        raw_policy = os.getenv("REACHY_OPENAI_REALTIME_MEMORY_WRITE_POLICY", cls.memory_write_policy)
        raw_policy = raw_policy.strip().lower()
        memory_write_policy = raw_policy if raw_policy in {"agent", "explicit"} else cls.memory_write_policy
        memory_nap_model = os.getenv("REACHY_OPENAI_REALTIME_NAP_MODEL", cls.memory_nap_model)
        return cls(
            model=os.getenv("OPENAI_REALTIME_MODEL", cls.model),
            voice=os.getenv("OPENAI_REALTIME_VOICE", cls.voice),
            language=language,
            memory_enabled=memory_enabled,
            memory_write_policy=memory_write_policy,
            memory_nap_model=memory_nap_model,
        )
