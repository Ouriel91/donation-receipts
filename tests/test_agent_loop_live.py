"""No model or real pytest process is started by these adapter tests."""

import json
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_loop import live as live_module
from agent_loop.core import TestResult as LoopTestResult, run_loop
from agent_loop.live import (MAX_OUTPUT, LiveClaude, LiveCodex, LivePytest, OutputLimitError,
                             ReviewPackage, collect_review_package)


BASE = "a" * 40


def _fixed_package(monkeypatch, package):
    monkeypatch.setattr("agent_loop.live.collect_review_package",
                        lambda root, requirements, base, explicit: package)


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

    monkeypatch.setattr("agent_loop.live._start_process",
                        lambda args, cwd, env: FakeProcess(args, cwd=cwd, env=env))
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


def _real_git_workspace(tmp_path):
    root = tmp_path / "fictional-git"
    root.mkdir()
    (root / ".fictional-agent-workspace").write_text("fiction only", encoding="utf-8")
    (root / ".gitignore").write_text(
        ".venv/\ntmp/\n__pycache__/\nsecrets/\nignored.py\n"
        ".fictional-agent-workspace\n", encoding="utf-8"
    )
    (root / "code.py").write_text("version = 0\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("add", "--", ".gitignore", "code.py")
    git("-c", "user.name=Fiction", "-c", "user.email=fiction@example.invalid",
        "commit", "-q", "-m", "fictional base")
    return root, git("rev-parse", "HEAD")


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
    assert args[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[args.index("--ask-for-approval") + 1] == "never"
    assert json.loads(processes[0].stdin.getvalue())["previous_feedback"] == "Fix fake failure"
    assert kwargs["cwd"] == root and processes[0].wait_timeouts[0] == 600
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]


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
            if "--ignored" in args:
                return ""
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
            if "--ignored" in args:
                return ""
            calls += 1
            if calls == 2:
                (root / "new.py").write_text("version 2")
            return "new.py\0"
        return ""

    monkeypatch.setattr("agent_loop.live._git", fake_git)
    with pytest.raises(ValueError, match="changed during snapshot"):
        collect_review_package(root, "fictional requirement", BASE)


def test_snapshot_requests_only_nonignored_new_files(fictional, monkeypatch):
    root, _ = fictional
    (root / "new.py").write_text("fictional = 1")

    def fake_git(where, *args):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args[0] == "rev-parse":
            return BASE
        if args[0] == "ls-files":
            assert "--exclude-standard" in args
            if "--ignored" in args:
                return ""
            return "new.py\0.fictional-agent-workspace\0"
        return ""

    monkeypatch.setattr("agent_loop.live._git", fake_git)
    package = collect_review_package(root, "fictional requirement", BASE)
    assert package.new_files == {"new.py": "fictional = 1"}


def test_real_git_snapshot_excludes_large_venv_and_requires_explicit_ignored_file(tmp_path):
    root, base = _real_git_workspace(tmp_path)
    (root / "code.py").write_text("version = 1\n", encoding="utf-8")
    (root / "new.py").write_text("new = True\n", encoding="utf-8")
    (root / "tmp").mkdir()
    (root / "tmp" / "report.txt").write_text("runtime", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "module.pyc").write_text("runtime", encoding="utf-8")
    venv = root / ".venv" / "pkg"
    venv.mkdir(parents=True)
    venv_names = [f"package_{number:04d}_" + "x" * 60 + ".py" for number in range(900)]
    assert sum(len(".venv/pkg/" + name) + 1 for name in venv_names) > MAX_OUTPUT
    for name in venv_names:
        (venv / name).write_text("x", encoding="utf-8")

    package = collect_review_package(root, "fictional requirement", base)
    assert "-version = 0" in package.changes
    assert "+version = 1" in package.changes
    assert package.new_files == {"new.py": "new = True\n"}
    assert ".venv" not in package.changes

    (root / "ignored.py").write_text("ignored = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unapproved ignored paths"):
        collect_review_package(root, "fictional requirement", base)
    included = collect_review_package(root, "fictional requirement", base,
                                      explicit_ignored_files=("ignored.py",))
    assert included.new_files == {"new.py": "new = True\n",
                                  "ignored.py": "ignored = True\n"}
    (root / "ignored.py").write_text("contact person@example.org", encoding="utf-8")
    with pytest.raises(ValueError, match="content is not allowed"):
        collect_review_package(root, "fictional requirement", base,
                               explicit_ignored_files=("ignored.py",))
    (root / "ignored.py").unlink()
    secret = root / "secrets" / "token.json"
    secret.parent.mkdir()
    secret.write_text('{"fictional": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="unapproved ignored paths"):
        collect_review_package(root, "fictional requirement", base)
    with pytest.raises(ValueError, match="not allowed"):
        collect_review_package(root, "fictional requirement", base,
                               explicit_ignored_files=("secrets/token.json",))
    with pytest.raises(ValueError, match="not ignored"):
        collect_review_package(root, "fictional requirement", base,
                               explicit_ignored_files=("new.py",))


def test_real_git_snapshot_refreshes_between_rounds(tmp_path):
    root, base = _real_git_workspace(tmp_path)
    (root / ".gitignore").write_text(
        ".venv/\ntmp/\n__pycache__/\nsecrets/\nignored.py\n"
        ".fictional-agent-workspace\n*.cache\n",
        encoding="utf-8"
    )
    (root / "new.py").write_text("first = True\n", encoding="utf-8")
    (root / "code.py").write_text("version = 1\n", encoding="utf-8")
    first = collect_review_package(root, "fictional requirement", base)
    (root / "new.py").write_text("second = True\n", encoding="utf-8")
    (root / "code.py").write_text("version = 2\n", encoding="utf-8")
    second = collect_review_package(root, "fictional requirement", base)
    assert "diff --git a/.gitignore b/.gitignore" in first.changes
    assert first.changes != second.changes
    assert first.new_files["new.py"] == "first = True\n"
    assert second.new_files["new.py"] == "second = True\n"


def test_real_git_changed_ignore_cannot_hide_new_python_file(tmp_path):
    root, base = _real_git_workspace(tmp_path)
    (root / ".gitignore").write_text(
        (root / ".gitignore").read_text(encoding="utf-8") + "*.py\n",
        encoding="utf-8"
    )
    (root / "new.py").write_text("hidden = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unapproved ignored paths"):
        collect_review_package(root, "fictional requirement", base)


def test_real_git_new_visible_ignore_and_source_reach_review(tmp_path):
    root, base = _real_git_workspace(tmp_path)
    feature = root / "feature"
    feature.mkdir()
    (feature / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (feature / "new.py").write_text("visible = True\n", encoding="utf-8")
    package = collect_review_package(root, "fictional requirement", base)
    assert package.new_files == {
        "feature/.gitignore": "*.tmp\n",
        "feature/new.py": "visible = True\n",
    }


def test_real_git_snapshot_failure_cannot_approve(tmp_path, monkeypatch):
    root, base = _real_git_workspace(tmp_path)
    feature = root / "feature"
    feature.mkdir()
    (feature / ".gitignore").write_text("*\n", encoding="utf-8")
    (feature / "hidden.py").write_text("hidden = True\n", encoding="utf-8")
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", "feature/.gitignore"],
                             cwd=root, check=False, capture_output=True)
    assert ignored.returncode == 0  # The new .gitignore hides itself.
    monkeypatch.setattr("agent_loop.live.LIVE_ISOLATION_VERIFIED", True)
    real_run = live_module._run

    def git_only(args, **kwargs):
        if not Path(args[0]).name.lower().startswith("git"):
            pytest.fail("Claude CLI started after snapshot failure")
        return real_run(args, **kwargs)

    monkeypatch.setattr("agent_loop.live._run", git_only)

    class FakeCodex:
        def run(self, task, feedback):
            return "fictional edit"

    class FakeTests:
        def run(self):
            return LoopTestResult("passed", "1 passed")

    report = run_loop("fictional task", FakeCodex(), FakeTests(),
                      LiveClaude(root, "fictional requirement", base))
    assert report.stop_reason == "tool_error"
    assert report.error == "ReviewIntegrityError"
    assert report.rounds[0].review is None


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
            if "--ignored" in args:
                return ""
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


def test_windows_ignored_runtime_names_are_case_insensitive(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows path semantics")
    from agent_loop import live

    (tmp_path / "NODE_MODULES").mkdir()
    (tmp_path / "src" / "__PYCACHE__").mkdir(parents=True)
    monkeypatch.setattr(live, "_git", lambda *args: "NODE_MODULES/\0src/__PYCACHE__/\0")
    live._check_ignored_integrity(tmp_path, [])
