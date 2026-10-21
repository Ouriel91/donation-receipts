# Supervised Git review

`python -m agent_loop.review` reviews a fixed pair of local Git commits. It
uses `core.run_loop` for one read-only round: Codex reviews the supplied
package, the evidence adapter reports existing test results, and Claude reviews
the same package and Codex's findings. A request for changes stops the round.
After a separate fix, run the command again with the new target commit SHA.

Run this from the repository root on the branch containing the review tool.
The base and target must be full local commit SHAs. For a PR, fetch its commits
separately and pass the chosen base and head SHAs; this command does not fetch,
push, create a PR, or modify the worktree.

## Preview and approval

```powershell
.\.venv\Scripts\python.exe -m agent_loop.review --base <full-base-sha> --target <full-target-sha>
```

This default command collects and displays the exact changed file names,
context file names, byte counts, and test evidence. It starts no model. The
runner rejects empty, binary, linked, oversized, sensitive-path, and
secret-pattern diffs. It reads committed objects only and uses a temporary
working directory outside the repository. The default test status is
`not_run`; the runner never executes tests.

Supply existing test evidence when available, then opt in:

```powershell
.\.venv\Scripts\python.exe -m agent_loop.review --base <full-base-sha> --target <full-target-sha> --test-status passed --test-detail "420 passed, 2 skipped on this target" --run-models
```

The test result text is supplied by the caller and is not independently
verified. Before a model starts, inspect the displayed scope and type `START`
to approve sending that bounded package. Any other answer cancels the review.
Review the source diff yourself when checking for sensitive information: a
pattern scanner cannot identify every donor name or private value.

The runner uses the installed CLI versions checked by the fictional demo
(`codex-cli 0.163.0` and Claude Code `2.1.280`). Codex uses read-only mode
with file and command tool features disabled. Claude uses restricted, safe mode
with an empty tool list, no permission prompts, and no MCP servers. Both run
from a temporary directory, receive only the approved package, and have bounded
output and a 180-second timeout. A changed CLI version stops the run until its
flags are inspected again. **Running either CLI as the normal Windows user is
not OS isolation.** The general Live adapters remain gated by
`LIVE_ISOLATION_VERIFIED=False` and are not used here.

The JSON report on stdout contains each reviewer's Critical, Major, and Minor
findings, file and line references, a recommendation, each finding's status,
test evidence, the Core report, and one decision: `Approved`,
`Changes Requested`, or `Human Decision`. Critical or Major findings from
either reviewer require changes; uncertain findings require a human decision.
CLI errors, invalid JSON, and failed test evidence cannot yield approval.
This workflow never runs model-generated commands, edits product code, or
commits changes.

## First two reviews

These commands are ready to run from a VS Code PowerShell terminal. Each first
shows its package and waits for `START` before calling models.

```powershell
.\.venv\Scripts\python.exe -m agent_loop.review --base f373904574baaa9d1dc5ee245f1deb328078590c --target 354e203d2b961f15a03680ac750e01beb4ebb87e --test-status passed --test-detail "420 passed, 2 skipped; full tests/ suite" --run-models
```

```powershell
.\.venv\Scripts\python.exe -m agent_loop.review --base f373904574baaa9d1dc5ee245f1deb328078590c --target 03287f70406a6b8d62f057bbe4d63476958daa56 --test-status passed --test-detail "22 passed; focused CLI documentation checks" --run-models
```

The README commit is a sibling of the demo commit. Its own tree does not
contain the demo files it references, so review that dependency explicitly and
integrate the demo before the README if both are accepted.

## Implementation review handoff

- Branch: `feat/agent-loop-git-review`, starting at demo commit `354e203`.
- Review version: the changes to `agent_loop/core.py` plus
  `agent_loop/review.py`, `tests/test_agent_loop_review.py`, and this document
  relative to `354e203`. Use the branch tip for the exact review SHA.
- Checks: focused Core, demo, and review tests **passed** (54); full `tests/`
  suite **passed** (437 passed, 2 skipped). Each pytest run used a fresh short
  `--basetemp` with model plugins disabled. Both target packages also passed
  preview-only collection. No model or Gmail session was started.
- Independent review should examine Git object selection, path and content
  screening, the START gate, CLI flags, error decisions, line references, and
  whether the two real packages supply enough context. The first real model
  execution remains a supervised user action. Project Live adapters remain
  blocked.
