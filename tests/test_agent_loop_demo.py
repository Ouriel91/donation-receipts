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
DATE_CODE = ("def is_donation_in_approval_period(donation_date, start_date, end_date):\n"
             "    if not isinstance(donation_date, date):\n"
             "        raise TypeError('invalid donation date')\n"
             "    if not isinstance(start_date, date):\n"
             "        raise TypeError('invalid start date')\n"
             "    if not isinstance(end_date, date):\n"
             "        raise TypeError('invalid end date')\n"
             "    if start_date > end_date:\n"
             "        raise ValueError('reversed period')\n"
             "    return start_date <= donation_date <= end_date\n")
DATE_FIXED = DATE_CODE.replace("start_date <= donation_date <= end_date",
                               "donation_date >= start_date and donation_date <= end_date")


class FakeCommand:
    def __init__(self, *, reviews=("approved",), test_results=("passed",),
                 failure=None, task_name="username", claude_version="2.1.316 (Claude Code)"):
        self.reviews = list(reviews)
        self.test_results = list(test_results)
        self.failure = failure
        self.task_name = task_name
        self.claude_version = claude_version
        self.feedback = []
        self.codes = []
        self.tested_code_bytes = []
        self.reviewed_code_bytes = []
        self.test_calls = 0
        self.claude_calls = 0

    def __call__(self, args, *, cwd, prompt, timeout, child_env, exit_codes=(0,)):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, demo.CODEX_VERSION if
                                           args[0] == "fake-node" else self.claude_version, "")
        if self.failure == "codex" and "exec" in args:
            raise RuntimeError("private CLI failure")
        if self.failure == "timeout" and "exec" in args:
            raise RuntimeError("private timeout detail")
        if "exec" in args:
            request = json.loads(prompt)
            self.feedback.append(request["previous_feedback"])
            assert request["task"] == demo.TASKS[self.task_name][0]
            code = (DATE_FIXED if len(self.feedback) > 1 else DATE_CODE) if self.task_name == "section46" else (
                FIXED if len(self.feedback) > 1 else CODE)
            self.codes.append(code)
            assert "--disable" in args and "shell_tool" in args
            assert args[args.index("--sandbox") + 1] == "read-only"
            return subprocess.CompletedProcess(args, 0, json.dumps({"code": code}), "")
        if "pytest" in args:
            self.test_calls += 1
            source = Path(cwd, "example.py").read_bytes()
            prefix = demo.TASKS[self.task_name][2].encode("utf-8")
            assert source == prefix + self.codes[-1].encode("utf-8")
            self.tested_code_bytes.append(source[len(prefix):])
            assert Path(cwd, "test_example.py").read_text(encoding="utf-8") == demo.TASKS[self.task_name][1]
            result = self.test_results.pop(0) if self.test_results else "passed"
            code = 0 if result == "passed" else 1
            failed = 0 if result == "passed" else 1
            Path(args[args.index("--junitxml") + 1]).write_text(
                f'<testsuite tests="2" failures="{failed}" errors="0" skipped="0"/>',
                encoding="utf-8")
            return subprocess.CompletedProcess(args, code, "", "")
        if "-p" in args:
            self.claude_calls += 1
            assert "--restricted" in args and "--safe-mode" in args
            assert args[args.index("--tools") + 1] == ""
            assert args[args.index("--permission-mode") + 1] == "dontAsk"
            assert args[args.index("--permission-prompts") + 1] == "none"
            assert args[args.index("--disallowedTools") + 1] == "mcp__*"
            assert "--strict-mcp-config" in args and "--no-session-persistence" in args
            assert args[args.index("--output-format") + 1] == "json"
            assert "--json-schema" in args
            assert "Bash" not in args
            payload = json.loads(prompt)
            assert payload["code"] == self.codes[-1]
            self.reviewed_code_bytes.append(payload["code"].encode("utf-8"))
            status = self.reviews.pop(0)
            findings = ([{"id": "F1", "kind": "fix", "summary": "Handle fictional edge case"}]
                        if status == "changes_requested" else [])
            envelope = {"is_error": False, "structured_output":
                        {"status": status, "findings": findings}}
            return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")
        raise AssertionError(args)


