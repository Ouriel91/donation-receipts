# Independent Claude review

Follow the shared rules in `docs/RULES.md`. `CLAUDE.md` is the short entry point. Codex implements; Claude reviews significant changes independently in a fresh context.

## Review handoff

Give Claude the task goal and acceptance criteria, the base and target commit or exact local diff, a list of untracked files to read, relevant code and documents, and test commands with **passed / failed / not run** results. Do not send credentials, tokens, donor data, or real receipts. Use `docs/TASK_TEMPLATE.md` for the task record.

Claude compares the specified version with the requirement, inspects the full diff and relevant surrounding code, identifies concrete defects or missing checks, and distinguishes verified findings from uncertainties. Findings should cite files and lines, explain impact, and suggest a focused check when useful. If no findings remain, say what was reviewed and what could not be verified.

Codex assesses findings, makes justified fixes, reruns relevant tests, and supplies an updated version for another review if fixes materially change the result. Claude does not implement the task as part of this review unless explicitly assigned a separate implementation task.

## Context and session boundaries

Open review in a new conversation with the saved handoff and repo state. If context becomes stale or a session is compacted, retain the task, reviewed version, findings, test results, decisions, and next step. Recheck the current diff before resuming. Avoid a full project scan for every small review; read relevant files and `docs/RULES.md` first.
