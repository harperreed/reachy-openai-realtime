# Security policy

## Reporting a vulnerability

Please do not disclose vulnerabilities or exposed credentials in a public issue.
Use GitHub's private vulnerability reporting for this repository when available.

If an OpenAI API key may have been exposed, revoke it immediately in the OpenAI
dashboard before reporting the related code issue.

## Credential handling

- API keys are stored only in the robot's persistent user configuration directory.
- The key file and its directory use permissions `0600` and `0700` respectively.
- Settings APIs report only whether a key exists; they never return its value.
- `.env`, private-key, and common secret files are ignored by Git.
- `scripts/check_secrets.py` runs locally and in CI before release.
