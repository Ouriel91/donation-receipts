"""Deterministic adapters used by the MVP; no subprocesses or network calls."""

import json
from dataclasses import dataclass, field

from agent_loop.core import TestResult


@dataclass
class MockCodex:
    results: list[str]
    feedback_seen: list[str | None] = field(default_factory=list)

    def run(self, task: str, feedback: str | None) -> str:
        self.feedback_seen.append(feedback)
        return self.results.pop(0)


@dataclass
class MockTests:
    results: list[TestResult]

    def run(self) -> TestResult:
        return self.results.pop(0)


@dataclass
class MockClaude:
    results: list[str]

    def review(self, task: str, implementation: str, tests: TestResult) -> str:
        return self.results.pop(0)


def _review(status: str, findings: list[dict] | None = None) -> str:
    return json.dumps({"status": status, "findings": findings or []})


def scenario(name: str) -> tuple[MockCodex, MockTests, MockClaude]:
    """Return predefined outcomes for a safe end-to-end demo."""
    approved = _review("approved")
    fix = _review("changes_requested", [
        {"id": "M1", "kind": "fix", "summary": "Handle the fake edge case."}
    ])
    human = _review("human_decision", [
        {"id": "H1", "kind": "human_decision", "summary": "Choose the fake policy."}
    ])
    cases = {
        "success": (["Mock change"], [TestResult("passed", "1 passed")], [approved]),
        "test_then_pass": (["Initial mock change", "Mock fix"],
                           [TestResult("failed", "1 failed"), TestResult("passed", "1 passed")],
                           [approved]),
        "review_then_pass": (["Initial mock change", "Mock fix"],
                             [TestResult("passed", "1 passed"), TestResult("passed", "1 passed")],
                             [fix, approved]),
        "limit": (["Mock change 1", "Mock change 2", "Mock change 3"],
                  [TestResult("failed", "1 failed")] * 3, []),
        "invalid_json": (["Mock change"], [TestResult("passed", "1 passed")], ["not-json"]),
        "human": (["Mock change"], [TestResult("passed", "1 passed")], [human]),
    }
    if name not in cases:
        raise ValueError("Unknown mock scenario")
    implementations, tests, reviews = cases[name]
    return MockCodex(implementations), MockTests(tests), MockClaude(reviews)