def run_fake(fake, approve=lambda code: True, task_name="username"):
    return demo.run_demo(run_models=True, approve=approve, task_name=task_name, command=fake,
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


@pytest.mark.parametrize("version", sorted(demo.CLAUDE_VERSIONS))
def test_verified_claude_versions_keep_the_same_tool_restrictions(version):
    report = run_fake(FakeCommand(claude_version=version))
    assert report.stop_reason == "success"


def test_unverified_claude_version_stops_before_model_call():
    def unverified_claude(args, **kwargs):
        if "--version" in args:
            version = demo.CODEX_VERSION if args[0] == "fake-node" else "2.1.317 (Claude Code)"
            return subprocess.CompletedProcess(args, 0, version, "")
        pytest.fail("model started with an unchecked Claude version")
    with pytest.raises(RuntimeError, match="version changed"):
        demo.run_demo(run_models=True, approve=lambda code: True,
                      command=unverified_claude,
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


def test_section46_fixed_task_runs_through_existing_core_and_adapters():
    fake = FakeCommand(task_name="section46", reviews=("changes_requested", "approved"))
    approvals = []
    report = run_fake(fake, approve=lambda code: approvals.append(code) or True,
                      task_name="section46")
    assert report.stop_reason == "success"
    assert [item.review["status"] for item in report.rounds] == ["changes_requested", "approved"]
    assert approvals == [DATE_CODE, DATE_FIXED]
    assert fake.test_calls == fake.claude_calls == 2
    assert "Handle fictional edge case" in fake.feedback[1]


def test_indented_code_bytes_match_display_pytest_claude_and_decoded_report(monkeypatch, capsysbinary):
    monkeypatch.setattr("builtins.input", lambda _: "RUN")
    fake = FakeCommand(task_name="section46")
    report = run_fake(fake, approve=demo._approve_code, task_name="section46")
    assert report.stop_reason == "success"
    displayed = capsysbinary.readouterr().err
    assert displayed == (b"\nFictional code proposed for execution:\n" +
                         DATE_CODE.encode("utf-8") + b"\n\n")
    expected = DATE_CODE.encode("utf-8")
    decoded_report = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
    assert fake.tested_code_bytes == [expected]
    assert fake.reviewed_code_bytes == [expected]
    assert decoded_report["rounds"][0]["implementation"].encode("utf-8") == expected


def test_mismatched_reported_code_cannot_succeed(monkeypatch):
    original_run = demo.DemoCodex.run
    def return_other_code(self, task, feedback):
        original_run(self, task, feedback)
        return DATE_FIXED
    monkeypatch.setattr(demo.DemoCodex, "run", return_other_code)
    fake = FakeCommand(task_name="section46")
    report = run_fake(fake, task_name="section46")
    assert report.stop_reason == "tool_error"
    assert fake.test_calls == 1
    assert fake.claude_calls == 0


def test_changed_source_during_pytest_cannot_succeed():
    class ChangedSource(FakeCommand):
        def __call__(self, args, **kwargs):
            result = super().__call__(args, **kwargs)
            if "pytest" in args:
                Path(kwargs["cwd"], "example.py").write_bytes(b"changed source")
            return result
    fake = ChangedSource(task_name="section46")
    report = run_fake(fake, task_name="section46")
    assert report.stop_reason == "tests_not_run"
    assert fake.claude_calls == 0


def test_section46_human_declines_before_saving_or_testing():
    fake = FakeCommand(task_name="section46")
    report = run_fake(fake, approve=lambda code: False, task_name="section46")
    assert report.stop_reason == "tests_not_run"
    assert fake.test_calls == fake.claude_calls == 0


@pytest.mark.parametrize("bad_code", [
    "from datetime import date\n" + DATE_CODE,
    DATE_CODE.replace("start_date > end_date", "start_date.isoformat() > end_date.isoformat()"),
    DATE_CODE.replace("start_date > end_date", "open('outside')"),
])
def test_section46_rejects_imports_attributes_and_extra_calls(bad_code):
    with pytest.raises(ValueError):
        demo._validate_code(bad_code, "section46")


def test_username_validator_does_not_gain_date_comparisons():
    with pytest.raises(ValueError):
        demo._validate_code(CODE.replace("username.strip().casefold()",
                                         "username <= 'alice'"))


def test_section46_real_pytest_on_predefined_fictional_code(tmp_path):
    class FictionalCodex:
        code = DATE_CODE
    workspace = tmp_path / "fictional"
    workspace.mkdir()
    (workspace / "test_example.py").write_text(demo.DATE_TEST_SOURCE, encoding="utf-8")
    tests = demo.DemoTests(workspace, FictionalCodex(), demo._run,
                           demo._demo_env(workspace, Path(sys.executable)),
                           lambda code: code == DATE_CODE, "section46")
    result = tests.run()
    assert result.status == "passed", result.detail
    assert result.detail == "7 passed, 0 failed, 0 errors, 0 skipped"
    assert tests.tested_code == DATE_CODE.encode("utf-8")
    assert (workspace / "example.py").read_bytes() == (
        demo.TASKS["section46"][2].encode("utf-8") + tests.tested_code)


def test_cli_selects_section46_after_start(monkeypatch, capsys):
    chosen = []
    monkeypatch.setattr(sys, "argv", ["demo", "--run-models", "--task", "section46"])
    monkeypatch.setattr("builtins.input", lambda _: "START")
    monkeypatch.setattr(demo, "run_demo", lambda **kwargs: chosen.append(kwargs) or
                        demo.Report("success", "Human review", []))
    assert demo.main() == 0
    assert chosen[0]["task_name"] == "section46"
    assert chosen[0]["approve"] == demo._approve_code
    assert "success" in capsys.readouterr().out


def test_cli_declined_start_never_calls_models(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["demo", "--run-models", "--task", "section46"])
    monkeypatch.setattr("builtins.input", lambda _: "NO")
    monkeypatch.setattr(demo, "run_demo", lambda **kwargs: pytest.fail("model started"))
    assert demo.main() == 1


def test_invalid_task_stops_before_model_call():
    with pytest.raises(ValueError, match="Unknown fictional task"):
        demo.run_demo(run_models=True, approve=lambda code: True, task_name="other",
                      command=lambda *args, **kwargs: pytest.fail("model started"))
