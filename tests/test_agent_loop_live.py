"""No model or real pytest process is started by these adapter tests."""

import json
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_loop.core import TestResult as LoopTestResult, run_loop
from agent_loop.live import (LiveClaude, LiveCodex, LivePytest, OutputLimitError,
                             ReviewPackage, collect_review_package)


BASE = "a" * 40


def _fixed_package(monkeypatch, package):
    monkeypatch.setattr("agent_loop.live.collect_review_package",
                        lambda root, requirements, base: package)


class _Input(io.BytesIO):
    def close(self):
        # Keep bytes available for assertions after the fake process exits.
        pass


def _mock_process(monkeypatch, handler):
    processes = []

    class FakeProcess:
        def __init__(self, args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.stdin = _Input()
            self.error = None
            try:
                result = handler(args, **kwargs)
            except subprocess.TimeoutExpired as exc:
                self.error = exc
                result = subprocess.CompletedProcess(args, 1, "", "")
            self.returncode = result.returncode
            self.stdout = io.BytesIO(result.stdout.encode())
            self.stderr = io.BytesIO(result.stderr.encode())
            self.wait_timeouts = []
            processes.append(self)

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if self.error:
                raise self.error
            return self.returncode

        def kill(self):
            self.error = None

        def terminate_tree(self):
            self.error = None

        def close_tree(self):
            pass

    monkeypatch.setattr("agent_loop.live._start_process", lambda args, cwd: FakeProcess(args, cwd=cwd))
    return processes


@pytest.fixture
def fictional(tmp_path, monkeypatch):
    root = tmp_path / "fictional"
    root.mkdir()
    (root / ".fictional-agent-workspace").write_text("fiction only", encoding="utf-8")
    python = root / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr("agent_loop.live.LIVE_ISOLATION_VERIFIED", True)
    return root, python


def test_gate_blocks_all_subprocesses(tmp_path, monkeypatch):
    _mock_process(monkeypatch, lambda *a, **k: pytest.fail("CLI started"))
    root = tmp_path / "fictional"
    root.mkdir()
    (root / ".fictional-agent-workspace").touch()
    for call in (
        lambda: LiveCodex(root).run("fake task", None),
        lambda: LivePytest(root, root / "python.exe").run(),
        lambda: LiveClaude(root, "fake requirement", BASE).review(
            "fake", "done", LoopTestResult("passed", "1 passed")),
    ):
        with pytest.raises(PermissionError, match="blocked"):
            call()


def test_marker_is_required_even_with_test_gate(tmp_path, monkeypatch):
    root = tmp_path / "unmarked"
    root.mkdir()
    monkeypatch.setattr("agent_loop.live.LIVE_ISOLATION_VERIFIED", True)
    _mock_process(monkeypatch, lambda *a, **k: pytest.fail("CLI started"))
    with pytest.raises(ValueError, match="marked fictional"):
        LiveCodex(root).run("fictional", None)


def test_codex_schema_prompt_and_feedback(fictional, monkeypatch):
    root, _ = fictional
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        assert schema["required"] == ["summary"]
        Path(args[args.index("--output-last-message") + 1]).write_text(
            json.dumps({"summary": "Made a fictional edit"}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    processes = _mock_process(monkeypatch, fake_run)
    adapter = LiveCodex(root)
    assert adapter.run("fictional task", "Fix fake failure") == "Made a fictional edit"
    args, kwargs = seen[0]
    assert args[:2] == ["codex", "exec"]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[args.index("--ask-for-approval") + 1] == "never"
    assert json.loads(processes[0].stdin.getvalue())["previous_feedback"] == "Fix fake failure"
    assert kwargs["cwd"] == root and processes[0].wait_timeouts[0] == 600


@pytest.mark.parametrize("answer,code,error", [
    (None, 0, ValueError),
    ("not json", 0, ValueError),
    ('{"summary":""}', 0, ValueError),
    ('{"summary":"ok","extra":1}', 0, ValueError),
    ('{"summary":"ok"}', 2, RuntimeError),
])
def test_codex_rejects_missing_invalid_and_failed_output(fictional, monkeypatch, answer, code, error):
    root, _ = fictional

    def fake_run(args, **kwargs):
        if answer is not None:
            Path(args[args.index("--output-last-message") + 1]).write_text(answer)
        return subprocess.CompletedProcess(args, code, "private stderr", "private stdout")

    _mock_process(monkeypatch, fake_run)
    with pytest.raises(error):
        LiveCodex(root).run("fictional task", None)


def test_pytest_uses_new_base_each_run_and_reports_counts(fictional, monkeypatch):
    root, python = fictional
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        code, failures = [(1, 1), (0, 0)][len(calls) - 1]
        Path(args[args.index("--junitxml") + 1]).write_text(
            f'<testsuites><testsuite tests="3" failures="{failures}" errors="0" skipped="0" /></testsuites>'
        )
        return subprocess.CompletedProcess(args, code, "test details", "private stderr")

    _mock_process(monkeypatch, fake_run)
    adapter = LivePytest(root, python, ("-q", "tests"))
    assert adapter.run() == LoopTestResult("failed", "2 passed, 1 failed, 0 errors, 0 skipped")
    assert adapter.run() == LoopTestResult("passed", "3 passed, 0 failed, 0 errors, 0 skipped")
    assert calls[0][0] == str(python) and calls[0][1:3] == ["-m", "pytest"]
    assert calls[0][-1] != calls[1][-1]
    assert all(Path(args[-1]).parent.is_dir() for args in calls)


@pytest.mark.parametrize("code,failures,expected", [
    (2, 0, "not_run"),
    (1, 1, "failed"),
    (0, 0, "passed"),
    (0, 1, "not_run"),
])
def test_pytest_exit_handling(fictional, monkeypatch, code, failures, expected):
    root, python = fictional
    def fake_run(args, **kw):
        Path(args[args.index("--junitxml") + 1]).write_text(
            f'<testsuite tests="1" failures="{failures}" errors="0" skipped="0" />'
        )
        return subprocess.CompletedProcess(args, code, "secret", "private stderr")
    _mock_process(monkeypatch, fake_run)
    assert LivePytest(root, python).run().status == expected


def test_pytest_rejects_missing_or_invalid_junit(fictional, monkeypatch):
    root, python = fictional
    def fake_run(args, **kwargs):
        Path(args[args.index("--junitxml") + 1]).write_text("<not-junit />")
        return subprocess.CompletedProcess(args, 0, "1 passed in 0.01s", "")
    _mock_process(monkeypatch, fake_run)
    assert LivePytest(root, python).run().status == "not_run"


@pytest.mark.parametrize("failure,detail", [
    (OutputLimitError("private stdout"), "pytest output exceeded the limit"),
    (ValueError("private invalid UTF-8"), "pytest output or timeout configuration is invalid"),
])
def test_pytest_distinguishes_output_limit_from_other_value_error(
    fictional, monkeypatch, failure, detail
):
    root, python = fictional
    def fail_run(*args, **kwargs):
        raise failure
    monkeypatch.setattr("agent_loop.live._run", fail_run)
    assert LivePytest(root, python).run() == LoopTestResult("not_run", detail)
    assert "private" not in detail


def test_claude_receives_package_with_read_only_tools(fictional, monkeypatch):
    root, _ = fictional
    package = ReviewPackage("fictional requirement", "fictional change", {"new.py": "x = 1"})
    _fixed_package(monkeypatch, package)
    review = {"status": "approved", "findings": []}
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0,
            json.dumps({"is_error": False, "structured_output": review}), "")

    processes = _mock_process(monkeypatch, fake_run)
    assert json.loads(LiveClaude(root, "fictional requirement", BASE).review(
        "fictional task", "done", LoopTestResult("passed", "1 passed in 0.01s"))) == review
    args, kwargs = seen[0]
    assert "--restricted" in args and "--safe-mode" in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    assert "--strict-mcp-config" in args
    assert "--no-session-persistence" in args
    assert json.loads(processes[0].stdin.getvalue().decode().split("\n", 1)[1])["new_files"] == {"new.py": "x = 1"}


@pytest.mark.parametrize("envelope", [
    {}, {"is_error": True, "structured_output": {"status": "approved", "findings": []}},
    {"is_error": False, "structured_output": {"status": "approved", "findings": "none"}},
])
def test_claude_rejects_invalid_envelope(fictional, monkeypatch, envelope):
    root, _ = fictional
    _fixed_package(monkeypatch, ReviewPackage("r", "c", {}))
    _mock_process(monkeypatch, lambda args, **kw:
                  subprocess.CompletedProcess(args, 0, json.dumps(envelope), ""))
    with pytest.raises(ValueError, match="review"):
        LiveClaude(root, "r", BASE).review("t", "i", LoopTestResult("passed", "1 passed"))


def test_timeout_and_large_output_do_not_enter_report(fictional, monkeypatch):
    root, python = fictional
    _fixed_package(monkeypatch, ReviewPackage("r", "c", {}))
    secret = "private-credential-value"
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1, output=secret, stderr=secret)
    _mock_process(monkeypatch, timeout)
    report = run_loop("fictional", LiveCodex(root), LivePytest(root, python),
                      LiveClaude(root, "r", BASE))
    assert report.stop_reason == "tool_error"
    assert secret not in json.dumps(report.to_dict())

    def too_large(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "x" * 70000, secret)
    _mock_process(monkeypatch, too_large)
    with pytest.raises(ValueError, match="too large"):
        LiveCodex(root).run("fictional", None)


def test_adapters_fit_existing_loop_with_fake_processes(fictional, monkeypatch):
    root, python = fictional
    package = ReviewPackage("fictional requirement", "fictional diff", {"new.py": "x = 1"})
    _fixed_package(monkeypatch, package)

    def fake_run(args, **kwargs):
        if args[0] == "codex":
            Path(args[args.index("--output-last-message") + 1]).write_text(
                '{"summary":"fictional edit"}'
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == str(python):
            Path(args[args.index("--junitxml") + 1]).write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0" />'
            )
            return subprocess.CompletedProcess(args, 0, "ignored test output", "")
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "is_error": False,
            "structured_output": {"status": "approved", "findings": []},
        }), "")

    processes = _mock_process(monkeypatch, fake_run)
    report = run_loop("fictional task", LiveCodex(root), LivePytest(root, python),
                      LiveClaude(root, "fictional requirement", BASE))
    assert report.stop_reason == "success"
    assert [process.args[0] for process in processes] == ["codex", str(python), "claude"]


