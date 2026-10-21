"""Git review tests use temporary commits and fake model CLIs only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_loop import demo, review
from agent_loop.core import TestResult as EvidenceResult


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "review-source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fictional Reviewer")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "safe.py")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "safe.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(root, "add", "safe.py")
    git(root, "commit", "-qm", "target")
    return root, base, git(root, "rev-parse", "HEAD")


def package(repository, status="not_run"):
    root, base, target = repository
    return review.collect_package(root, base, target, EvidenceResult(status, "Fictional evidence"))


def finding(*, severity="Major", status="open"):
    return {"id": "F1", "severity": severity, "path": "safe.py", "line": 1,
            "side": "target", "summary": "Value is wrong",
            "recommendation": "Correct the constant", "status": status}


def response(findings=()):
    status = ("human_decision" if any(f["status"] == "uncertain" for f in findings)
              else "changes_requested" if any(f["severity"] in {"Critical", "Major"}
                                              for f in findings) else "approved")
    return {"status": status, "findings": list(findings)}


class FakeModels:
    def __init__(self, codex=None, claude=None, fail=None):
        self.codex = response() if codex is None else codex
        self.claude = response() if claude is None else claude
        self.fail = fail
        self.calls = []

    def __call__(self, args, *, cwd, prompt, timeout, child_env, exit_codes=(0,)):
        if "--version" in args:
            version = demo.CODEX_VERSION if args[0] == "fake-node" else demo.CLAUDE_VERSION
            return subprocess.CompletedProcess(args, 0, version, "")
        self.calls.append((args, Path(cwd), json.loads(prompt)))
        assert timeout == 180
        assert Path(cwd).name.startswith("agent-git-models-")
        assert "safe.py" in prompt
        is_claude = "-p" in args
        if self.fail == ("claude_timeout" if is_claude else "codex_timeout"):
            raise subprocess.TimeoutExpired(args, timeout)
        if self.fail == ("claude_cli" if is_claude else "codex_cli"):
            raise RuntimeError("private CLI error")
        if "exec" in args:
            assert args[args.index("--sandbox") + 1] == "read-only"
            assert "shell_tool" in args and "code_mode_host" in args
            return subprocess.CompletedProcess(args, 0,
                                               self.codex if isinstance(self.codex, str)
                                               else json.dumps(self.codex), "")
        assert args[args.index("--tools") + 1] == ""
        assert "--restricted" in args and "--safe-mode" in args
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "is_error": False, "structured_output": self.claude}), "")


def run_fake(pkg, fake, approve=lambda: True):
    return review.review_package(pkg, run_models=True, approve=approve, command=fake,
                                 commands=(["fake-node", "fake-codex"],
                                           ["fake-claude"], Path(sys.executable)))


def test_valid_commit_package_and_empty_diff(repository):
    root, base, target = repository
    pkg = package(repository)
    assert pkg.base == base and pkg.target == target
    assert pkg.paths == ("safe.py",)
    assert "+VALUE = 2" in pkg.diff
    assert pkg.tests.status == "not_run"
    with pytest.raises(ValueError, match="empty"):
        review.collect_package(root, base, base, EvidenceResult("not_run", "none"))


def test_sensitive_file_and_content_stop_before_models(repository):
    root, base, _ = repository
    (root / "accounts").mkdir()
    (root / "accounts" / "credentials.json").write_text("{}", encoding="utf-8")
    git(root, "add", "accounts/credentials.json")
    git(root, "commit", "-qm", "sensitive path")
    with pytest.raises(ValueError, match="Sensitive or unsupported Git path"):
        review.collect_package(root, base, git(root, "rev-parse", "HEAD"),
                               EvidenceResult("not_run", "none"))
    (root / "accounts" / "credentials.json").unlink()
    git(root, "add", "-u")
    (root / "safe.py").write_text("email = 'donor@example.com'\n", encoding="utf-8")
    git(root, "add", "safe.py")
    git(root, "commit", "-qm", "sensitive content")
    with pytest.raises(ValueError, match="Sensitive content"):
        review.collect_package(root, base, git(root, "rev-parse", "HEAD"),
                               EvidenceResult("not_run", "none"))


def test_approved_read_only_and_no_git_changes(repository):
    root, _, _ = repository
    before = (git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain"),
              (root / "safe.py").read_bytes())
    fake = FakeModels()
    outcome = run_fake(package(repository), fake)
    assert outcome["decision"] == "Approved"
    assert outcome["core_report"]["stop_reason"] == "success"
    assert len(outcome["core_report"]["rounds"]) == 1
    assert len(fake.calls) == 2
    assert before == (git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain"),
                      (root / "safe.py").read_bytes())


def test_changes_requested_stops_without_retry(repository):
    fake = FakeModels(codex=response([finding()]))
    outcome = run_fake(package(repository), fake)
    assert outcome["decision"] == "Changes Requested"
    assert outcome["core_report"]["stop_reason"] == "changes_requested"
    assert len(outcome["core_report"]["rounds"]) == 1
    assert len(fake.calls) == 2
    assert outcome["codex"]["findings"][0]["recommendation"] == "Correct the constant"


def test_human_decision_and_failed_test_evidence(repository):
    uncertain = run_fake(package(repository), FakeModels(claude=response([
        finding(severity="Minor", status="uncertain")])))
    assert uncertain["decision"] == "Human Decision"
    failed = run_fake(package(repository, "failed"), FakeModels())
    assert failed["decision"] == "Human Decision"
    assert failed["core_report"]["rounds"][0]["tests"]["status"] == "failed"


@pytest.mark.parametrize("failure", ["codex_cli", "codex_timeout",
                                          "claude_cli", "claude_timeout"])
def test_cli_failure_or_timeout_is_not_approval(repository, failure):
    outcome = run_fake(package(repository), FakeModels(fail=failure))
    assert outcome["decision"] == "Human Decision"
    assert outcome["core_report"]["stop_reason"] == "tool_error"
    assert "private" not in json.dumps(outcome)


@pytest.mark.parametrize("bad_agent", ["codex", "claude"])
def test_invalid_json_is_not_approval(repository, bad_agent):
    fake = FakeModels(codex="not json" if bad_agent == "codex" else None,
                      claude="not json" if bad_agent == "claude" else None)
    outcome = run_fake(package(repository), fake)
    assert outcome["decision"] == "Human Decision"
    assert outcome["core_report"]["stop_reason"] == "tool_error"


def test_invalid_line_reference_is_not_approval(repository):
    bad = finding()
    bad["line"] = 9000
    outcome = run_fake(package(repository), FakeModels(codex=response([bad])))
    assert outcome["decision"] == "Human Decision"


def test_corrected_commit_can_be_reviewed_as_new_target(repository):
    root, base, old_target = repository
    (root / "safe.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(root, "add", "safe.py")
    git(root, "commit", "-qm", "correction")
    new_package = review.collect_package(root, base, git(root, "rev-parse", "HEAD"),
                                         EvidenceResult("passed", "Fictional tests passed"))
    assert new_package.target != old_target
    assert run_fake(new_package, FakeModels())["decision"] == "Approved"


def test_start_declined_prevents_all_model_calls(repository):
    fake = FakeModels()
    outcome = run_fake(package(repository), fake, approve=lambda: False)
    assert outcome["reason"] == "start_declined"
    assert fake.calls == []


def test_library_entry_requires_explicit_model_opt_in(repository):
    fake = FakeModels()
    with pytest.raises(PermissionError, match="opt-in"):
        review.review_package(package(repository), approve=lambda: True,
                              command=fake)
    assert fake.calls == []


def test_preview_is_default_and_does_not_start_models(repository, monkeypatch, capsys):
    root, base, target = repository
    monkeypatch.chdir(root)
    monkeypatch.setattr(review, "review_package", lambda *a, **k: pytest.fail("model started"))
    assert review.main(["--base", base, "--target", target]) == 0
    assert "Preview only" in capsys.readouterr().out


def test_core_rejects_multi_round_read_only_review():
    from agent_loop.core import run_loop
    with pytest.raises(ValueError, match="one round"):
        run_loop("review", object(), object(), object(), review_only=True)
