"""Small orchestration core independent of any CLI or product code."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TestResult:
    status: str
    detail: str


class CodexAdapter(Protocol):
    def run(self, task: str, feedback: str | None) -> str: ...


class TestAdapter(Protocol):
    def run(self) -> TestResult: ...


class ClaudeAdapter(Protocol):
    def review(self, task: str, implementation: str, tests: TestResult) -> str: ...


@dataclass
class RoundResult:
    number: int
    implementation: str | None = None
    tests: TestResult | None = None
    review: dict | None = None


@dataclass
class Report:
    stop_reason: str
    next_step: str
    rounds: list[RoundResult] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        if self.error is None:
            result.pop("error")
        return result


def load_task(path: Path, allowed_root: Path) -> str:
    """Read a small Markdown task only from the caller's approved directory."""
    root = allowed_root.resolve(strict=True)
    task_path = path.resolve(strict=True)
    if not task_path.is_relative_to(root) or task_path.suffix.lower() != ".md":
        raise ValueError("Task must be a Markdown file inside the allowed directory")
    relative_parts = task_path.relative_to(root).parts
    if any(part.lower() in {"accounts", "receipts", "reports", "credentials"}
           for part in relative_parts):
        raise ValueError("Task path is restricted")
    if task_path.stat().st_size > 64 * 1024:
        raise ValueError("Task file is too large")
    content = task_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("Task file is empty")
    return content


def parse_review(raw: str) -> dict:
    """Validate the entire review contract before the loop acts on findings."""
    try:
        review = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Review is not valid JSON") from exc
    if not isinstance(review, dict) or set(review) != {"status", "findings"}:
        raise ValueError("Review has an invalid shape")
    if not isinstance(review["status"], str) or review["status"] not in {
        "approved", "changes_requested", "human_decision"
    }:
        raise ValueError("Review has an invalid status")
    findings = review["findings"]
    if not isinstance(findings, list):
        raise ValueError("Review findings must be a list")
    for item in findings:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "summary"}:
            raise ValueError("Review finding has an invalid shape")
        if not isinstance(item["kind"], str) or item["kind"] not in {
            "fix", "non_blocking", "human_decision"
        }:
            raise ValueError("Review finding has an invalid kind")
        if not all(isinstance(item[key], str) and item[key].strip()
                   for key in ("id", "summary")):
            raise ValueError("Review finding has empty fields")
    kinds = {item["kind"] for item in findings}
    expected = ("human_decision" if "human_decision" in kinds else
                "changes_requested" if "fix" in kinds else "approved")
    if review["status"] != expected:
        raise ValueError("Review status conflicts with findings")
    return review


def run_loop(task: str, codex: CodexAdapter, tests: TestAdapter,
             claude: ClaudeAdapter, max_rounds: int = 3,
             review_only: bool = False) -> Report:
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not 1 <= max_rounds <= 3:
        raise ValueError("max_rounds must be between 1 and 3")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Task must be nonempty")
    if review_only and max_rounds != 1:
        raise ValueError("Read-only review must use one round")

    rounds: list[RoundResult] = []
    feedback = None
    for number in range(1, max_rounds + 1):
        round_result = RoundResult(number=number)
        rounds.append(round_result)
        try:
            implementation = codex.run(task, feedback)
        except Exception as exc:
            return Report("tool_error", "Inspect the Codex adapter failure manually.",
                          rounds, error=type(exc).__name__)
        if not isinstance(implementation, str) or not implementation.strip():
            return Report("invalid_output", "Inspect the Codex output manually.", rounds)
        round_result.implementation = implementation

        try:
            test_result = tests.run()
        except Exception as exc:
            return Report("tool_error", "Inspect the test adapter failure manually.",
                          rounds, error=type(exc).__name__)
        if not isinstance(test_result, TestResult) or not isinstance(test_result.status, str) or test_result.status not in {
            "passed", "failed", "not_run"
        } or not isinstance(test_result.detail, str) or not test_result.detail.strip():
            return Report("invalid_output", "Inspect the test result manually.", rounds)
        round_result.tests = test_result
        if test_result.status == "not_run" and not review_only:
            return Report("tests_not_run", "Run tests or resolve the environment issue manually.", rounds)
        if test_result.status == "failed" and not review_only:
            feedback = f"Tests failed: {test_result.detail}"
            continue

        try:
            raw_review = claude.review(task, implementation, test_result)
        except Exception as exc:
            return Report("tool_error", "Inspect the Claude adapter failure manually.",
                          rounds, error=type(exc).__name__)
        try:
            review = parse_review(raw_review)
        except ValueError as exc:
            cause = exc.__cause__ or exc
            return Report("invalid_output", "Inspect the review output manually.",
                          rounds, error=type(cause).__name__)
        round_result.review = review
        if review["status"] == "human_decision":
            return Report("human_decision", "Ask a person to decide on the review findings.", rounds)
        if review["status"] == "approved":
            return Report("success", "Review the report and decide whether to commit manually.", rounds)
        if review_only:
            return Report("changes_requested",
                          "Fix the findings separately, then review a new target commit.", rounds)
        feedback = "Review fixes: " + "; ".join(
            item["summary"] for item in review["findings"] if item["kind"] == "fix"
        )

    return Report("max_rounds", "Inspect remaining failures and decide the next step manually.", rounds)
