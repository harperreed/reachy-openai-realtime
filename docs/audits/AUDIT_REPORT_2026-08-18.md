# Documentation Audit Report

Generated: 2026-08-18 | Commit: `22be660`

## Executive summary

The user-facing documentation is broadly accurate, but eight claims overstate what the code guarantees.
The highest-risk drift concerns API-key handling: structured events redact OpenAI-style keys, while
`application.log` has no redaction filter. The security policy also points to GitHub private vulnerability
reporting only “when available”; the repository setting was disabled when checked on 2026-08-18, and the
policy gives no private fallback.

| Metric | Count |
|---|---:|
| Documents scanned | 3 |
| Atomic claims checked | 163 |
| Verified TRUE | 141 (86.5%) |
| **Verified FALSE** | **8 (4.9%)** |
| Needs hardware, service, or policy review | 14 (8.6%) |

Compound statements were split into atomic claims. Normative advice and marketing language were not
counted. Historical plans, `docs/production-hardening-spec.md`, `plan.md`, prior audits, and `gotchas.md`
were excluded because they are not user-facing current-state documentation.

## False claims requiring fixes

### `README.md`

| Severity | Line | Claim | Repository reality | Suggested fix |
|---|---:|---|---|---|
| Medium | 26 | “the entire UI and runtime log” follow the selected language | Static labels and keyed events are translated, but raw errors, motion summaries, and some runtime values pass through unchanged (`static/main.js:217-221`, `runtime_status.py:140-145`, `static/i18n.js:188-209`). | Say that static UI text and keyed status/activity entries follow the selected language. |
| Medium | 50 | Language changes “the full management UI immediately—including status details and activity logs” | Language switching is immediate, but the same unkeyed runtime strings remain untranslated. | Narrow “full” to the translated UI and keyed status/activity entries. |
| Medium | 113 | `TOOL_EXECUTION` and `RECOVERING` are reachable from any active state; `ASSISTANT_SPEAKING ↔ INTERRUPTING` | `RECOVERING` is broadly reachable. `TOOL_EXECUTION` starts only from `WAITING_RESPONSE` or `ASSISTANT_SPEAKING`, and `INTERRUPTING` returns to `USER_SPEAKING` or `LISTENING` (`session/fsm.py:28-47`). | Describe the normal path, then state the recovery, stopping, and tool branches separately. |
| High | 160 | Both log files redact “all values” before writing | Recursive redaction applies to `EventRecorder` fields (`observability/events.py:24-31,55-75`). `application.log` uses a plain `RotatingFileHandler` with no filter (`main.py:42-52`). | Add and test an application-log redaction filter, or limit the claim to `events.jsonl`. |

The related line-160 claim that `application.log` can never contain an API key remains unproved. The app
does not intentionally log the key, but the root handler captures unfiltered application, dependency, and
exception records.

### `SECURITY.md`

| Severity | Line | Claim | Repository reality | Suggested fix |
|---|---:|---|---|---|
| Medium | 13 | API keys are stored “only” in the persistent user configuration directory | The app also accepts `OPENAI_API_KEY` from the process environment and loads a legacy instance `.env` (`settings.py:75-84`; `tests/test_settings.py:45-52`). | Describe where the settings UI saves keys, then list the environment and legacy load paths. |
| Medium | 14 | Every loaded key file has mode `0600` | App-created and migrated files get `0600`, but existing persistent and legacy files are loaded without mode normalization (`settings.py:64-84,97-116`). | Qualify the claim, or normalize and test permissions during every load. |
| Medium | 14 | Every loaded key directory has mode `0700` | App-created or touched directories get `0700`; existing and legacy directories are not normalized on load. | Qualify the claim, or enforce the directory mode during every load. |
| Low | 16 | Private-key files are ignored by Git | `.gitignore` covers `*.pem` and `*.key`, but not common extensionless names such as `id_rsa` and `id_ed25519` (`.gitignore:4-9`). | List the exact ignored patterns, or add the intended filename patterns. |

## Human review queue

### Wireless hardware and deployment

`docs/WIRELESS.md` has no false code-level claims. Nine items need checks on a real supported Wireless
image because the repository cannot prove the robot account, filesystem, dashboard, network, model choice,
or physical output:

