import json
import sys
from unittest.mock import Mock

import pytest

from agent_loop.__main__ import main
from agent_loop.core import TestResult as LoopTestResult, load_task, parse_review, run_loop
from agent_loop.mock import MockClaude, MockCodex, MockTests, scenario


TASK = "# Fictional task\nMake a fake change."


def _run(name, max_rounds=3):
    codex, tests, claude = scenario(name)
    return run_loop(TASK, codex, tests, claude, max_rounds), codex


def test_success_in_first_round():
    report, codex = _run("success")
    assert report.stop_reason == "success"
    assert len(report.rounds) == 1
    assert report.rounds[0].tests.status == "passed"
    assert report.rounds[0].review["status"] == "approved"
    assert codex.feedback_seen == [None]


def test_failed_tests_then_successful_fix():
    report, codex = _run("test_then_pass")
    assert report.stop_reason == "success"
    assert [item.tests.status for item in report.rounds] == ["failed", "passed"]
    assert report.rounds[0].review is None
    assert "Tests failed" in codex.feedback_seen[1]


def test_claude_fix_finding_returns_to_codex():
    report, codex = _run("review_then_pass")
    assert report.stop_reason == "success"
    assert report.rounds[0].review["status"] == "changes_requested"
    assert "Handle the fake edge case" in codex.feedback_seen[1]


def test_three_rounds_stop_without_success():
    report, _ = _run("limit")
    assert report.stop_reason == "max_rounds"
    assert len(report.rounds) == 3
    assert all(item.tests.status == "failed" for item in report.rounds)
    assert "commit" not in report.next_step.lower()


def test_invalid_json_stops_with_partial_report():
    report, _ = _run("invalid_json")
    assert report.stop_reason == "invalid_output"
    assert report.rounds[0].tests.status == "passed"
    assert report.rounds[0].review is None
    assert "review output" in report.next_step
    assert report.error == "JSONDecodeError"


def test_recursive_json_error_becomes_invalid_output(monkeypatch):
    def recursive_loads(raw):
        raise RecursionError("private detail must not appear")

    monkeypatch.setattr("agent_loop.core.json.loads", recursive_loads)
    report, _ = _run("success")
    assert report.stop_reason == "invalid_output"
    assert report.error == "RecursionError"
    assert "private detail" not in json.dumps(report.to_dict())


def test_adapter_failure_stops_with_accurate_report():
    codex = MockCodex(["Mock change"])
    tests = MockTests([])
    report = run_loop(TASK, codex, tests, MockClaude([]))
    assert report.stop_reason == "tool_error"
    assert len(report.rounds) == 1
    assert report.rounds[0].implementation == "Mock change"
    assert report.rounds[0].tests is None
    assert report.rounds[0].review is None
    assert report.to_dict()["next_step"] == "Inspect the test adapter failure manually."
    assert report.to_dict()["error"] == "IndexError"


@pytest.mark.parametrize("which", ["codex", "claude"])
def test_model_adapter_error_reports_type_only(which):
    secret = "private-message-must-not-appear"
    codex = MockCodex(["Mock change"])
    claude = MockClaude([json.dumps({"status": "approved", "findings": []})])
    if which == "codex":
        codex = Mock()
        codex.run.side_effect = RuntimeError(secret)
    else:
        claude = Mock()
        claude.review.side_effect = RuntimeError(secret)
    report = run_loop(TASK, codex,
                      MockTests([LoopTestResult("passed", "1 passed")]), claude)
    assert report.stop_reason == "tool_error"
    assert report.error == "RuntimeError"
    assert secret not in json.dumps(report.to_dict())
    assert report.rounds[0].review is None


def test_empty_codex_output_stops_before_tests():
    report = run_loop(TASK, MockCodex(["  "]), MockTests([]), MockClaude([]))
    assert report.stop_reason == "invalid_output"
    assert report.rounds[0].tests is None


