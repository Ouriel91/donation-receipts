# Task B: annual duplicate consolidation and source links

## Task and starting point

- Branch: `feat/2026-extraction-quality`; starting HEAD:
  `9a41782c5a8d337ae76a4a187283bf78273cad9e` (task A committed).
- Working tree was clean. Task A is preserved unchanged in its own commit.
- Scope: annual summary consolidation, complementary field completion, source
  links, monthly placement, aggregate workbook metrics, and synthetic tests.
- Expected diff: `src/receipt_summary.py`, `src/receipt_metadata_extractor.py`,
  `tests/test_extraction_quality.py`, new
  `src/receipt_deduplication.py`, new `tests/test_receipt_deduplication.py`,
  `README.md`, and this handoff. The receipt-phrase follow-up below adds only
  focused extractor changes. No Gmail, manifest, PDF, or agent infrastructure
  changes. No OCR or external registry/tax eligibility work.
- Relevant guidance: `docs/RULES.md`, `docs/TASK_TEMPLATE.md`,
  `docs/CLAUDE_WORKFLOW.md`, and the testing, secrets, receipt-detection, and
  filesystem guidance in `docs/skills/`.
- Recommended implementation stage: a medium-reasoning coding model for focused
  local changes; an independent Claude review if a safe existing workflow is
  available. This recommendation does not switch a model or enable an adapter.

## Decisions and acceptance criteria

- Require all four identity fields: valid corporate/nonprofit registration ID,
  exact receipt string, valid date, and positive finite decimal amount. Never
  remove identifier separators or leading zeros, or compare amounts as floats.
- Complete identities must agree throughout the candidate group. Conflicting
  nonempty organization, donor, or reporting fields block consolidation as well.
  Whitespace-only differences in complementary fields are tolerated.
- Candidate groups span every scanned month. Org + receipt, or three matching
  identity fields when an identifier is missing/different, trigger review.
  Receipt number alone never triggers a merge. Different organizations never
  merge; if the other three fields match they are flagged for human review.
- Ambiguous/incomplete bridge records prevent merging an entire connected
  candidate group. No partial-identity automatic merges or fuzzy matching.
- Fill missing complementary fields of the first scanned record only when
  consistent. The completion metric counts fields filled in that output record,
  not missing slots in the discarded copies. Preserve all source references.
- Consolidate extracted rows before computing final donor match, severity,
  status, and field notes. Suspected duplicates remain separate with at least
  warning/partial; existing critical/needs_review is retained. Missing reporting
  number remains distinct from tax eligibility and follows task A's year rules.
- Primary source and monthly placement follow the earliest scanned source
  month/name. Preserve month sheets, including those left with a header only.
- Preserve existing metadata column positions; append source-count and source
  list links. A source sheet contains one hyperlink per PDF plus a backlink to
  its donation row, and no amount column. File links are absolute local URIs.
  Filenames/extracted strings are stored as text, never as Excel formulas.
- Keep the existing workbook function's `(path, severity_counts)` return shape.
  Add aggregate before/after metrics in the workbook summary sheet. Suspected
  rows still contribute amounts and provisional donation counts until reviewed;
  unknown amounts are counted separately rather than treated as verified zero.

## Initial task B validation

- Focused tests: **passed**, 398 tests (2.21 seconds).
- Full suite: **passed**, 631 passed, 2 skipped (18.05 seconds).
- Added 49 synthetic cases. The first focused run had one fixture failure:
  a numeric filename supplied the supposedly missing receipt ID via the existing
  fallback. Non-numeric fixture filenames corrected the setup; the complete
  focused rerun and subsequent full suite passed.
- Local report: **passed** after a verified unique backup of the workbook on
  disk. All 71 PDFs are represented exactly once in source links; every linked
  file exists, every backlink and source count agrees with its donation row,
  receipt identifiers round-trip as strings, and workbook ZIP/openpyxl checks pass.
- Results: 71 input PDFs, 70 donation rows, one merged group of two sources
  across months, and one completed tax-report field. One suspected group of
  two sources lacks receipt IDs and remains separate for manual review.
- Severity before: 43 ready, 25 warning, 3 critical. After: 43 ready, 24 warning,
  3 critical. Two missing amounts remain missing. Verified that the known total
  decreases by exactly the extra copy's amount, that confirmed groups contribute
  once, and that suspected rows remain in the total. Donation counts/totals are
  provisional until the suspected group is reviewed.
- Baseline exception: one nonempty receipt field in the pre-run workbook differed
  from the new output. A local replay of the trusted task A implementation from
  commit `9a41782` reproduced the new full identifier; the disk workbook contained
  a shortened numeric value. Its origin was not assumed. This is not a task B
  extractor change. The original workbook backup is preserved. Other nonempty
  metadata matched or was completed consistently; no conflicting merge overwrites
  or field loss was found.
- Regenerated again after backing up the interim report, and repeated validation
  against the preserved task A workbook. Receipt-tree (including non-PDF inputs)
  and manifest hashes were identical before/after that run; the task A backup
  hash was also unchanged. One-off helpers and aggregate output stay ignored under
  `tmp/` and `reports/`; no receipt values or filenames were emitted to a model.
