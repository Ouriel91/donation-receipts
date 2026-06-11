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

## Gmail Mode

### Prerequisites

- A Google Cloud project with Gmail API enabled
- OAuth 2.0 credentials downloaded as `credentials.json`

### Credential Files

Place credential files inside the relevant account folder:

```
accounts/<name>/credentials.json   ← downloaded from Google Cloud Console
accounts/<name>/token.json         ← auto-generated on first OAuth run
```

Both files are excluded from Git via `.gitignore`. Never commit them.

### First Run (OAuth)

Run once per account to generate `token.json`. A browser window opens for Google login:

```bash
python -c "from pathlib import Path; from src.providers.gmail_auth import get_gmail_credentials; get_gmail_credentials(Path('accounts/<name>'))"
```

### Dry Run

Plans what would be saved without writing any files or updating the manifest:

```bash
python -m src.main --mode gmail --account <name> --dry-run
```

### Real Run

Downloads attachments and saves receipts:

```bash
python -m src.main --mode gmail --account <name>
```

---

## Gmail Setup (Legacy Placeholder — now implemented above)

### Prerequisites

- A Google Cloud project with Gmail API enabled
- OAuth 2.0 credentials downloaded as `credentials.json`

### Credential Files

Place credential files inside the relevant account folder:

```
accounts/primary/credentials.json   ← downloaded from Google Cloud Console
accounts/primary/token.json         ← auto-generated on first OAuth run
```

Both files are excluded from Git via `.gitignore`. Never commit them.

### Install Dependencies

```bash
pip install -r requirements.txt
```

### First Run

Run the auth helper once per account to generate `token.json`:

```bash
python -c "from pathlib import Path; from src.providers.gmail_auth import get_gmail_credentials; get_gmail_credentials(Path('accounts/primary'))"
```

A browser window will open for Google login. After login, `token.json` is saved locally and reused on future runs.
