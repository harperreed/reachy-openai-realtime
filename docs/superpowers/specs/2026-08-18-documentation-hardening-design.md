# Documentation Hardening Design

Date: 2026-08-18

## Goal

Resolve the actionable findings in `docs/audits/AUDIT_REPORT_2026-08-18.md`. Keep strong security claims
where a small enforcement change can support them, and narrow claims that would require unrelated product
work.

## Scope

### Enforce documented safeguards

1. Redact OpenAI-style API keys from every formatted line written to `application.log`, including normal
   messages and formatted exception output. Reuse the existing `redact_secrets` function so both log sinks
   share one redaction rule.
2. Enable GitHub private vulnerability reporting for the repository and link
   `https://github.com/tinjyuu/reachy-openai-realtime/security/advisories/new` from `SECURITY.md`.
3. Add `id_rsa`, `id_dsa`, `id_ecdsa`, and `id_ed25519` to `.gitignore` while keeping the existing secret
   scanner.

### Make documentation match current behavior

1. Replace claims that the entire UI and runtime log are translated. State that static UI text and keyed
   status/activity entries follow the selected language; raw diagnostic values may not.
2. Replace the compact FSM diagram with an accurate normal path and separate recovery, stopping, and tool
   branches.
3. Describe API-key storage by path:
   - the settings UI saves to the persistent app configuration directory with `0700` directory and `0600`
     file modes;
   - `OPENAI_API_KEY` remains available for temporary process configuration;
   - legacy `.env` files may be loaded or migrated.
4. Describe the secret scan as a local command and a push/pull-request CI check. Do not claim a release gate
   that the repository does not implement.
5. Add Wireless image prerequisites and mark network, model, microphone, speaker, and natural-language tool
   outcomes as manual acceptance checks.

## Implementation

### Application-log redaction

Add a small `logging.Formatter` subclass in `reachy_openai_realtime/main.py`. Its `format()` method will run
the fully formatted record through `redact_secrets`. Applying redaction after standard formatting covers
message arguments and exception text without mutating the shared `LogRecord` seen by other handlers.
`attach_file_logging()` will use this formatter.

Tests will create an isolated log directory, write a normal message and an exception containing test-format
OpenAI keys, flush the handler, and assert that the file contains `sk-***` but none of the original keys.
The test will use the real logging path and handler; it will not mock logging behavior.

### Repository security settings

Use GitHub's repository API to enable private vulnerability reporting, then confirm the setting through a
read-only API call. Update `SECURITY.md` with the direct advisory form. This is the only external state
change.

### Documentation edits

Edit `README.md`, `SECURITY.md`, and `docs/WIRELESS.md` in place. Preserve their current structure and tone.
Do not add an API reference, dependency scanner, release workflow, complete runtime localization, or legacy
configuration migration.

## Verification

Success requires:

1. A failing application-log redaction test before the formatter change, then a passing test afterward.
2. `uv run python scripts/check_secrets.py` passes.
3. `uv run ruff check .` passes.
4. `uv run pytest` passes.
5. `uv run reachy-mini-app-assistant check .` passes.
6. GitHub reports private vulnerability reporting as enabled and the documented advisory URL resolves.
7. A focused documentation re-audit finds none of the eight false claims from the 2026-08-18 report.

## Risks and limits

- Redaction recognizes the OpenAI-style key pattern implemented by `redact_secrets`; it is not a general
  data-loss prevention system.
- GitHub private reporting depends on repository-hosted state and can later be disabled outside Git.
- Wireless hardware outcomes remain manual checks until they are run on a named robot image and SDK
  version.
