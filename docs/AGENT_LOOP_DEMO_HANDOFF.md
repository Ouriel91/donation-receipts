# Agent loop demo task and review handoff

## Task

- Goal: connect the prior fictional Codex/pytest/Claude exchange to the
  existing Orchestrator without opening the production Live gate.
- In scope: one fixed `normalize_username` demo, CLI text adapters, human test
  approval, bounded process execution, structured review, fake adapter tests,
  and usage documentation.
- Out of scope: donation project files and data, Gmail, Live adapters, OS
  isolation work, real model execution during implementation, and Git delivery.
- Expected files: `agent_loop/demo.py`, a small optional process-runner
  dependency injection point in `agent_loop/live.py`,
  `tests/test_agent_loop_demo.py`, and demo documentation.
- Success: at most three core-managed rounds; approved and fix-requested
  reviews; automated feedback; human approval before running model code;
  ordinary tests start no models.
- Risks: execution under the ordinary Windows user is not OS isolation.
  Installed CLI flags can change, so the runner pins inspected versions.

## Opening state

- Starting branch: `feat/agent-loop-isolation` at `a7f3ecb`, clean tree.
- Fetched `origin/main`, fast-forwarded local `main` from `ad640a3` to
  `f373904`, then created `feat/agent-loop-demo` from `f373904`.
- Read `AGENTS.md`, project rules and task template, Claude workflow,
  `docs/AGENT_LOOP.md`, `docs/AGENT_LOOP_LIVE.md`, isolation notes, core and
  adapters, and relevant testing, secrets, and filesystem guidance.
- Recommended implementation model: Sol medium, because the work is focused
  adapter and test wiring with an existing Core contract.

## Design and review version

- `run_loop` owns round count, test failure behavior, review parsing, feedback,
  and report construction. The demo adapters only implement its three
  protocols. `live._run` is reused for bounded processes with an optional
  demo child environment; existing Live calls retain their original default.
- `pytest.ini` now collects from `tests/` so old temporary fictional tests
  under `tmp/pytest-parent-*` cannot enter a later full run.
- The demo supplies an explicit CLI opt-in, a terminal `START` gate, and a
  `RUN` gate for every generated function before saving or testing it.
- Version for independent Claude review: local diff against `f373904` plus
  the untracked files listed by `git status`. No commit is authorized.
- Review should check the opt-in and human approval path, CLI argument order
  and restrictions, AST filter, temp path scope, result parsing, failure
  behavior, and whether all model output reaching reports is bounded.

## Test and delivery record

- Focused test: `python -m pytest -q -p no:cacheprovider --tb=short
  --basetemp <fresh>/run tests/test_agent_loop_demo.py tests/test_agent_loop.py
  tests/test_agent_loop_live.py` — **passed**, 72 tests on the final diff.
- Full test: same flags without test paths — **passed**, 420 tests, 2 skipped
  on the final diff.
- Both final pytest commands ran with `PYTHONDONTWRITEBYTECODE=1` and
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` outside the restricted agent filesystem
  sandbox. Each used a new parent and `--basetemp` under repository `tmp/`.
- Earlier full run: **failed during collection**, because old basetemp copies
  of `test_example.py` under `tmp/` shared a module name. The `testpaths`
  change addresses this collection issue; the subsequent full run passed.
- Real model run: **not run** by instruction.
- Commit: not authorized. Push: not authorized. PR: not authorized. Merge:
  not authorized.
- Next stage: independent Claude review of this exact local diff and test
  report, then a separately authorized supervised CLI demo run.
