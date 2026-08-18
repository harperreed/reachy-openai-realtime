# Documentation Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce application-log key redaction, enable private vulnerability reporting, and make the user-facing documentation match current behavior.

**Architecture:** Reuse `observability.events.redact_secrets` in a formatter that redacts the final rendered application-log line. Keep product behavior unchanged elsewhere; narrow claims about localization, state transitions, legacy key loading, release gates, and hardware outcomes. Enable GitHub's repository-hosted private reporting through its documented REST endpoint.

**Tech Stack:** Python 3.10+, standard-library `logging`, pytest, Ruff, GitHub REST API through `gh`, Markdown.

## Global Constraints

- Keep the change within the approved design at `docs/superpowers/specs/2026-08-18-documentation-hardening-design.md`.
- Use the existing `redact_secrets(text: str) -> str` as the single redaction rule.
- Preserve process-environment and legacy `.env` loading behavior.
- Do not add dependencies, an API reference, a release workflow, full runtime localization, or a new migration path.
- Use TDD for the application-log change: observe the regression test fail before implementation and pass afterward.
- The only external state change is enabling GitHub private vulnerability reporting for `tinjyuu/reachy-openai-realtime`.

---

## File map

- `reachy_openai_realtime/main.py`: format and attach the redacted `application.log` handler.
- `tests/test_main_logging.py`: real-file regression coverage for message and exception redaction.
- `.gitignore`: ignore common extensionless SSH private-key filenames.
- `SECURITY.md`: document the enabled private report route and exact credential safeguards.
- `README.md`: correct localization, key-path, FSM, and log-redaction claims.
- `docs/WIRELESS.md`: state image prerequisites and distinguish code behavior from manual hardware checks.

### Task 1: Redact formatted application logs

**Files:**
- Modify: `tests/test_main_logging.py`
- Modify: `reachy_openai_realtime/main.py`

**Interfaces:**
- Consumes: `reachy_openai_realtime.observability.events.redact_secrets(text: str) -> str`
- Produces: `RedactingFormatter(logging.Formatter)` and the unchanged
  `attach_file_logging() -> RotatingFileHandler` interface

- [ ] **Step 1: Add the failing regression test**

Append this test to `tests/test_main_logging.py`:

```python
def test_attach_file_logging_redacts_message_keys(monkeypatch, tmp_path) -> None:
    fresh = tmp_path / "apps" / "reachy_openai_realtime"
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(fresh))
    message_key = "sk-test-message-abcdefghijklmnop"

    handler = attach_file_logging()
    logger = logging.getLogger("application-log-redaction-test")
    logger.setLevel(logging.INFO)
    try:
        logger.error("message key: %s", message_key)
        handler.flush()
        contents = log_path().read_text(encoding="utf-8")
        assert message_key not in contents
        assert "sk-***" in contents
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_attach_file_logging_redacts_exception_keys(monkeypatch, tmp_path) -> None:
    fresh = tmp_path / "apps" / "reachy_openai_realtime"
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(fresh))
    exception_key = "sk-test-exception-abcdefghijklmnop"

    handler = attach_file_logging()
    logger = logging.getLogger("application-log-redaction-test")
    logger.setLevel(logging.INFO)
    try:
        try:
            raise RuntimeError(f"exception key: {exception_key}")
        except RuntimeError:
            logger.exception("request failed")
        handler.flush()
        contents = log_path().read_text(encoding="utf-8")
        assert exception_key not in contents
        assert "sk-***" in contents
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
```

- [ ] **Step 2: Run the test and verify the red state**

Run:

```bash
uv run pytest \
  tests/test_main_logging.py::test_attach_file_logging_redacts_message_keys \
  tests/test_main_logging.py::test_attach_file_logging_redacts_exception_keys \
  -v
```

Expected: both tests FAIL because each original key appears in `application.log`.

- [ ] **Step 3: Add the minimal formatter**

Change the event import in `reachy_openai_realtime/main.py`:

```python
from .observability.events import EventRecorder, redact_secrets
```

Add the formatter before `attach_file_logging`:

```python
class RedactingFormatter(logging.Formatter):
    """Redact OpenAI-style keys after a log record is fully formatted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))
```

Use it in `attach_file_logging()`:

```python
handler.setFormatter(
    RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
)
```

- [ ] **Step 4: Run focused tests and Ruff**

Run:

```bash
uv run pytest tests/test_main_logging.py tests/test_observability_events.py -v
uv run ruff check reachy_openai_realtime/main.py tests/test_main_logging.py
```

Expected: both commands exit 0; the three application-log tests and event-redaction tests pass.

- [ ] **Step 5: Commit the redaction change**

```bash
git add reachy_openai_realtime/main.py tests/test_main_logging.py
git commit -m "fix: redact API keys from application logs"
```

### Task 2: Enable and document private vulnerability reporting

**Files:**
- Modify: `.gitignore`
- Modify: `SECURITY.md`

