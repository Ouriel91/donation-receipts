# Donation Receipts

Donation Receipts is a Python command-line application for collecting likely donation receipt attachments from Gmail, organizing them locally, and generating annual Excel workbooks for review. It includes a local harness for exercising email detection and file handling without connecting to Gmail.

## Implemented capabilities

- Detects likely donation emails using Hebrew and English keywords in the subject and body. Gmail processing uses the message snippet as the body.
- Accepts PDF, JPG, JPEG, and PNG attachments; skips low-confidence messages and unsupported attachment extensions.
- Downloads Gmail attachments and checks PDF text for donation signals before saving PDFs.
- Organizes files by account, email year, and month, with sanitized filenames and numeric suffixes to avoid overwriting existing files.
- Records processed message IDs per account in a local JSON manifest and skips those messages on subsequent runs.
- Provides Gmail dry runs, explicit reprocessing, year-based searches, custom Gmail queries, and receipt-directory rebuilding.
- Generates annual Excel workbooks from saved PDFs, with Hebrew headers, right-to-left monthly sheets, extracted metadata, donor matching, review notes, and severity classifications.
- Handles Gmail result pagination and retries selected rate-limit errors with bounded delays.

## Architecture and processing flow

`src/main.py` dispatches three implemented modes: `harness`, `gmail`, and `summary`. Paths are configured in `src/config.py` and are relative to the current working directory. Run commands from the repository root.

### Gmail processing

1. Load OAuth credentials for the selected account and fetch messages matching the search query.
2. Map each message to its ID, subject, sender, date, snippet, and attachment metadata.
3. Skip messages already recorded for that account, unless reprocessing is enabled.
4. Apply keyword-based detection and skip low-confidence messages.
5. Plan paths for supported attachments using the email date.
6. Download attachments. For PDFs, extract text and search both raw and normalized Hebrew text for donation signals.
7. Save accepted attachments and record messages with at least one saved attachment in the manifest.
8. Print processing counts and skipped-attachment reasons.

Files follow this structure:

```text
receipts/<account>/<year>/<NN_Month>/<DD_MM_YY>__<sanitized_label>.<extension>
```

Existing filenames receive suffixes such as `__2`. The directory date comes from the email, not the extracted receipt date.

### Harness processing

The harness loads local email fixtures from `harness/emails`, applies email detection, plans supported attachment paths, and uses the fixed account label `harness`. A non-dry run writes empty placeholder files and updates the manifest. It does not download attachments or validate PDF content.

### Summary processing

Summary mode scans PDFs directly inside the expected month folders for one account and year. It writes:

```text
reports/<account>/<year>/donation_summary_<year>.xlsx
```

The workbook contains a sheet for each month with PDFs, or an empty-summary sheet when none are found. Fields include organization name, registration number, receipt number, tax report number, receipt date, amount, donor name and ID, donor match, severity, extraction status, and notes.

Severity rules classify receipts as `ready`, `warning`, or `critical`. Missing amount, date, receipt number, or registration number is critical. For report years from 2026 onward, a missing tax report number is also critical. Donor mismatches are critical; an undetected configured donor or missing organization name can produce a warning. These are application review rules, not a determination of legal or tax eligibility.

The console's `usability` value is the proportion classified as ready or warning. It is not an extraction accuracy measurement.

## Setup

Use Python 3.10 or later for the source syntax shown in this project. From a PowerShell terminal at the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Dependencies include the Google authentication and Gmail API libraries, PDF libraries, `openpyxl`, and `pytest`. `requirements-dev.txt` includes `requirements.txt`.

### Gmail OAuth

Prepare a Google OAuth application for Gmail API access and place its installed-application client configuration at:

```text
accounts/<account>/credentials.json
```

If no valid token exists, Gmail authentication opens a local-server OAuth authorization flow. The application requests only:

```text
https://www.googleapis.com/auth/gmail.readonly
```

It saves authorized credentials to `accounts/<account>/token.json`, refreshes expired tokens when possible, and starts authorization again if refresh fails with a `RefreshError`.

Use a neutral local account label such as `sample_account` in commands. Each Gmail invocation selects one account directory.

### Optional summary configuration

Create `accounts/<account>/config.json` to configure expected donor names or IDs. The file is optional; summary generation continues without it.

```json
{
  "display_name": "Example account",
  "expected_donor_names": [],
  "expected_donor_ids": []
}
```

Configured IDs are checked first. When an ID is unavailable, configured names can be matched by case-insensitive substring comparison. Keep actual donor values private.

## PowerShell CLI

These commands match the supplied CLI implementation. Replace `sample_account` and the year as needed.

### Harness

Preview detection and planned paths:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode harness --dry-run
```

Write placeholder files and record processed fixture messages:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode harness
```

### Gmail

Preview the default search, `newer_than:7d has:attachment`:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account --dry-run
```

Download and process attachments using the default search:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account
```

Search using the CLI's year-based query:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account --year 2025
```

This constructs `after:2025/01/01 before:2026/01/01 has:attachment`.

Supply an explicit Gmail query:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account --query 'after:2025/01/01 before:2026/01/01 has:attachment donation' --dry-run
```

Revisit messages already in the manifest within the selected search:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account --year 2025 --reprocess
```

Preview or perform a rebuild:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account --year 2025 --rebuild --dry-run
.\.venv\Scripts\python.exe -m src.main --mode gmail --account sample_account --year 2025 --rebuild
```

### Summary

Generate the annual workbook from locally saved PDFs:

```powershell
.\.venv\Scripts\python.exe -m src.main --mode summary --account sample_account --year 2025
```

### Useful flags and behavior

