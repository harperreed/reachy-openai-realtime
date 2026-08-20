# Security policy

## Reporting a vulnerability

Do not disclose vulnerabilities or exposed credentials in a public issue. GitHub private vulnerability
reporting is not currently configured for this repository. To request private coordination, open a public
issue that contains no vulnerability details, credentials, logs, or reproduction steps.

If an OpenAI API key may have been exposed, revoke it in the OpenAI dashboard before reporting the related
code issue. Do not include the key in the report.

## Credential handling

- The settings UI saves API keys in the robot's persistent user configuration directory. Directories and
  key files created or updated by the app use modes `0700` and `0600` respectively.
- Temporary development sessions may use `OPENAI_API_KEY`, and existing legacy `.env` files may be loaded
  or migrated. See `docs/WIRELESS.md` for the supported setup paths.
- Settings APIs report only whether a key exists; they never return its value.
- Git ignores `.env`, `.env.*`, `*.pem`, `*.key`, `secrets.*`, and the extensionless SSH private-key names
  `id_rsa`, `id_dsa`, `id_ecdsa`, and `id_ed25519`. `.env.example` remains allowed.
- `scripts/check_secrets.py` runs on demand and in CI for pushes and pull requests.