**Interfaces:**
- Consumes: GitHub REST endpoint `/repos/{owner}/{repo}/private-vulnerability-reporting`
- Produces: an enabled repository setting and the public documentation link
  `https://github.com/tinjyuu/reachy-openai-realtime/security/advisories/new`

- [ ] **Step 1: Confirm the current hosted setting**

Run:

```bash
gh api repos/tinjyuu/reachy-openai-realtime/private-vulnerability-reporting
```

Expected before mutation: `{"enabled":false}`. If it is already true, record that and do not issue the PUT.

- [ ] **Step 2: Enable private vulnerability reporting**

Run only when Step 1 reports false:

```bash
gh api --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  repos/tinjyuu/reachy-openai-realtime/private-vulnerability-reporting
```

Expected: HTTP 204 with no response body. This is the approved external security-setting change.

- [ ] **Step 3: Verify the hosted setting**

Run:

```bash
gh api repos/tinjyuu/reachy-openai-realtime/private-vulnerability-reporting
curl -L --max-time 20 -sS -o /dev/null -w '%{http_code}\n' \
  https://github.com/tinjyuu/reachy-openai-realtime/security/advisories/new
```

Expected: `{"enabled":true}` and HTTP 200.

- [ ] **Step 4: Expand exact Git ignore rules**

Add below the existing `*.key` rule in `.gitignore`:

```gitignore
id_rsa
id_dsa
id_ecdsa
id_ed25519
```

Verify without creating key files:

```bash
git check-ignore --no-index id_rsa id_dsa id_ecdsa id_ed25519
```

Expected: all four names are printed and the command exits 0.

- [ ] **Step 5: Replace `SECURITY.md` with exact policy text**

Keep the existing headings and use this content:

```markdown
# Security policy

## Reporting a vulnerability

Do not disclose vulnerabilities or exposed credentials in a public issue. Report them through
[GitHub private vulnerability reporting](https://github.com/tinjyuu/reachy-openai-realtime/security/advisories/new).

If an OpenAI API key may have been exposed, revoke it in the OpenAI dashboard before reporting the related
code issue. Do not include the key in the report.

## Credential handling

- The settings UI saves API keys in the robot's persistent user configuration directory. Directories and
  key files created or updated by the app use modes `0700` and `0600` respectively.
- Temporary development sessions may use `OPENAI_API_KEY`, and existing legacy `.env` files may be loaded
  or migrated. See `docs/WIRELESS.md` for the supported setup paths.
- Settings APIs report only whether a key exists; they never return its value.
- Git ignores `.env`, `.env.*`, `*.pem`, `*.key`, `secrets.*`, and common extensionless SSH private-key
  filenames. `.env.example` remains allowed.
- `scripts/check_secrets.py` runs on demand and in CI for pushes and pull requests.
```

- [ ] **Step 6: Run security checks**

Run:

```bash
uv run python scripts/check_secrets.py
git diff --check
```

Expected: the secret scan passes and the diff has no whitespace errors.

- [ ] **Step 7: Commit repository security documentation**

```bash
git add .gitignore SECURITY.md
git commit -m "docs: document enforced credential safeguards"
```

### Task 3: Correct README and Wireless behavior claims

**Files:**
- Modify: `README.md`
- Modify: `docs/WIRELESS.md`

**Interfaces:**
- Consumes: current behavior in `config.py`, `session/fsm.py`, `settings.py`, `main.py`, and `static/i18n.js`
- Produces: user-facing instructions that distinguish guarantees from manual acceptance checks

- [ ] **Step 1: Narrow localization claims in `README.md`**

Replace the feature bullet with:

```markdown
- English by default, with static UI text and keyed status/activity entries following the selected language
```

Replace the final paragraph of “Supported languages” with:

```markdown
The language selection is persisted on the robot. Static UI text and translated status/activity entries
change immediately; raw diagnostic values may remain in their source language. The spoken conversation
changes from the next response. The app supplies the selected language through Realtime session and
response instructions, following OpenAI's documented session configuration flow.
```

- [ ] **Step 2: Document the actual configuration paths in `README.md`**

After the configuration table, add:

```markdown
`REACHY_OPENAI_REALTIME_CONFIG_DIR` overrides the persistent app configuration directory for custom or test
installs. Otherwise the app uses `$XDG_CONFIG_HOME/reachy-mini/apps/reachy_openai_realtime`, falling back to
`~/.config/reachy-mini/apps/reachy_openai_realtime` when `XDG_CONFIG_HOME` is unset.
```

Replace the API-key permission paragraph with:

```markdown
The settings UI creates or updates the directory with mode `0700` and the file with mode `0600`; settings
and diagnostics APIs never return the saved value. For temporary development, the app can instead read
`OPENAI_API_KEY` from the process environment. Existing legacy `.env` files may be loaded or migrated from
the former `reachy_japanese_realtime` app.
```

