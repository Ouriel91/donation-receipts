# Offline Excel Organization Enrichment

From the repository root with requirements installed:

```sh
python -m src.organization_enrichment --input /path/to/existing.xlsx --cache /path/to/organization-cache.json --output /path/to/new-enriched.xlsx
```

Both inputs must exist. Output must be a new `.xlsx` file in an existing directory. The command never reads PDFs, runs extraction, imports Gmail providers, queries registries, or uses AI. It uses `OrganizationCache.lookup` without modifying the cache. Invalid cache documents fail the command; absent organizations produce unknown results.

Worksheets with both Hebrew headers `מספר עמותה` and `תאריך` in row 1 are enriched regardless of sheet name. Other worksheets remain present. A sheet with just one required header, duplicate required headers, or existing enrichment headers fails the whole operation. Empty rows are skipped; every other row below the headers gets results, including invalid fields. Header rows elsewhere and merged header layouts are unsupported.

Four columns are appended after existing columns:

| Column | Values |
| --- | --- |
| Organization existence (cached) | `exists`, `does_not_exist`, `unknown` |
| Section 46 on donation date (cached) | `approved`, `denied`, `unknown` |
| Cache freshness | `fresh`, `stale`, `not_verified` |
| Enrichment notes | Reason plus the cached-evidence limitation |

Approval and denial require explicit cached periods covering the donation date, inclusive of endpoints. Existence does not imply approval, and a period gap does not imply denial. The existing cache defaults to a one-day TTL; expired or future-dated evidence produces unknown for both existence and Section 46. The lookup API cannot distinguish a missing entry from entirely unknown evidence; those results use `not_verified`. `fresh` describes cached evidence age, not official verification. There is no official-source provenance field; these results are not an independently verified official tax determination.

Text registration numbers must contain nine ASCII digits; leading zeros are retained. Nine-digit integral numeric values are accepted. Shorter numeric values are unknown even if their Excel number format displays leading zeros: the command does not reconstruct missing digits. Dates accept native Excel dates/datetimes (using their date), exact `DD/MM/YYYY`, or `YYYY-MM-DD`. Two-digit years, raw numeric serials without date formatting, invalid dates, and formula inputs are unknown. Formulas are preserved but never evaluated for lookup.

The workbook is loaded normally with formulas and external links retained. Original cells, monthly sheets, extraction/severity statuses, hyperlinks, and supported formatting are preserved by openpyxl. Excel features unsupported by openpyxl, such as some drawings/extensions, may be lost; formula result caches can also be lost and require recalculation in Excel. Only `.xlsx` is supported. Existing tables, filters and print ranges are preserved, not expanded to include new columns.

Symbolic links in input, cache and output paths are rejected. Any existing output (including hard links) is rejected. A temporary workbook is saved in the destination directory and synced, then published using an atomic no-clobber hard link and the temporary name removed. This prevents a competing writer's file from being overwritten. Filesystems without hard-link support fail safely; hostile concurrent replacement of parent directories is outside the protection offered here. No source workbook replacement occurs.

If temporary-file cleanup fails after publication, the command still reports success and emits one fixed warning on stderr: `Warning: output published successfully; temporary workbook cleanup failed.` The complete output is available; a `.enrichment-*.xlsx` temporary name may remain in the output directory. Do not rerun using that same output path. Cleanup failures before publication remain errors.

## Initial Windows workflow and smoke test

Use a local, non-OneDrive output directory for the initially supported Windows workflow. Hard-link support is required. **OneDrive output compatibility is not yet verified**; this also applies to sync-provider interactions with temporary files. Windows runtime validation has not been performed in the Linux implementation environment. Cached Section 46 evidence is not an official tax determination.

From the repository root in PowerShell, with project dependencies already installed, run these exact commands. Verify that `LOCALAPPDATA` points to a local, non-synced directory; if it is redirected, choose an existing local, non-OneDrive parent instead.

```powershell
$ErrorActionPreference = 'Stop'
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project Python environment not found' }
if (-not $env:LOCALAPPDATA) { throw 'Choose an existing local, non-OneDrive parent directory' }
$smokeDir = Join-Path $env:LOCALAPPDATA ('donation-enrichment-smoke-' + [guid]::NewGuid().ToString('N'))
& $python -m scripts.smoke_organization_enrichment --directory $smokeDir
if ($LASTEXITCODE -ne 0) { throw 'Synthetic enrichment smoke test failed' }
Write-Host "Synthetic workbook and cache retained in: $smokeDir"
```

The script creates its own sample Excel workbook and cache in a new directory, invokes the CLI in a subprocess, and checks approval, denial, unknown and stale results, leading zeros, original input/cache bytes, monthly sheets, formulas, styles, hyperlinks and refusal to overwrite its output. It uses no personal files or network services. It retains only generated synthetic artifacts for inspection. Success includes:

```text
Offline enrichment: worksheets=2, rows=6, approved=2, denied=1, unknown=3, stale=1
Cached evidence only; no official-source provenance or independent tax determination.
PASS: synthetic CLI, workbook preservation, unknown/stale results, and no-overwrite check
```

This smoke script can also run on Linux; a Linux pass does not establish Windows or OneDrive compatibility. Inspect the synthetic output in Excel before the first supervised run on an existing workbook.

## Synthetic demo

The reusable `demo` fixture in `tests/test_organization_enrichment.py` creates only synthetic workbook/cache inputs, including recently checked historical periods and expired evidence. Reproduce the demo in a new temporary directory:

```sh
python - <<'PY'
from pathlib import Path
from tempfile import mkdtemp
import runpy
from src.organization_enrichment import main
fixture = runpy.run_path('tests/test_organization_enrichment.py')['demo'].__wrapped__
source, cache, output = fixture(Path(mkdtemp(prefix='enrichment-demo-')))
main(['--input', str(source), '--cache', str(cache), '--output', str(output)])
print(output)
PY
```

Actual synthetic CLI output from validation:

```text
Offline enrichment: worksheets=2, rows=7, approved=2, denied=1, unknown=4, stale=1
Cached evidence only; no official-source provenance or independent tax determination.
```

Actual workbook results (notes abbreviated here; every actual note includes the limitation above):

| Sheet | Organization | Donation date | Existence | Section 46 | Freshness | Reason |
| --- | --- | --- | --- | --- | --- | --- |
| ינואר | 012345678 | 2020-01-15 | exists | approved | fresh | explicit cached evidence |
| ינואר | 012345678 | 2021-01-15 | exists | denied | fresh | explicit cached evidence |
| ינואר | 999999999 | 2020-01-15 | unknown | unknown | not_verified | no usable evidence / missing entry |
| ינואר | missing | 2020-01-15 | unknown | unknown | not_verified | missing/invalid registration number |
| ינואר | bad | 31/02/2020 | unknown | unknown | not_verified | missing/invalid registration number and donation date |
| ינואר | 123456789 | 2020-01-15 | unknown | unknown | stale | stale or future-dated evidence |
| פברואר | 012345678 | 2020-01-15 | exists | approved | fresh | explicit cached evidence |

The demo preserves `ינואר`, `פברואר`, `סיכום`, and `קובצי מקור`, including an existing SUM formula, internal hyperlink, Hebrew statuses, styles, column width, filter and freeze pane. Automated tests compare original cells and source/cache bytes before and after enrichment.
