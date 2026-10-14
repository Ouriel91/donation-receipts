# Project work rules

This is the shared source of work rules for Codex and Claude. `AGENTS.md` and `CLAUDE.md` point here. Current code and Git state take precedence over older plans. Apply relevant guidance in `docs/skills/` for testing, secrets, filesystem output, and receipt detection.

Use `README.md` and `docs/PROJECT_CONTEXT.md` for project context, and `docs/RUNBOOK.md` for operating instructions when relevant. These documents may be outdated; verify their claims and commands against current code. A small task does not require reading every document in full.

## Scope and design

- Define one focused task with a goal, in-scope and out-of-scope work, expected files, success criteria, and relevant tests. Use `docs/TASK_TEMPLATE.md`.
- Keep changes small, readable, reversible, and limited to the task. Prefer simple Python. Avoid speculative abstractions, unrelated refactors, and large frameworks without explicit approval.
- For new features, runtime flow, filesystem writes, Gmail integration, manifest changes, or broad refactors, first outline the intended change, affected files, risks, and test plan. Explain risky architecture decisions before implementation. Tiny documentation fixes need no formal plan.
- Keep Gmail integration separate from business logic, filesystem writing isolated, and runtime modes separate from providers.
- Preserve dry-run behavior for risky operations. Never overwrite existing user files; prefer append-only operations where suitable.
- For future daily and backfill work, account for missed days and resumability. Manifest tracking must prevent duplicate downloads; filenames must remain deterministic and collision-safe. These are requirements, not claims that dedicated daily or backfill modes exist.

## Data and testing

- Never hardcode or commit credentials, tokens, donor data, or real receipts. Respect `.gitignore`; keep real data out of prompts, review material, tests, and logs.
- Prefer deterministic local tests, fake data, mocked providers, and harness coverage before manual Gmail checks. Add meaningful tests for behavior changes, especially critical file operations and recurring faults. Do not require real Gmail for automated tests.
- Before running a command, check whether it reads or changes real data or uses an external service. A Gmail dry run still accesses Gmail. Do not run Gmail or destructive rebuilds as part of an ordinary test gate.
- Report tests as **passed**, **failed**, or **not run**, with command and environment limits. Do not present setup errors as product failures or partial runs as full passes.
- The full suite last passed on Python 3.14.5 and pytest 9.0.3: **315 passed** with a fresh `--basetemp`. From the repo root in Windows PowerShell, a normal run can use:

  ```powershell
  $parent = Join-Path (Get-Location) ("tmp/pytest-parent-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $parent -ErrorAction Stop | Out-Null
  .\.venv\Scripts\python.exe -m pytest -q --basetemp (Join-Path $parent 'run')
  ```

  Use a new isolated parent and `--basetemp` for every run; pytest may clean its base directory. After runs, inspect old `tmp/pytest-parent-*` directories and remove only those confirmed to belong to completed test runs; never use a blanket deletion or remove a directory during an active run. In the restricted agent environment, `tmp_path` setup raised `PermissionError` even with a fresh path; the suite passed with filesystem access outside that restriction. The successful review run used `$env:PYTHONDONTWRITEBYTECODE='1'`, `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'`, and `-p no:cacheprovider --tb=line` in addition to the command above. These review-specific switches are not standing requirements.

## Sessions and roles

- At session start, inspect branch and working tree, preserve existing changes, read this file and relevant code, and state the task and success criteria. Resolve missing context or reproduction steps before escalating to a stronger model.
- Codex implements. Claude independently reviews the requirement, a defined commit or diff including untracked files, and reported test results in a clean context. `docs/CLAUDE_WORKFLOW.md` specifies the review handoff.
- At session close, record decisions, exact version reviewed, files changed, test results, remaining risks, and next step in the task handoff or appropriate project document. Before compacting or changing sessions, carry forward that concise summary. A reviewer starts from the saved handoff and repo state, not the implementation conversation.
- Codex evaluates findings, fixes justified issues, reruns relevant checks, and requests another review when fixes materially change the reviewed version.

## Models

Before each future stage, recommend a model and reasoning level briefly, based on complexity and available context. This is a proposed policy, subject to actual tool availability:

- Sol Light: mapping, summaries, simple tasks.
- Sol medium: focused planning, implementation, tests.
- Sol high or Astra: complex investigation or architecture; return to the usual working model after the decision.
- Claude Sonnet: independent review of significant changes; Claude Opus only for unusually complex review.
- Local Qwen: bounded helper work after a measurable quality check.

The model names above are recommendations, not a claim that those models or reasoning levels are available in the current tools. A Markdown instruction does not switch a model automatically. Distinguish written policy from actual tool settings and checks enforced by Hooks or CI; do not claim enforcement without configuration that performs it.

## Git and authorization

- Use a focused branch and small commits. The sequence is **branch → implementation → relevant tests → local commit → independent review → justified fixes and repeated tests → PR → merge**. Review an identified version; update the identifier after changes.
- A local commit, push, PR creation, merge, and deployment are separate actions with separate effects. Deployment is not implied by merge. Follow authorization already given for an action; do not ask again for the same approved action. Do not infer authorization for a later external or destructive action from approval of an earlier step.
- Check the diff, unexpected files, secrets exposure, and `.gitignore` before sharing a version for review. Keep the test report explicit even when a check cannot run.
