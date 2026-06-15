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

---

## Summary Workbook

Generates a yearly Excel workbook from saved receipt PDFs.

### Input

```
receipts/<account>/<year>/<month>/*.pdf
```

### Output

```
reports/<account>/<year>/donation_summary_<year>.xlsx
```

One sheet per month that has PDFs. One row per receipt PDF.

### Run

```bash
python -m src.main --mode summary --account <name> --year <year>
```

Example:

```bash
python -m src.main --mode summary --account primary --year 2026
```

### Columns

`file_name`, `file_path`, `organization_name`, `registration_number`,
`receipt_number`, `tax_report_number`, `receipt_date`, `amount`,
`extraction_status`, `notes`

### extraction_status values

- `ok` — all critical fields found
- `partial` — critical fields found but some optional fields missing
- `needs_review` — a critical field (amount, receipt_date, organization_name) is
  missing, or the PDF could not be read

`needs_review` rows are expected and are meant for manual correction in Excel.
`file_name` and `file_path` are always populated so every row traces back to its
source PDF.

### Notes

- Existing workbooks are overwritten on re-run (reports are regenerable).
- If no receipts exist for the given account/year, the workbook is still created
  with a Summary sheet explaining that no receipts were found.

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
