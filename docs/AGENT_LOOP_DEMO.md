# Supervised fictional CLI demo

`python -m agent_loop` remains the Mock command. This separate demo uses
`agent_loop.core.run_loop` with `DemoCodex`, `DemoTests`, and `DemoClaude` from
`agent_loop/demo.py`. It does not instantiate `LiveCodex`, `LiveClaude`, or
`LivePytest`; `LIVE_ISOLATION_VERIFIED` remains `False`.

From a VS Code PowerShell terminal at the repository root, run one command:

```powershell
.\.venv\Scripts\python.exe -m agent_loop.demo --run-models
```

The command first requires typing `START`. Before **each** test run it prints
the complete proposed function and requires typing `RUN`. Any other response
stops before executing that code. The default invocation without
`--run-models` exits before starting a model. The task and tests are fixed in
the runner; no task path, repository path, or model-generated command is
accepted. The temporary demo directory is created by Python outside this
repository and removed when the run ends.

The fictional task is `normalize_username(username)`: trim outer whitespace,
Unicode casefold, return an empty string for whitespace-only input, and raise
`TypeError` for non-strings. Codex returns only a JSON `code` string. The
runner accepts a single restricted Python function, shows it for approval,
writes `example.py`, and runs fixed pytest cases. Claude receives that code,
the task, and test counts as text and returns `approved` or
`changes_requested` with structured findings. The core supplies review or
test feedback to Codex in the next round and enforces a maximum of three
rounds. The JSON report lists every round, test result, review, and stop
reason. Exit code 0 means approved after passing tests; 1 means the demo
stopped without approval. No commit, push, PR, or merge occurs.

The installed CLI versions inspected for these flags were `codex-cli 0.163.0`
and Claude Code `2.1.280`. The runner rejects a different version until its
flags are checked again. Codex starts with `--sandbox read-only`, no approval
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
remain blocked. Actual model behavior and authentication have not been tested
by the implementation test suite; the first opt-in run needs human supervision.
