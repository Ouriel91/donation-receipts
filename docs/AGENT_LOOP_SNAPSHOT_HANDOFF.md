# Claude review handoff: snapshot integrity, N1 and M1

## Task and exact version

- Branch: `fix/agent-loop-snapshot`; base: `main` at `24e108b`.
- Target: current uncommitted working tree against that base. Read this untracked handoff separately from `git diff`.
- Scope: accurate, bounded review snapshots only. No model call, Gmail access, product-code change, dependency installation, commit, push, or merge.
- Modified files: `agent_loop/live.py`, `tests/test_agent_loop_live.py`, `docs/AGENT_LOOP_LIVE.md`. New file: this handoff.
- SHA-256: `live.py` `105100F4825B7294EF57A9499084ED1FF5070A95446DE338D15248819A3D3DC0`; `test_agent_loop_live.py` `75ED1C0E2E7941B9F25EA44A1BAA7F01B200F8EC8CC44ADE6E3405FCF260AF2B`; `AGENT_LOOP_LIVE.md` `2B180A2C86611CD9DA50ED9FD51EBFFA4EC6A25FCB73BA56385197B77D3972EB`.

## Behavior to review

- Tracked changes use a Git diff against the full supplied base commit. Normal new files use Git's `--exclude-standard` rules, which keep ignored `.venv` trees out of the main file list. A relevant ignored file requires explicit selection and the existing path, symlink, size, and content checks.
- Before a package is accepted, `git ls-files --others --ignored --exclude-standard --directory -z` reports ignored paths with whole ignored directories collapsed to single entries. Only a fixed runtime allowlist, the fictional-workspace marker, and explicitly selected ignored files are accepted. Unknown ignored paths raise `ReviewIntegrityError` with no path or content in the error.
- This also blocks a new `.gitignore` that hides itself or a changed tracked `.gitignore` that hides a new source file. Runtime paths such as root `.venv`, root `tmp`, and `__pycache__` are exempt by fixed code policy; they are not inspected as source.
- Two matching snapshots are still required before each Claude review. The Orchestrator converts collection failure to `tool_error` with `ReviewIntegrityError`, never an approved review. `LIVE_ISOLATION_VERIFIED` remains `False` and the project entry point remains Mock.

## Verification

- **passed:** `python -m pytest -q tests/test_agent_loop_live.py` with a fresh `--basetemp`: 34 passed.
- **passed:** `python -m pytest -q` with another fresh `--basetemp`: 374 passed, including the Mock tests.
- Both runs used the project `.venv` on Windows, `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-p no:cacheprovider`, and `--tb=line`.
- Real Git tests cover a large ignored `.venv`, root `tmp` and `__pycache__`, a new Python file that reaches review, a visible new `.gitignore`, a changed tracked `.gitignore` hiding Python, and a nested new `.gitignore` containing `*` that hides itself and Python. The last case verifies that Claude is not started and the loop cannot approve.
- **not run:** Codex CLI, Claude CLI, Gmail, or a live Orchestrator cycle.

## Review focus and remaining limits

Check whether directory-level ignored listing catches hidden source and hidden `.gitignore` files under Git's real semantics, including pathspec and symlink edge cases. Check the fixed runtime exemptions, output/time limits, explicit ignored-file path, snapshot freshness, and safe error reporting. Cite file and line for concrete defects; distinguish commit blockers from later live-operation risks.

The runtime allowlist deliberately omits its own contents. S1 (source hidden inside approved runtime directories) and S2 (tracked changes hidden by Git index flags such as `assume-unchanged`) are documented in `docs/AGENT_LOOP_LIVE.md` as live-operation blockers. Excessive nonruntime ignored output fails closed at the runner cap. The content filter cannot certify arbitrary text as free of personal information. Windows host isolation, process environment filtering, effective CLI permissions, and prompt-injection defenses remain separate prerequisites before any gate-opening change.

Delivery state at review: local commit **not done**; push **not done**; PR **not done**; merge **not done**.
