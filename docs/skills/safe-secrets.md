# Skill: Safe Secrets Handling

## Purpose

Define how the project should handle credentials, tokens, and local configuration safely.

---

## Core Rule

Never hardcode secrets in source code, tests, documentation examples, or committed files.

---

## Secrets Examples

Sensitive files may include:

- Gmail OAuth credentials
- Gmail OAuth tokens
- .env files
- API keys
- Access tokens
- Refresh tokens
- Personal email account tokens

---

## Expected Secret Locations

Secrets should stay local only.

Expected local paths:

```text
secrets/credentials.json
accounts/<account-name>/token.json
.env