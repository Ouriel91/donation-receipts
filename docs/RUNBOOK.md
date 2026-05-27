# RUNBOOK

## Purpose

This file explains how to run, maintain, and recover the project locally.

It is operational documentation, not architecture documentation.

---

## Local Setup

### Create Python Virtual Environment

```bash
python -m venv .venv
```

---

## Gmail Setup (Future)

> Not yet implemented. These steps will be required once OAuth is wired.

### Prerequisites

- A Google Cloud project with Gmail API enabled
- OAuth 2.0 credentials downloaded as `credentials.json`

### Credential Files

Place credential files inside the relevant account folder:

```
accounts/{account-name}/credentials.json   ← downloaded from Google Cloud Console
accounts/{account-name}/token.json         ← auto-generated on first OAuth run
```

Both files are excluded from Git via `.gitignore`. Never commit them.

### Install Dependencies

```bash
pip install -r requirements.txt