- [ ] Lines 8, 14, and 15: verify `pollen@reachy-mini.local`, SSH access, writable `/tmp`, and
  `/venvs/apps_venv/bin/pip` on every supported image.
- [ ] Lines 22 and 34: verify dashboard discovery/settings routing and
  `/venvs/apps_venv/bin/python` on the supported SDK/image.
- [ ] Lines 39, 41, and 42: run the greeting, microphone-meter, and audible-response hardware checks.
  The code proves the greeting request, dBFS update, 800 ms silence threshold, commit, and response request;
  it cannot prove network, model, speaker, or room behavior.
- [ ] Line 44: verify the sample natural-language requests produce the intended model tool calls. The code
  proves that `nod`, `look`, and `express` exist and are bounded, not that the model must select them.

### Security and hosted services

- [ ] `README.md:160`: either test application-log key redaction or replace the absolute “never” claim.
- [ ] `SECURITY.md:6`: GitHub private vulnerability reporting was disabled when checked through the GitHub
  API on 2026-08-18. Enable it and link the form, or provide another private report channel.
- [ ] `SECURITY.md:8-9`: verify the current OpenAI key-revocation path and link the official instructions.
- [ ] `SECURITY.md:16`: replace the undefined phrase “common secret files” with exact patterns or classes.
- [ ] `SECURITY.md:17`: the secret scan runs on pushes and pull requests, but no committed release workflow
  enforces “before release” (`.github/workflows/ci.yml:3-5,20-21`). Confirm the release process or narrow
  the wording to a manual pre-publish step.

## Pass-two pattern expansion

| Pattern | Count | Root cause |
|---|---:|---|
| Absolute scope (`entire`, `full`, `all`, `only`, `ever`) | 7 claims plus 1 review item | A partial guarantee was documented as universal. |
| Hardware or hosted-service assumptions | 9 review items | Repository evidence stops at the app boundary. |
| Security controls described more broadly than enforced | 5 claims/review items | Event redaction, file modes, ignore rules, and CI triggers each cover a narrower path. |

Expansion searches found no dead script, service, timer, path, or command references, and no incorrect
documented defaults or environment-variable names. The only tracked script is `scripts/check_secrets.py`,
and both README invocations resolve. The repository contains no service or timer files and the docs name
none.

## Inventory gaps

- The README configuration table omits `REACHY_OPENAI_REALTIME_CONFIG_DIR`,
  `REACHY_JAPANESE_REALTIME_CONFIG_DIR`, and `XDG_CONFIG_HOME` (`settings.py:13-29`). The table does not
  claim to be exhaustive; decide whether the current config-dir override is a supported user contract.
- The app implements eight HTTP routes (`main.py:88-234`). The README documents `/api/status` and
  `/api/diagnostics`; the other six serve the management UI. Either label those six internal or document
  them if external callers may depend on them.
- `SECURITY.md` has no supported-version policy, direct private report link, or fallback private contact.
- The repository has no committed dependency vulnerability scanner or automated dependency updater. CI
  runs the custom secret scan, a locked install, Ruff, and pytest; it does not run dependency audit tooling.
- The custom scanner checks current working-tree files for OpenAI keys, private-key headers, and GitHub
  tokens (`scripts/check_secrets.py:21-53`). It does not scan Git history despite CI fetching full history.

## Checks that passed

- All documented repository-relative file links and script paths resolve.
- Both external links in the README returned HTTP 200 on 2026-08-18.
- The documented development command `uv run reachy-mini-app-assistant check .` completed successfully.
- Defaults for model, voice, and language match `AppConfig` (`config.py:80-98`).
- Watchdog deadlines, reconnect delays, mic recovery timings, playback thresholds, and log rotation sizes
  match their implementations.
- [OpenAI's current Realtime guide](https://developers.openai.com/api/docs/guides/realtime-conversations)
  confirms the 60-minute maximum session duration and recommends `marin` or `cedar` for best voice quality.

## Recommended order

1. Fix or narrow the `application.log` redaction claim.
2. Enable a private vulnerability-report path and put a direct link in `SECURITY.md`.
3. Replace absolute key-storage and permission language with the actual save/load rules.
4. Correct the FSM description and translation scope.
5. Run the Wireless hardware review queue on the supported image and record the tested image/SDK version.
