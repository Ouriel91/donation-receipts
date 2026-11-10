# Phase 4 — official Section 46 enrichment

Phase 4 adds live, read-only Tax Authority verification to an existing donation
workbook. It does not invoke Gmail, extract receipts, rebuild reports, calculate
a guaranteed refund, or claim a donation was reported to the Tax Authority.

## Existing report and original preservation

From the Windows project directory with the existing Python environment:

```powershell
.\.venv\Scripts\python.exe -m src.enrich_2026 --input reports/primary/2026/donation_summary_2026.xlsx
```

A new uniquely named XLSX is created next to the original. Existing output files
are never overwritten. All original sheets, supported formatting, formulas,
hyperlinks and review fields use the existing openpyxl preservation path. The
source SHA256 is checked before/after processing. openpyxl may remove unsupported
Excel extensions and formula result caches; the formulas themselves are retained.

Official names replace extracted names only on exact registration-number matches
in the local official snapshot. The identity audit preserves extracted names,
registry provenance and source workbook hash. Unmatched names remain explicitly
unverified. Pending identities are kept apart from the official snapshot and are
reconciled against it on later runs. Name counters count written official names
and, separately, actual name changes.

Four Hebrew columns explain Section 46 on the donation date, potential credit,
last successful check and the verification details. Potential credit is always
conditional; missing evidence is not a denial.

## Evidence and cache behavior

The fixed HTTPS endpoint is
`https://openapi.taxes.gov.il/shaam/production/Charity/v1/check46Clause`.
POST sends an integer `Charity_association_number`, without an Authorization
header. The transport rejects redirects and non-200 HTTP results, caps responses
at 64 KiB and uses a 20-second socket timeout. JSON schema and canonical dates
are validated. Evidence binds to the requested association, not an invented tax
file. Existing document-backed evidence keeps its original stronger validation.

Only HTTP 200 with `Status` exactly integer 200 and either the exact three-date
approval object or exact `Message` text `Charity association number not found`
is recognized. The latter means no current approval found by this service. It
proves neither registry nonexistence nor historical denial. Unknown schemas and
service failures remain unknown.

The private `section46-live.json` sidecar retains request identity, official
source, raw successful responses, response SHA256, retrieval time, returned as-of
date where present, and independent attempt/success times. No as-of date or
historical interval is invented for the current-not-found response. A negative
service response is a successful cached observation, not an API error.

The default configurable TTL is 30 days (`--freshness-days`, range 1–365),
independent of an approval interval's end. Approval evidence also requires a fresh
server as-of date. Negative observations expire from retrieval time. Requests are
lazy, deduplicated per organization and spaced one second apart. Fresh evidence
is reused, including a historical gap or current-not-found result. Diagnosed
failures have a one-day retry cooldown. Successful prior evidence is preserved.

Historical approval requires an explicit fresh interval covering the donation
date. A current-not-found observation does not erase verified historical approval
periods. Rows distinguish historical gaps, stale evidence, service failures,
unrecognized responses and not-yet-checked cases. Missing identities do not block
Section 46 lookup. Errors expose only safe diagnostic metadata, not donor text.

## Persistent Windows state and refresh

All default enrichment, refresh, manual import and Section 46 CLI operations use
`%LOCALAPPDATA%\DonationReceiptsRuntime\registry`. An explicit `--root` overrides
`DONATION_RECEIPTS_REGISTRY_ROOT`, which overrides the platform default. Manual
import also supports an explicit `--cache`. Missing Windows LOCALAPPDATA fails
rather than silently selecting a project/OneDrive cache. Existing runtime data
must remain local and must never be replaced with sandbox state.

Manual refresh uses the same default root:

```powershell
.\.venv\Scripts\python.exe -m src.registry_sync
```

Refresh uses the existing SHA256-bound recovery only for the exact previously
approved government CSV and its three reviewed malformed spans. Other malformed
files remain rejected. Enrichment requires no registry reimport. No scheduling
is installed or added by Phase 4. Future incremental integrations should reuse
this root resolver and cache rather than create separate state.

For the bounded local follow-up on four previously unrecognized responses:

```powershell
.\.venv\Scripts\python.exe -m src.section46_recheck
```

It prints those organization numbers and safe response types locally. Exactly
four saved candidates are required; all other API requests are blocked, including
during workbook enrichment. Persisted recheck markers allow repeats to reuse
negative TTLs and failure cooldowns. It also creates a new enriched copy.

## Validation and verified completion

Automated tests use synthetic data and mocked transport. Real reports, receipts,
CSV snapshots, operational state, donor data and credentials stay on Windows and
are excluded from Git/review material.

The user reports successful real Windows completion and visual name verification:
75,933 official organizations; 69 donations across 10 worksheets; 30 unique
organizations; 67 corrected names; 53 donation-date approvals; 16 historically
unknown rows. Four previously unrecognized results were independently checked
and are now classified as no current approval. The original workbook was
preserved and the final enriched workbook was created. These are user-verified
product results; the sandbox has not rerun that processing.