| Flag | Behavior |
| --- | --- |
| `--mode` | Required. Implemented values are `harness`, `gmail`, and `summary`. |
| `--account` | Required for Gmail and summary modes. Selects the local account directory. |
| `--year` | Sets the Gmail year query unless `--query` is supplied; required for summary and Gmail rebuilding. |
| `--query` | Supplies a Gmail search query and overrides the year-derived query. |
| `--dry-run` | In harness and Gmail modes, plans receipt paths without saving receipt files or updating the manifest. |
| `--reprocess` | In Gmail mode, bypasses the processed-message check for messages returned by the current query. |
| `--rebuild` | In Gmail mode, deletes `receipts/<account>/<year>/` before processing and implies `--reprocess`. Requires `--year`. |

`--reprocess` is a flag, not a separate backfill mode. It does not expand the search range, remove existing receipts, or replace existing manifest records. Reprocessing can create additional files with numeric suffixes.

A Gmail dry run still authenticates, fetches message data, and may create or refresh the OAuth token. It skips attachment downloads and PDF validation, so its reported paths and counts are previews. Summary mode does not use `--dry-run`; it writes the workbook.

**Caution:** `--rebuild` deletes local receipt files before fetching Gmail messages and leaves the manifest intact. Back up the target year and preview the query with `--dry-run` first. It does not guarantee restoration of every deleted file. A custom query still overrides `--year`, so ensure it matches the directory being rebuilt.

## Project layout

```text
src/
  main.py                       CLI dispatch and console summaries
  config.py                     Local path configuration
  gmail_runner.py               Gmail processing pipeline
  harness_runner.py             Local fixture processing
  attachment_saver.py           Path planning and file saving
  manifest.py                   Processed-message tracking
  receipt_detector.py           Email keyword detection
  pdf_donation_validator.py     PDF donation-signal checks
  pdf_text_extractor.py         PDF text extraction
  hebrew_text_normalizer.py     Hebrew text normalization
  receipt_summary.py            Metadata review and Excel generation
  account_config.py             Optional account configuration
  providers/
    email_provider.py           Provider interface
    gmail_auth.py               OAuth credentials and token refresh
    gmail_provider.py           Gmail fetching and downloads
    harness_provider.py         Local fixture provider
agent_loop/                     Experimental development tooling
harness/emails/                 Local email fixtures
accounts/<account>/             Local credentials and optional configuration
receipts/                       Saved attachments
reports/                        Generated workbooks
data/processed_messages.json    Local processing manifest
docs/AGENT_LOOP_DEMO.md         Supervised Agent Loop demo documentation
requirements.txt
requirements-dev.txt
```

## Tests

Install development dependencies and invoke pytest from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

The test suite covers CLI behavior, Gmail and harness pipelines with fake providers, receipt detection, PDF handling, file saving, the manifest, summary workbooks, and the experimental Agent Loop. It uses local fixtures and mocks; it does not require Gmail access. A harness dry run previews processing but does not verify Gmail authentication or attachment downloads.

## Experimental Agent Loop

The Agent Loop is an experimental development tool. `python -m agent_loop` remains the Mock command. General Live adapters remain gated, with `LIVE_ISOLATION_VERIFIED` set to `False`.

A separate, supervised fictional demo is documented in `docs/AGENT_LOOP_DEMO.md`:

```powershell
.\.venv\Scripts\python.exe -m agent_loop.demo --run-models
```

The demo uses fixed tasks and tests, requires `START` before model execution and `RUN` before each test run, and limits the loop to three rounds. It uses the CLIs' existing login and accepts only inspected CLI versions. Running without `--run-models` exits before starting a model.

The demo does not instantiate the general Live adapters and performs no commit, push, pull request, or merge. Its CLI restrictions and Python syntax filter do not provide OS isolation. Actual model behavior and authentication have not been tested by the implementation test suite; the first opt-in run requires human supervision.

## OAuth and local data privacy

Gmail access uses a read-only OAuth scope. Receipt files, workbooks, account configuration, and the processing manifest are stored locally. The manifest contains account labels, message IDs, and saved paths; receipts and reports can contain sensitive donor information.

The repository's ignore rules exclude OAuth credentials and tokens, account configuration, receipts, reports, runtime JSON data, and logs. Ignore rules do not encrypt files or protect copies shared elsewhere. Keep these files out of commits and shared output, and review console logs before sharing them.

The core receipt flow shown here uses Gmail API access and local processing. The optional Agent Loop demo invokes external model CLIs for its fictional task; its documented runner does not read project credentials or send project files to those models.

## Limitations

- Detection and PDF validation rely on keyword rules. Unusual wording, misleading snippets, or missing extractable text can cause incorrect acceptance or rejection.
- Gmail PDF validation requires extractable donation signals. The shown validation flow has no OCR fallback; scanned PDFs may be rejected.
- JPG, JPEG, and PNG attachments can be saved from qualifying messages without a content check. The annual workbook scans only PDFs in the expected month directories.
- Duplicate tracking is per account and message ID, not attachment content. A message with some successfully saved attachments is recorded even if other attachments fail.
- The manifest is a local JSON file without a demonstrated concurrent-write mechanism. Avoid overlapping runs that share it.
- Running summary again writes the same account/year workbook path.
- Only harness, Gmail, and summary modes are implemented. Scheduling and unattended multi-account orchestration are not implemented by the shown CLI.

## Roadmap

Potential future work, not implemented capabilities:

- OCR support for scanned receipts and image summaries.
- Attachment-level retry tracking and content-based duplicate detection.
- More robust extraction across receipt layouts and clearer review diagnostics.
- Verified isolation before enabling general Agent Loop Live adapters.
