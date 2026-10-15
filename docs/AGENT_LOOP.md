# Mock Agent Loop

This MVP simulates Codex implementation, pytest results, and Claude review. It
does not start models, Gmail, or product code. The command accepts only a
Markdown task inside `agent_loop/tasks/` and prints a JSON report to stdout.

From the repository root in Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m agent_loop --task example.md --scenario success
.\.venv\Scripts\python.exe -m agent_loop --task example.md --scenario review_then_pass
```

Available scenarios: `success`, `test_then_pass`, `review_then_pass`, `limit`,
`invalid_json`, and `human`. `--max-rounds` accepts 1 to 3, with 3 as the
default. A successful loop exits 0; a completed loop that stops for a failure,
invalid output, human decision, or round limit exits 1. Invalid command usage,
task paths, or loop configuration exit 2 without a JSON report.
No files are written by the command.

The review contract is JSON with `status` (`approved`, `changes_requested`, or
`human_decision`) and a `findings` array. Each finding has nonempty `id`,
`summary`, and `kind` (`fix`, `non_blocking`, or `human_decision`). Status must
agree with the most urgent finding kind. Failed tests skip review and send
test feedback into the next round. `not_run`, invalid output, tool failure,
human decisions, and reaching the round limit stop safely.

The JSON report contains `stop_reason`, `next_step`, and a `rounds` list. On
adapter failure or invalid review JSON it also contains optional `error` with
only the exception type (for example, `RuntimeError` or `JSONDecodeError`).
Raw exception messages are excluded because they may contain sensitive text.

The core uses adapter protocols and phase 1 CLI adapters have been prepared.
They are not connected to this Mock command. Before connecting them, provide
an isolated workspace without product data, enforce filesystem and command
permissions on Windows, verify those controls, and sanitize review inputs and
logs. This MVP does not perform commit, push, or merge.

Phase 1 CLI adapter design and its closed live execution gate are documented in
[`AGENT_LOOP_LIVE.md`](AGENT_LOOP_LIVE.md). This command still runs Mock only.
