"""Fail when repository files appear to contain credentials or private keys."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
ENV_ALLOWLIST = {".env.example"}
PATTERNS = {
    "OpenAI API key": re.compile(r"sk-(?!test-)[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
}


def candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


def main() -> int:
    findings: list[tuple[Path, int, str]] = []
    for path in candidate_files():
        relative = path.relative_to(ROOT)
        if path.name.startswith(".env") and path.name not in ENV_ALLOWLIST:
            findings.append((relative, 0, "environment file"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((relative, line_number, rule))

    if findings:
        print("Secret scan failed. Potential credentials were found:", file=sys.stderr)
        for path, line_number, rule in findings:
            location = f"{path}:{line_number}" if line_number else str(path)
            print(f"- {location} ({rule})", file=sys.stderr)
        return 1
    print("Secret scan passed: no credentials detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