def test_second_round_reviews_fresh_diff_and_untracked_content(fictional, monkeypatch):
    root, python = fictional
    (root / "code.py").write_text("old")
    rounds = []

    def fake_git(where, *args):
        assert where == root
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root) + "\n"
        if args[0] == "rev-parse":
            return BASE + "\n"
        if args[0] == "ls-files":
            return "new.py\0"
        if "--name-only" in args:
            return "code.py\0"
        return "diff --git a/code.py b/code.py\n+" + (root / "code.py").read_text() + "\n"

    monkeypatch.setattr("agent_loop.live._git", fake_git)

    def fake_process(args, **kwargs):
        if args[0] == "codex":
            number = 1 + sum(process.args[0] == "codex" for process in processes)
            (root / "code.py").write_text(f"version {number}")
            (root / "new.py").write_text(f"new version {number}")
            Path(args[args.index("--output-last-message") + 1]).write_text(
                json.dumps({"summary": f"round {number}"})
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == str(python):
            Path(args[args.index("--junitxml") + 1]).write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0" />'
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        status = "changes_requested" if not rounds else "approved"
        rounds.append(status)
        findings = ([{"id": "F1", "kind": "fix", "summary": "fix fictional issue"}]
                    if status == "changes_requested" else [])
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "is_error": False, "structured_output": {"status": status, "findings": findings}
        }), "")

    processes = _mock_process(monkeypatch, fake_process)
    report = run_loop("fictional task", LiveCodex(root), LivePytest(root, python),
                      LiveClaude(root, "fictional requirement", BASE))
    assert report.stop_reason == "success"
    reviews = [json.loads(process.stdin.getvalue().decode().split("\n", 1)[1])
               for process in processes if process.args[0] == "claude"]
    assert [item["changes"] for item in reviews] == [
        "diff --git a/code.py b/code.py\n+version 1\n",
        "diff --git a/code.py b/code.py\n+version 2\n",
    ]
    assert [item["new_files"]["new.py"] for item in reviews] == [
        "new version 1", "new version 2"
    ]
    assert all(item["base_commit"] == BASE for item in reviews)


