# Claude review handoff: Real Agent Loop phase 1, focused fixes

## Task and version

- Branch: `feat/agent-loop-live`; base commit: `90a30eb`.
- Target: current uncommitted working tree. Read untracked files in full; `git diff` alone does not include them.
- Original review raised M1 (stale review package) and M2 (Windows timeout can leave pipe-holding descendants). This revision addresses those findings and a Minor `LivePytest` error classification issue.
- No commit, push, PR, merge, model CLI call, Gmail access, or donor-data access occurred.

## Files to review

- Tracked change: `docs/AGENT_LOOP.md`.
- Untracked: `agent_loop/live.py`, `agent_loop/windows_job.py`, `tests/test_agent_loop_live.py`, `docs/AGENT_LOOP_LIVE.md`, and this handoff.
- Relevant surrounding code: `agent_loop/core.py`, `agent_loop/mock.py`, `agent_loop/__main__.py`, `tests/test_agent_loop.py`, `docs/RULES.md`, `docs/CLAUDE_WORKFLOW.md`.
- SHA-256 of the implementation and test inputs (excluding this handoff):
  - `live.py`: `05F918E7357745A4E9BB27021970C59B05AD3EE79344600DC8EBB3ADB88DD93D`
  - `windows_job.py`: `9B2720925FF9A0F773A73CFD1BF0F73201B59B5548096F5FCD554D06A59CAE7F`
  - `test_agent_loop_live.py`: `8094092CB15366AC4F65E01840FBC4C759E6D47E8602F5374DD1F5CF908EE691`
  - `AGENT_LOOP_LIVE.md`: `A9A01571D3C8103BADF422F651AF9496E63781A371C69FF45F9141BDC23FD99E`
  - `AGENT_LOOP.md`: `0179C421DD9644EB22F44A395EE58CD3E3A26DAE744064ABDFBF803DB7FA3084`

## Implementation decisions

- `LiveClaude.review` gathers a fresh Git diff against a full specified base commit immediately before each review. It reads new untracked files, including ignored source files outside known runtime directories, and requires two identical consecutive snapshots. Git errors, unsafe paths or content, excessive size, or concurrent changes abort before Claude starts. The existing Orchestrator protocol remains unchanged.
- The subprocess runner starts Windows processes suspended, assigns them to a Job Object with kill-on-close, then resumes them. Timeout, cleanup wait, and stream-reader joins are bounded. Job setup or cleanup failure is reported as adapter failure. The Job owns only the processes launched by this runner; no broad process kill is used.
- `LivePytest` now distinguishes stdout overflow (`OutputLimitError`) from unrelated `ValueError` causes. Returned messages are fixed strings without raw stdout or stderr.
- `LIVE_ISOLATION_VERIFIED` remains `False`; `python -m agent_loop` remains Mock only.

## Verification

- **passed**: `python -m pytest -q tests/test_agent_loop_live.py` with a fresh `--basetemp`: 29 passed. This includes a local Windows Python parent that starts a pipe-holding child; timeout returned within 8 seconds and the child was no longer active.
- **passed**: `python -m pytest -q` with another fresh `--basetemp`: 369 passed. This includes the existing Mock tests.
- Python: project `.venv` on Windows. Both runs used `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-p no:cacheprovider`, and `--tb=line` for this review environment.
- **not run**: real Codex CLI, real Claude CLI, or a live Orchestrator cycle. No verified isolated guest exists and the gate is closed.

## Review focus and remaining limits

Please check whether each Claude round receives current tracked and untracked changes, whether snapshot failure can reach the model, and whether the path/content filters fail safely. Check Windows Job Object ownership, failure paths, bounded waits and joins, and whether the local child-process test supports the claimed behavior. Review the Minor error-classification change and Mock compatibility.

The marker, CLI tool flags, and Job Object are not host filesystem isolation. Full Windows guest isolation, process environment filtering, effective CLI permissions, and prompt-injection resistance belong to the separate gate-opening task. The content filter rejects known sensitive patterns but cannot certify arbitrary project text. Processes spawned through services or other out-of-job paths were not tested. The live gate must stay closed.

N1 was documented after the review: scanning ignored files can exceed the runner's stdout limit before `.venv` is filtered. Phase 2 must resolve this before live operation.

Review findings should cite file and line, classify confirmed defects separately from unverified limits, and state whether each finding blocks a local commit or only live operation.

Delivery state at review: local commit **not done**; push **not done**; PR **not done**; merge **not done**.