- Commands use `python -m pytest -q -p no:cacheprovider --tb=short`, a new short
  `--basetemp` for each run, `PYTHONDONTWRITEBYTECODE=1`, and
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Focused paths:
  `tests/test_receipt_deduplication.py tests/test_receipt_summary.py
  tests/test_extraction_quality.py tests/test_receipt_metadata_extractor.py`.
  Full suite: `tests/`. Filesystem tests require access outside the restricted
  agent filesystem environment.
- `git diff --check`: **passed**. The privacy check covers added and removed
  content plus all three untracked task files; only the five planned task B
  files changed. No receipt, workbook, configuration, or local report is staged.

## Receipt phrase follow-up

- Reproduced with invented data: `קבלה # 313123` matched, whereas
  `קבלה על תרומה 313123` failed in both raw and normalized text. The legacy
  donation pattern requires `קבלה תרומה` and does not allow the extra word.
- Added explicit forward and number-before-label patterns for `קבלה על תרומה`,
  including normalized/reversed Hebrew, `קבלת` from normalizing `תלבק`, flexible
  whitespace, and optional colon/hash punctuation. Full alphanumeric tokens
  preserve long identifiers, separators, and leading zeros. Existing identifier
  exclusions and explicit-label priority remain in use; no partial-token matches
  are permitted. Legacy numeric receipt heuristics were not broadened.
- Local preflight found a second layout issue: the alternative PDF engine
  supplied the peer ID before a reversed hash/receipt label. Added an explicit
  number-before-`#` receipt-label pattern, with complete-token boundaries and the
  same identifier exclusions. No private tokens or receipt text were emitted.
- Consolidation rules and implementation are unchanged. The follow-up fixes the
  missing input receipt ID; it does not permit incomplete/conflicting identities
  to merge or override optional-field conflicts.
- Added 110 synthetic regression cases, including end-to-end workbook processing
  of the two receipt formats, conflicting identity/report fields, text priority
  over filename fallback, and normalized/visual PDF forms. One first-run case
  found the `קבלת` normalization form; the final focused rerun passed.
- A synthetic self-check also found that a donation heading followed by a date
  could be read as a receipt token. The two new patterns now skip date-shaped
  candidates; existing explicit receipt-label rules are unchanged. Four added
  raw/normalized regression cases cover that ambiguity.
- Cross-engine regressions include a document whose label is absent in the
  primary engine but available in visual order in PyMuPDF; consistent copies
  merge, while identity or reporting conflicts keep them separate.
- Focused: **passed**, 508 tests (41.25 seconds), using the same focused paths,
  environment options, and a fresh short basetemp as above.
- Full suite for the final hash-order addition: **passed**, 741 passed, 2 skipped
  (110.82 seconds), with a separate fresh short basetemp and the same options.
- `git diff --check` and local added/removed-content privacy screening: **passed**;
  all numeric ID examples are declared synthetic and private artifacts are ignored.
- Local report backup, regeneration, and comparison: **passed**. Read-only
  preflight initially encountered `PermissionError`; the user closed Excel and
  reading succeeded. After the final full-suite gate, a unique workbook backup
  was created and verified, then only the summary CLI was run.
- The human-confirmed target pair now consolidates. All 71 sources remain linked
  exactly once; donation rows changed from 70 to 69, merged groups from 1 to 2,
  and suspected groups from 1 (two documents) to 0. The existing merged group
  was preserved. All merged groups passed independent complete-identity and
  non-conflicting-field checks using locally extracted metadata.
- One previously missing receipt field was extracted; no populated receipt field
  changed. Complementary fields supplied by copies remain one (the prior tax
  report completion). Two missing amounts remain missing. No nonempty fields
  were lost or overwritten by conflicting values, and the known-total reduction
  equals exactly the newly eliminated copy's amount.
- Severity changed from 43 ready / 24 warning / 3 critical to
  44 ready / 23 warning / 2 critical. Workbook opening, local link targets,
  backlinks, receipt string cells, source counts, and monthly placement passed
  validation. Receipt-tree/manifest hashes and the backup hash were unchanged.
  Only booleans and aggregate statistics left the local validation process.

## Review and delivery

- Claude review: **Pending / not run**. No `agent_loop.review` exists on this
  branch. The demo accepts only its fixed fictional task; general Live adapters
  remain blocked by `LIVE_ISOLATION_VERIFIED=False`. No substitute was built.
- Review the task B working diff against the exact task A commit above, including
  the three new files. Do not include private reports, local helper scripts,
  receipt contents, configuration, or older historical diffs in model context.
- Key remaining limitation: matching extracted metadata is not independent
  ground truth. Manual review is required for suspects and incorrect extraction.
- Implementation and automated/local validation are ready for an authorized
  local commit. Independent Claude review remains pending. The human-confirmed
  pair is now merged without changing the conservative consolidation rules;
  remaining critical rows still require human review of their missing fields.
- No Commit, Push, PR, or Merge authorized/performed for task B. No Gmail,
  rebuild, reprocess, or source PDF/manifest modifications.