- [ ] **Step 3: Replace the FSM description in `README.md`**

Use this text:

```markdown
The common conversation path is `DISCONNECTED` → `CONNECTING` → `INITIALIZING` → `LISTENING` →
`USER_SPEAKING` → `WAITING_RESPONSE` → `ASSISTANT_SPEAKING` → `LISTENING`. Barge-in moves from
`ASSISTANT_SPEAKING` through `INTERRUPTING` to `USER_SPEAKING` or `LISTENING`.

`TOOL_EXECUTION` branches from `WAITING_RESPONSE` or `ASSISTANT_SPEAKING`, then returns to
`WAITING_RESPONSE` or `LISTENING`. `RECOVERING` and `STOPPING` can be entered from any state;
`STOPPING` ends at `DISCONNECTED`.
```

- [ ] **Step 4: Correct the log privacy text in `README.md`**

Replace the observability claim with:

```markdown
Both files rotate at 2–5 MB and keep two generations. OpenAI-style API keys are redacted from both files,
and the app does not write raw microphone audio. Logs can contain diagnostic errors and assistant
transcripts; review them before sharing.
```

- [ ] **Step 5: Add Wireless prerequisites and qualify acceptance checks**

After the `docs/WIRELESS.md` title, add:

```markdown
The commands below assume a Reachy Mini Wireless image with the `pollen` account, mDNS hostname
`reachy-mini.local`, and app environment at `/venvs/apps_venv`. Confirm those values for the installed image
before continuing.
```

In section 3, replace “The full UI changes immediately” with “Static UI text and translated status entries
change immediately.” Before the numbered hardware list, add:

```markdown
Run these as manual acceptance checks on the robot. Network, model, microphone, speaker, and room behavior
cannot be guaranteed by the package alone.
```

Replace checklist items 4 and 6 with:

```markdown
4. After 800 ms of silence, the app commits the input and requests a response; successful playback confirms
   the network, model, and speaker path.
6. Reachy uses subtle head and antenna motion while speaking. Try prompts such as “nod”, “look right”, or
   “act surprised”; when the model selects a motion tool, Reachy executes a gentle bounded motion. A selected
   look direction remains the base pose for speaking, listening, and idle motion until another `look` request
   changes it.
```

- [ ] **Step 6: Review only the changed claims against source**

Run:

```bash
rg -n "entire UI|full management UI|reachable from any active state|all values are redacted|stored only|before release" \
  README.md SECURITY.md docs/WIRELESS.md
rg -n "RedactingFormatter|LEGAL_TRANSITIONS|CONFIG_DIR_ENV|private-vulnerability-reporting" \
  reachy_openai_realtime tests README.md SECURITY.md docs/WIRELESS.md
git diff --check
```

Expected: the first search returns no stale claims; the second shows each replacement's implementation or
hosted-setting reference; the diff check exits 0.

- [ ] **Step 7: Commit corrected user documentation**

```bash
git add README.md docs/WIRELESS.md
git commit -m "docs: align user guides with runtime behavior"
```

### Task 4: Run complete verification and close the audit

**Files:**
- Modify only if review finds a factual error: `README.md`, `SECURITY.md`, `docs/WIRELESS.md`

**Interfaces:**
- Consumes: all earlier task outputs
- Produces: a verified, clean documentation-hardening branch

- [ ] **Step 1: Run the canonical repository checks**

```bash
uv run python scripts/check_secrets.py
uv run ruff check .
uv run pytest
uv run reachy-mini-app-assistant check .
```

Expected: secret scan passes, Ruff reports no errors, all tests pass, and the Reachy app check reports that
the app passed all checks.

- [ ] **Step 2: Recheck hosted state and documentation links**

```bash
gh api repos/tinjyuu/reachy-openai-realtime/private-vulnerability-reporting
curl -L --max-time 20 -sS -o /dev/null -w '%{http_code} %{url_effective}\n' \
  https://github.com/tinjyuu/reachy-openai-realtime/security/advisories/new
```

Expected: `{"enabled":true}` and HTTP 200 at the advisory form.

- [ ] **Step 3: Perform a focused re-audit**

Check each of the eight false-claim rows in `docs/audits/AUDIT_REPORT_2026-08-18.md` against the changed docs
and code. Confirm:

```text
README.md:26   localization scope is qualified
README.md:50   language-change scope is qualified
README.md:113  FSM branches match LEGAL_TRANSITIONS
README.md:160  both log sinks now use the shared key-redaction rule
SECURITY.md:13 save, environment, and legacy paths are distinct
SECURITY.md:14 permissions apply to files/directories created or updated by the app
SECURITY.md:16 exact ignored patterns are named
SECURITY.md:17 CI triggers are named without claiming a release gate
```

- [ ] **Step 4: Run final repository-state checks**

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -6
```

Expected: no whitespace errors, no uncommitted files, and the task commits appear on
`docs/documentation-audit-2026-08-18`.