def test_snapshot_fails_closed_when_content_changes_during_collection(fictional, monkeypatch):
    root, _ = fictional
    (root / "new.py").write_text("version 1")
    calls = 0

    def fake_git(where, *args):
        nonlocal calls
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args[0] == "rev-parse":
            return BASE
        if args[0] == "ls-files":
            calls += 1
            if calls == 2:
                (root / "new.py").write_text("version 2")
            return "new.py\0"
        return ""

    monkeypatch.setattr("agent_loop.live._git", fake_git)
    with pytest.raises(ValueError, match="changed during snapshot"):
        collect_review_package(root, "fictional requirement", BASE)


def test_snapshot_includes_ignored_new_source_but_skips_runtime_files(fictional, monkeypatch):
    root, _ = fictional
    (root / "ignored.py").write_text("fictional = 1")

    def fake_git(where, *args):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args[0] == "rev-parse":
            return BASE
        if args[0] == "ls-files":
            assert "--exclude-standard" not in args
            return "ignored.py\0tmp/report.txt\0.fictional-agent-workspace\0"
        return ""

    monkeypatch.setattr("agent_loop.live._git", fake_git)
    package = collect_review_package(root, "fictional requirement", BASE)
    assert package.new_files == {"ignored.py": "fictional = 1"}


