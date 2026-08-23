# ABOUTME: Loads the single-source robot prompt (robot.md) and returns its labeled sections
# ABOUTME: with safe {placeholder} substitution. Edit robot.md to change how the robot talks.
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

_PROMPT_FILE = "robot.md"


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    """Parse robot.md into {heading: body}, keyed by its ``## `` section titles."""
    raw = files(__package__).joinpath(_PROMPT_FILE).read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    name: str | None = None
    body: list[str] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            if name is not None:
                sections[name] = "\n".join(body).strip()
            name = line[3:].strip()
            body = []
        elif name is not None:
            body.append(line)
    if name is not None:
        sections[name] = "\n".join(body).strip()
    return sections


def prompt_section(name: str, **fields: str) -> str:
    """Return the named robot.md section with each ``{field}`` replaced by its value.

    Substitution is plain text replacement, so a stray brace in the file cannot raise.
    Unknown section names raise KeyError.
    """
    try:
        text = _sections()[name]
    except KeyError as exc:
        raise KeyError(f"Unknown prompt section: {name!r}") from exc
    for key, value in fields.items():
        text = text.replace("{" + key + "}", value)
    return text
