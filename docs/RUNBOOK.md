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

### Custom Query Window

Override the default search window without code changes:

```bash
# Fetch attachments from the last 90 days (backfill / wider validation)
python -m src.main --mode gmail --account primary \
    --query "newer_than:90d has:attachment"

# Dry-run first to preview without writing
python -m src.main --mode gmail --account primary --dry-run \
    --query "newer_than:90d has:attachment"
```

If `--query` is omitted the default (`newer_than:7d has:attachment`) is used.

### Backfill / Reprocess

Re-download and re-save attachments for messages already in the manifest, without deleting or clearing history:

```bash
python -m src.main --mode gmail --account primary \
    --query "newer_than:365d has:attachment" --reprocess
```

Then regenerate the summary workbook if needed:

```bash
python -m src.main --mode summary --account primary --year 2026
```

**How it works:**

- `--reprocess` skips the manifest duplicate check for this run only.
- The manifest is never deleted or modified differently — existing entries stay.
- If the receipt file is still on disk, the re-downloaded copy is saved alongside it with a `__2` suffix (e.g. `receipt__2.pdf`).
- If the receipt file was deleted but the manifest entry remains, it is saved at the original path.
- Daily runs without `--reprocess` continue to skip duplicates as normal.

### Rebuild Year

Delete all saved receipts for an account/year and re-download from scratch:

```bash
python -m src.main --mode gmail --account primary --year 2026 \
    --query "newer_than:365d has:attachment" --rebuild
```

**What `--rebuild` does:**

- Deletes `receipts/<account>/<year>/` before running
- Implies `--reprocess` (skips manifest duplicate check for this run)
- Does **not** delete the manifest, credentials, or any other year's receipts
- Useful after metadata extraction improvements to regenerate a clean set of PDFs

Run the summary workbook afterwards to refresh the Excel report:

```bash
python -m src.main --mode summary --account primary --year 2026
```

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