def test_claude_does_not_start_when_snapshot_collection_fails(fictional, monkeypatch):
    root, _ = fictional
    monkeypatch.setattr("agent_loop.live._git",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("Git failed")))
    _mock_process(monkeypatch, lambda *args, **kwargs: pytest.fail("Claude started"))
    with pytest.raises(RuntimeError, match="Git failed"):
        LiveClaude(root, "fictional requirement", BASE).review(
            "fictional task", "fictional edit", LoopTestResult("passed", "1 passed"))


@pytest.mark.parametrize("name,content", [
    ("accounts/token.json", "fictional"),
    ("new.py", "contact person@example.org"),
])
def test_snapshot_rejects_sensitive_paths_and_content(fictional, monkeypatch, name, content):
    root, _ = fictional
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    def fake_git(where, *args):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args[0] == "rev-parse":
            return BASE
        if args[0] == "ls-files":
            return name + "\0"
        return ""

    monkeypatch.setattr("agent_loop.live._git", fake_git)
    with pytest.raises(ValueError):
        collect_review_package(root, "fictional requirement", BASE)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_windows_timeout_kills_child_holding_pipes(tmp_path):
    from agent_loop.live import _run
    import ctypes
    from ctypes import wintypes

    pid_file = tmp_path / "child.pid"
    script = tmp_path / "fictional_parent.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=sys.stdout, stderr=sys.stderr)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="process tree"):
        _run([sys.executable, str(script), str(pid_file)], cwd=tmp_path,
             prompt=None, timeout=1)
    assert time.monotonic() - start < 8
    assert pid_file.is_file()
    pid = int(pid_file.read_text())
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            code = wintypes.DWORD()
            assert api.GetExitCodeProcess(handle, ctypes.byref(code))
            assert code.value != 259  # STILL_ACTIVE
        finally:
            api.CloseHandle(handle)
    else:
        assert ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID is gone
