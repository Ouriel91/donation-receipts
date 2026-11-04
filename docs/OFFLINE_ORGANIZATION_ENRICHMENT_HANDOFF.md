# Task and review handoff: Offline Excel Organization Enrichment

## Task

Goal: enrich an existing summary workbook from the approved local OrganizationCache without PDFs, extraction, Gmail, internet, real donor data, or AI guesses.

Scope: standalone CLI/module, synthetic tests, user documentation. Preserve existing workbook content and extraction/severity statuses; fail safely on collisions; expose explicit unknowns and cached-evidence limitations. Out of scope: cache schema changes, Agent Loop redesign, commits, push, PR, merge, LIVE_ISOLATION_VERIFIED changes.

Expected and actual files: `src/organization_enrichment.py`, `tests/test_organization_enrichment.py`, `docs/OFFLINE_ORGANIZATION_ENRICHMENT.md`, `scripts/smoke_organization_enrichment.py`, this handoff. Inputs are existing XLSX/cache JSON; output is a new XLSX with four appended enrichment columns. Success criteria are the user requirements and documented synthetic cases.

## Session and decisions

Starting branch: `feat/organization-cache-integration`; base commit: `47b1ac5eb2f9e4ba970e8f68990e17060a2ea187`. Starting working tree was clean. Read RULES, TASK_TEMPLATE, CLAUDE_WORKFLOW, filesystem-output/testing-policy/safe-secrets guidance, relevant cache/report/main code and tests. Recommended implementation model: Sol medium (focused Python feature and filesystem tests); no switch performed.

Use header-based worksheet discovery without importing extraction or Gmail modules. Retain default cache TTL. Unknown-all results use not_verified because lookup does not distinguish missing entries from unknown evidence. Append only; reject ambiguous headers and already-enriched sheets. Atomic no-clobber hard-link publication is used instead of replacement to honor the project's no-overwrite rule even with competing writers. No cache writes are performed by enrichment.

## Original implementation validation

Environment: Linux, Python 3.14.10. All checks offline with synthetic data.

- **Passed**: `python -m pytest tests/test_organization_enrichment.py tests/test_organization_cache.py tests/test_receipt_summary.py -q --basetemp /tmp/enrich-relevant-01`: 194 passed (before final added test).
- **Passed**: `python -m pytest tests/test_organization_enrichment.py -q --basetemp /tmp/enrich-added-01`: 25 passed, including final added test.
- **Passed**: `python -m pytest -q -rs --basetemp /tmp/enrich-full-final-01`: 979 passed, 12 skipped, one existing fitz deprecation warning. Skips: seven Windows-specific tests and five Docker opt-in tests. These platform/integration checks were **not run**.
- **Passed**: synthetic demo using the documented fixture and actual CLI; output: `worksheets=2, rows=7, approved=2, denied=1, unknown=4, stale=1`. Full sample results and reproduction command are in the user documentation. Generated artifacts are in `/tmp/enrichment-demo`, outside tracked project files.
- **Passed**: `git diff --check` (new files also inspected directly).

## Defined version for independent review

No tracked changes; all five listed files are new/untracked. Review these complete files against the base commit. SHA-256 of the implementation, test, user-doc and smoke-script files:

```text
2a62921863af1e3c72e1cd801248b608a1ca5ffc3d095df915ef68a139c42f34  src/organization_enrichment.py
073846505eec3721b1105be5450f9452d22ab2d0d47deb81b52a2e827f10382e  tests/test_organization_enrichment.py
557e532a51dc78a8b37081f94b731a75c2ebd34b45a1696f3ca7a94ea02e1545  docs/OFFLINE_ORGANIZATION_ENRICHMENT.md
9c983e05a6cbf178c426535f04bef5673b16c5a2566ca58d4adecdeaac313777  scripts/smoke_organization_enrichment.py
```

The user reports Claude completed a read-only review with verdict FIXES REQUIRED. The requested findings were writable Windows fsync, successful-publication cleanup handling, capability-aware symlink testing, and Windows/OneDrive documentation. All requested fixes are implemented below. Review of this revised version has **not run**. Next step: a focused independent re-review of these fixes, then a synthetic Windows smoke run. Recommended reviewer: Claude Sonnet; no model invocation was performed in this fix session.

Limitations: openpyxl cannot preserve every Excel feature or formula cached result; formulas are never evaluated for lookup. Hard-link support is required. Header row must be row 1. Freshness means cache age, not official validation; no provenance field exists. Hostile parent-directory replacement is outside scope. See user documentation for accepted number/date formats.

Delivery: no commit, push, PR, merge or deployment performed. LIVE_ISOLATION_VERIFIED untouched. Stop after implementation and validation as requested.


## Review fixes for PR #2

Scope is limited to the four user-supplied findings and their validation. Sol medium remains suitable for this focused stage; no model switch was made. Existing local feature files were preserved and updated; no unrelated modules changed.

- Sync now opens the temporary workbook with `r+b`, flushes it and calls `os.fsync`. A focused test checks the exact mode, flush and fsync descriptor.
- Publication state is tracked. An `OSError` removing the temporary name after successful atomic publication emits a fixed, bounded stderr warning without changing success. A test verifies CLI success, the complete readable workbook, unchanged source, leftover temporary content, and continued refusal to overwrite. Before publication, cleanup errors remain errors. Atomic `os.link` publication is unchanged.
- Only the symlink collision case skips on Windows error 1314 (missing create-symlink privilege). Other symlink errors propagate; ordinary collisions, hard links and publication tests remain mandatory.
- Documentation now specifies local, non-OneDrive Windows output, explicitly unverified OneDrive support, and exact PowerShell commands. The official-tax-determination disclaimer remains.
- Added a standalone synthetic smoke script with no pytest dependency. It creates a new sample workbook and cache, invokes the CLI, validates evidence and preservation, and checks no-overwrite behavior. It never uses personal files or the network.

### Fix validation

Environment remains Linux / Python 3.14.10. Windows is unavailable; Windows and OneDrive smoke validation are **not run**. Exact PowerShell instructions are in `docs/OFFLINE_ORGANIZATION_ENRICHMENT.md`.

- **Passed**: `python -m pytest tests/test_organization_enrichment.py -q --basetemp /tmp/enrich-fixes-focused-01`: 27 passed.
- **Passed**: existing documented synthetic demo, rerun with the fixture and CLI in a new directory: `worksheets=2, rows=7, approved=2, denied=1, unknown=4, stale=1`. Artifact: `/tmp/enrich-fixes-demo-ufcdt3rw/output.xlsx`.
- **Passed on Linux only**: `python -m scripts.smoke_organization_enrichment --directory /tmp/enrich-fixes-smoke-01`: `worksheets=2, rows=6, approved=2, denied=1, unknown=3, stale=1`; preservation and no-overwrite checks passed. Synthetic artifacts retained in that directory.
- **Passed**: `python -m pytest -q -rs --basetemp /tmp/enrich-fixes-full-01`: 981 passed, 12 skipped, one existing fitz deprecation warning. Seven Windows-specific tests and five opt-in Docker tests were **not run**. Runtime: 52.39 seconds.
- **Passed**: whitespace inspection of all five feature files, including untracked additions.

Remaining next steps: focused independent review of this revised version and the documented synthetic Windows run. OneDrive support remains unverified. No commit, push, PR, network access, real receipts, Agent Loop edits or LIVE_ISOLATION_VERIFIED changes were performed.
