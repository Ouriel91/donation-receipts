"""Fictional runner tests never start a model CLI."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_loop import demo


CODE = ("def normalize_username(username):\n"
        "    if not isinstance(username, str):\n"
        "        raise TypeError('username must be a string')\n"
        "    return username.strip().casefold()\n")
FIXED = CODE.replace("username.strip().casefold()", "username.strip().lower().casefold()")


class FakeCommand:
    def __init__(self, *, reviews=("approved",), test_results=("passed",),
                 failure=None):
        self.reviews = list(reviews)
        self.test_results = list(test_results)
        self.failure = failure
        self.feedback = []
        self.codes = []
        self.test_calls = 0
        self.claude_calls = 0

    def __call__(self, args, *, cwd, prompt, timeout, child_env, exit_codes=(0,)):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, demo.CODEX_VERSION if
                                           args[0] == "fake-node" else demo.CLAUDE_VERSION, "")
        if self.failure == "codex" and "exec" in args:
            raise RuntimeError("private CLI failure")
        if self.failure == "timeout" and "exec" in args:
            raise RuntimeError("private timeout detail")
        if "exec" in args:
            request = json.loads(prompt)
            self.feedback.append(request["previous_feedback"])
            code = FIXED if len(self.feedback) > 1 else CODE
            self.codes.append(code)
            assert "--disable" in args and "shell_tool" in args
            assert args[args.index("--sandbox") + 1] == "read-only"
            return subprocess.CompletedProcess(args, 0, json.dumps({"code": code}), "")
        if "pytest" in args:
            self.test_calls += 1
            assert Path(cwd, "example.py").read_text(encoding="utf-8") == self.codes[-1]
            result = self.test_results.pop(0) if self.test_results else "passed"
            code = 0 if result == "passed" else 1
            failed = 0 if result == "passed" else 1
            Path(args[args.index("--junitxml") + 1]).write_text(
                f'<testsuite tests="2" failures="{failed}" errors="0" skipped="0"/>',
                encoding="utf-8")
            return subprocess.CompletedProcess(args, code, "", "")
        if "-p" in args:
            self.claude_calls += 1
            assert args[args.index("--tools") + 1] == ""
            assert "Bash" not in args
            payload = json.loads(prompt)
            assert payload["code"] == self.codes[-1]
            status = self.reviews.pop(0)
            findings = ([{"id": "F1", "kind": "fix", "summary": "Handle fictional edge case"}]
                        if status == "changes_requested" else [])
            envelope = {"is_error": False, "structured_output":
                        {"status": status, "findings": findings}}
            return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")
        raise AssertionError(args)


def run_fake(fake, approve=lambda code: True):
    return demo.run_demo(run_models=True, approve=approve, command=fake,
                         commands=(["fake-node", "fake-codex"], ["fake-claude"],
                                   Path(sys.executable)))


def test_first_round_approved():
    fake = FakeCommand()
    report = run_fake(fake)
    assert report.stop_reason == "success"
    assert len(report.rounds) == 1
    assert report.rounds[0].tests.status == "passed"
    assert report.rounds[0].review["status"] == "approved"
    assert fake.feedback == [None]


def test_changes_requested_reaches_codex_and_then_approved():
    fake = FakeCommand(reviews=("changes_requested", "approved"))
    report = run_fake(fake)
    assert report.stop_reason == "success"
    assert [item.review["status"] for item in report.rounds] == [
        "changes_requested", "approved"]
    assert "Handle fictional edge case" in fake.feedback[1]


def test_failed_tests_skip_review_and_send_feedback():
    fake = FakeCommand(test_results=("failed", "passed"))
    report = run_fake(fake)
    assert report.stop_reason == "success"
    assert [item.tests.status for item in report.rounds] == ["failed", "passed"]
    assert report.rounds[0].review is None
    assert "Tests failed" in fake.feedback[1]
    assert fake.claude_calls == 1


@pytest.mark.parametrize("failure", ["codex", "timeout"])
def test_cli_failure_and_timeout_stop_safely(failure):
    report = run_fake(FakeCommand(failure=failure))
    assert report.stop_reason == "tool_error"
    assert report.error == "RuntimeError"
    assert "private" not in json.dumps(report.to_dict())


def test_human_declines_before_code_is_saved_or_run():
    fake = FakeCommand()
    report = run_fake(fake, approve=lambda code: False)
    assert report.stop_reason == "tests_not_run"
    assert report.rounds[0].tests.status == "not_run"
    assert fake.test_calls == fake.claude_calls == 0


def test_three_round_limit_is_core_limit():
    fake = FakeCommand(reviews=("changes_requested",) * 3)
    report = run_fake(fake)
    assert report.stop_reason == "max_rounds"
    assert len(report.rounds) == 3
    assert fake.feedback[0] is None
    assert all("Handle fictional edge case" in item for item in fake.feedback[1:])


def test_invalid_code_never_reaches_approval():
    fake = FakeCommand()
    def bad_command(args, **kwargs):
        if "exec" in args:
            return subprocess.CompletedProcess(args, 0,
                json.dumps({"code": "import os\nos.system('echo no')"}), "")
        return fake(args, **kwargs)
    report = demo.run_demo(run_models=True,
                           approve=lambda code: pytest.fail("approval asked"),
                           command=bad_command,
                           commands=(["fake-node", "fake-codex"], ["fake-claude"],
                                     Path(sys.executable)))
    assert report.stop_reason == "tool_error"
    assert report.rounds[0].tests is None


def test_default_cli_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["demo"])
    monkeypatch.setattr(demo, "run_demo", lambda **kwargs: pytest.fail("model started"))
    with pytest.raises(SystemExit) as exc:
        demo.main()
    assert exc.value.code == 2


def test_runner_requires_explicit_opt_in():
    with pytest.raises(PermissionError, match="opt-in"):
        demo.run_demo(approve=lambda code: True,
                      commands=(["fake-node"], ["fake-claude"], Path(sys.executable)))


def test_version_change_stops_before_model_call():
    def changed_version(args, **kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, "unknown version", "")
        pytest.fail("model started with an unchecked CLI version")
    with pytest.raises(RuntimeError, match="version changed"):
        demo.run_demo(run_models=True, approve=lambda code: True,
                      command=changed_version,
                      commands=(["fake-node"], ["fake-claude"], Path(sys.executable)))


def test_real_pytest_only_on_predefined_fictional_code(tmp_path):
    class FictionalCodex:
        code = CODE
    workspace = tmp_path / "fictional"
    workspace.mkdir()
    (workspace / "test_example.py").write_text(demo.TEST_SOURCE, encoding="utf-8")
    result = demo.DemoTests(workspace, FictionalCodex(), demo._run,
                            demo._demo_env(workspace, Path(sys.executable)),
                            lambda code: code == CODE).run()
    assert result.status == "passed", result.detail