def test_empty_review_output_stops_with_test_result():
    report = run_loop(TASK, MockCodex(["Mock change"]),
                      MockTests([LoopTestResult("passed", "1 passed")]), MockClaude([""]))
    assert report.stop_reason == "invalid_output"
    assert report.rounds[0].tests.status == "passed"
    assert report.rounds[0].review is None


def test_human_decision_stops():
    report, _ = _run("human")
    assert report.stop_reason == "human_decision"
    assert report.rounds[0].review["findings"][0]["kind"] == "human_decision"
    assert "person" in report.next_step
    assert len(report.rounds) == 1


def test_not_run_tests_stop_without_review():
    report = run_loop(TASK, MockCodex(["Mock change"]),
                      MockTests([LoopTestResult("not_run", "Environment unavailable")]),
                      MockClaude([]))
    assert report.stop_reason == "tests_not_run"
    assert report.rounds[0].tests.status == "not_run"
    assert report.rounds[0].review is None


def test_invalid_test_status_stops_safely():
    report = run_loop(TASK, MockCodex(["Mock change"]),
                      MockTests([LoopTestResult(["passed"], "1 passed")]),
                      MockClaude([]))
    assert report.stop_reason == "invalid_output"
    assert report.rounds[0].tests is None
    assert report.rounds[0].review is None


def test_non_blocking_finding_allows_success():
    review = json.dumps({"status": "approved", "findings": [
        {"id": "M1", "kind": "non_blocking", "summary": "Optional fake improvement"}
    ]})
    report = run_loop(TASK, MockCodex(["Mock change"]),
                      MockTests([LoopTestResult("passed", "1 passed")]), MockClaude([review]))
    assert report.stop_reason == "success"
    assert report.rounds[0].review["findings"][0]["kind"] == "non_blocking"


@pytest.mark.parametrize("raw", [
    "not-json",
    json.dumps({"status": "approved", "findings": "none"}),
    json.dumps({"status": ["approved"], "findings": []}),
    json.dumps({"status": "approved", "findings": [
        {"id": "M1", "kind": "fix", "summary": "Fix it"}
    ]}),
    json.dumps({"status": "approved", "findings": [
        {"id": "M1", "kind": [], "summary": "Fix it"}
    ]}),
])
def test_review_rejects_invalid_shapes(raw):
    with pytest.raises(ValueError):
        parse_review(raw)


def test_task_loader_stays_inside_allowed_directory(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    task_file = allowed / "task.md"
    task_file.write_text(TASK, encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Forbidden fake content", encoding="utf-8")
    assert load_task(task_file, allowed) == TASK
    with pytest.raises(ValueError):
        load_task(outside, allowed)
    with pytest.raises(ValueError):
        load_task(allowed / ".." / "outside.md", allowed)


def test_restricted_name_in_parent_does_not_block_allowed_task(tmp_path):
    allowed = tmp_path / "accounts" / "safe_tasks"
    allowed.mkdir(parents=True)
    task_file = allowed / "task.md"
    task_file.write_text(TASK, encoding="utf-8")
    assert load_task(task_file, allowed) == TASK


def test_restricted_name_inside_allowed_root_is_rejected(tmp_path):
    allowed = tmp_path / "safe_tasks"
    restricted = allowed / "credentials"
    restricted.mkdir(parents=True)
    task_file = restricted / "task.md"
    task_file.write_text(TASK, encoding="utf-8")
    with pytest.raises(ValueError, match="restricted"):
        load_task(task_file, allowed)


def test_cli_invalid_configuration_exits_two(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent_loop", "--task", "example.md", "--max-rounds", "4"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "Invalid task or loop configuration" in capsys.readouterr().err


def test_round_limit_cannot_exceed_three():
    codex, tests, claude = scenario("success")
    with pytest.raises(ValueError, match="max_rounds"):
        run_loop(TASK, codex, tests, claude, max_rounds=4)
