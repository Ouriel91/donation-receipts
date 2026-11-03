# Supervised fictional CLI demo

`python -m agent_loop` remains the Mock command. This separate demo uses
`agent_loop.core.run_loop` with `DemoCodex`, `DemoTests`, and `DemoClaude` from
`agent_loop/demo.py`. It does not instantiate `LiveCodex`, `LiveClaude`, or
`LivePytest`; `LIVE_ISOLATION_VERIFIED` remains `False`.

From a VS Code PowerShell terminal at the repository root, choose one command:

```powershell
.\.venv\Scripts\python.exe -m agent_loop.demo --run-models
.\.venv\Scripts\python.exe -m agent_loop.demo --task section46 --run-models
```

The first command keeps the original username task. The second runs a
synthetic section 46 date-range task. Run one command at a time.

The command first requires typing `START`. Before **each** test run it prints
the complete proposed function and requires typing `RUN`. Any other response
stops before executing that code. The default invocation without
`--run-models` exits before starting a model. Both task choices and their
tests are fixed in the runner; no task path, repository path, or
model-generated command is accepted. The temporary demo directory is created
by Python outside this repository and removed when the run ends.

The fictional task is `normalize_username(username)`: trim outer whitespace,
Unicode casefold, return an empty string for whitespace-only input, and raise
`TypeError` for non-strings. The section 46 task is
`is_donation_in_approval_period(donation_date, start_date, end_date)`. It
accepts `datetime.date` objects, includes both approval endpoints, raises
`TypeError` for other input types (including date strings), and raises
`ValueError` for a reversed period. Its fixed tests cover both endpoints,
dates outside the range, a single leap day, invalid input types, and a
reversed period. The runner supplies `date` from a fixed import. For this
task, the syntax filter permits date comparisons but still rejects model
imports, attribute access, and extra calls. The username filter is unchanged.

For either task, Codex returns only a JSON `code` string. The runner accepts
a single restricted Python function, shows it for approval,
writes `example.py`, and runs fixed pytest cases. Claude receives that code,
the task, and test counts as text and returns `approved` or
`changes_requested` with structured findings. The core supplies review or
test feedback to Codex in the next round and enforces a maximum of three
rounds. The JSON report lists every round, test result, review, and stop
reason. Exit code 0 means approved after passing tests; 1 means the demo
stopped without approval. No commit, push, PR, or merge occurs.

The checked CLI versions are `codex-cli 0.163.0` and Claude Code `2.1.280` or
`2.1.316`. The local `claude --help` for `2.1.316` lists every flag used by
`DemoClaude`; the [CLI reference](https://code.claude.com/docs/en/cli-reference)
documents the same tool and permission behavior, and the
[2.1.316 changelog](https://code.claude.com/docs/en/changelog) records a fix
that tightens `--restricted` file access. The runner rejects any other version
until its flags are checked again. Codex starts with `--sandbox read-only`, no approval
escalation, and disabled shell, code host, browser, computer, apps, plugins,
and multi-agent features. Claude starts with `--tools ""`, restricted and safe
modes, denied MCP tools, and no permission prompts. Both are launched with
fixed argument arrays and bounded process lifetime and output. The runner
passes only a small runtime environment and uses the CLIs' existing login;
it does not read project credentials or send project files to either model.

**This is not OS isolation.** Both processes run under the ordinary Windows
user. CLI flags and a Python syntax filter narrow this specific demo but
cannot prove that the processes cannot read other files accessible to that
user. The separate production Live adapters and their Windows isolation gate
remain blocked. The implementation test suite does not start either real
model; every opt-in run needs human supervision.

The proposed function is emitted to the approval terminal as UTF-8 bytes and
written to `example.py` without newline conversion. For the section 46 task,
the file also has a fixed `from datetime import date` prefix; the function
bytes after that prefix match the displayed function. The runner compares the
file before and after pytest and checks the tested function against the code
sent to Claude. A mismatch stops the loop before approval. The report's
`implementation` is JSON-escaped on screen; decode the JSON value to compare
its source bytes with the displayed function.

## Demo stage closure handoff

- Base: `main` at `78d4aaa`; review the uncommitted diff in
  `agent_loop/demo.py`, `tests/test_agent_loop_demo.py`, and this document.
- The selected task changes only the fixed prompt, tests, and trusted source
  prefix. Both choices use the existing adapters and `run_loop`.
- Focused pytest: **passed**, 47 tests in `tests/test_agent_loop_demo.py` and
  `tests/test_agent_loop.py` with a fresh `--basetemp`, bytecode and plugin
  autoload disabled, and `-p no:cacheprovider`. The first run inside the
  restricted agent filesystem failed during temporary-directory setup; the
  successful run used filesystem access outside that restriction.
- Compatibility update: **passed**, 85 tests in `tests/test_agent_loop_demo.py`,
  `tests/test_agent_loop.py`, and `tests/test_agent_loop_live.py` with a fresh
  `--basetemp` and the same review switches. Both accepted Claude versions
  retain the exact tool restriction arguments; an unlisted version stops
  before a model call.
- Source integrity update: **passed**, 53 focused tests in
  `tests/test_agent_loop_demo.py` and `tests/test_agent_loop.py` with a fresh
  `--basetemp` and the same review switches. A fake CLI checks byte identity
  across display, pytest, Claude, and decoded report; an additional local
  pytest run executes only predefined fictional section 46 code. Mismatched
  report code and changed test source cannot produce `success`.
- Final full pytest: **passed**, 792 tests; 2 skipped; 1 `fitz` deprecation
  warning. Run from the repo root with a fresh `tmp/p-<UUID8>` `--basetemp`,
  `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`,
  `-p no:cacheprovider`, and `--tb=short`. Both fixed demo tasks are covered.
- The user reported a supervised real `section46` run with 7 passing tests,
  Claude approval, and one round. No real model was started during this
  closure check. The reviewed version is the local diff against `78d4aaa`;
  there are no untracked files. Next: one explicitly authorized local commit
  and an independent review of that version. Further demo tasks are out of
  scope. No commit, push, PR, or merge was performed here.
