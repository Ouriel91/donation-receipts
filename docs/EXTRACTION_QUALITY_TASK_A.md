# Extraction quality task A: review handoff

## Scope and version

- Branch: `feat/2026-extraction-quality`; base: `541b87e`.
- Review the uncommitted diff of `src/receipt_metadata_extractor.py`,
  `src/receipt_summary.py`, `tests/test_receipt_metadata_extractor.py`,
  `tests/test_receipt_summary.py`, and `README.md`, plus the new
  `tests/test_extraction_quality.py` and this document.
- Goal: explicit receipt identifiers before filename fallbacks, robust existing
  donor-ID labels after PDF normalization, and consistent year-aware review
  severity/status/notes. No donor-name extraction change, OCR, duplicate
  consolidation, external registry integration, or agent infrastructure work.
- All new test text and identifiers are invented. No private receipt, workbook,
  Gmail content, account configuration, or donor data belongs in the review.
- Legacy examples in an extractor comment and the two existing test files were anonymized, with
  identifier-prefix relationships preserved. Deleted lines in a historical diff
  still contain legacy values: do not send that raw diff to external models.
  Use current sanitized source/test files for a later model review.

## Decisions and acceptance checks

- Explicit receipt labels accept Latin letters/digits with `-`, `/`, `_`, or
  `.` separators and require at least one digit. Normal and reversed Hebrew
  labels are supported. Other extracted identifiers are excluded conservatively;
  a conflicting candidate remains unfilled rather than being reused.
- Text identifiers take precedence before a filename fallback across raw and
  normalized text and across both existing PDF extraction engines. Existing
  unlabelled numeric heuristics are not broadened.
- Donor-ID labels retain explicit anchors and exactly nine digits; common bidi
  marks are ignored locally. The normalizer can reverse the unpunctuated label
  or Hebrew quote punctuation, while spaced punctuation can interrupt matching.
- From 2026, a missing tax report number alone is `warning` / `partial` with
  `מספר אישור דיווח חסר — דרוש אימות`. The value is not invented, and no tax
  eligibility is inferred. Missing core fields or donor mismatch remain
  `critical` / `needs_review`.
- In 2020–2025, a missing report number alone remains nonblocking; the summary
  status is `ok` with the existing not-required note. Summary extraction status
  is aligned with the same severity rules after field merging and donor checks.

## Task A validation before the long-identifier follow-up

- Focused: **passed**, 278 tests, covering the new synthetic regressions plus
  existing metadata and summary tests; final rerun after fixture/comment cleanup
  (1.52 seconds).
- Full suite: **passed**, 511 passed, 2 skipped after fixture cleanup (47.93 seconds).
- `git diff --check`: **passed**. Added-content screening found no secret or
  email patterns and no nine-digit literals outside the declared synthetic
  fixtures. New files were checked separately for whitespace.
- Commands use `python -m pytest -q -p no:cacheprovider --tb=short`, a new short
  `--basetemp` per run, `PYTHONDONTWRITEBYTECODE=1`, and
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Focused paths are
  `tests/test_extraction_quality.py tests/test_receipt_metadata_extractor.py
  tests/test_receipt_summary.py`; the full run targets `tests/`.
- The first focused run found three normalization edge cases; they were fixed.
  Self-review then added cross-engine receipt-precedence regressions.
- Final local validation regenerated the summary after making a verified,
  uniquely named backup. The resulting workbook opened and matched the scanned
  PDF count. Only aggregate comparisons were emitted. Input receipt, manifest,
  and preserved-baseline hashes remained unchanged. A populated receipt value
  changed from filename fallback to receipt-specific text; human comparison is
  still needed to establish its correctness. No private values were sent to a model.

## Long receipt identifier follow-up

- Human review identified a shortened identifier. Local diagnosis reproduced the
  cause: the reversed donation regex capped its capture at eight digits and had
  no left boundary, so it could match a suffix inside a longer numeric run.
  Both PDF engines contained the full digit run, and normalization preserved it.
- Removed the eight-digit caps from numeric receipt layouts and middle filename
  fallback. Added token boundaries to prevent partial numeric matches. Removed
  the receipt validator's arbitrary 32-character limit. Existing explicit
  alphanumeric labels and identifier exclusions remain in use.
- Receipt values remain strings throughout extraction. The workbook writer now
  also applies Excel text format (`@`) to the receipt column. The inspected old
  workbook cell was numeric; its conversion history was not established.
- Added 71 synthetic regression cases for long numeric and alphanumeric IDs,
  leading zeros, complete filename fallback, token boundaries, both engine
  precedence paths, and XLSX string round trips beyond Excel's numeric precision.
- Focused: **passed**, 349 tests (6.59 seconds), using the same three focused
  test modules and command options above with a fresh short `--basetemp`.
- Full suite: **passed**, 582 passed, 2 skipped (31.45 seconds), targeting
  `tests/` with a separate fresh short `--basetemp`.
- After closing Excel, local diagnosis and summary regeneration completed.
  A unique backup was created and its hash verified before replacement. Local
  source comparison verified the affected row's complete identifier and XLSX
  string type/text format. Only booleans and aggregates were returned.
- Report rows still match the scanned PDFs. No populated fields were lost;
  severity counts were unchanged. Receipt and manifest hashes were unchanged.
  The backup and local validation artifact stay in the ignored report directory;
  the one-off local helper stays in ignored `tmp/`.
- `git diff --check`: **passed**. The previous working-tree files were preserved;
  no commit or external service operation was performed.

## Independent review and delivery

- Claude review: **Pending / not run**. This branch has no `agent_loop.review` module.
  The available demo only accepts its fixed fictional task, and general Live
  adapters remain gated by `LIVE_ISOLATION_VERIFIED=False`. No replacement or
  gate bypass was added. This handoff does not claim independent approval.
- A later reviewer should examine label boundaries, identifier collisions,
  raw/normalized and cross-engine precedence, and consistency of workbook
  severity, status, and notes for 2020–2026.
- Remaining limitation: populated receipt fields still need comparison with
  source receipts to establish accuracy. A classification improvement alone
  does not measure extraction accuracy.
- No Commit, Push, PR, or Merge has been performed for task A. Gmail, rebuild,
  and reprocess were not used; source receipts and the manifest were not changed.
