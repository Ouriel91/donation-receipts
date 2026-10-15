"""Command line entry point for mock scenarios only."""

import argparse
import json
from pathlib import Path

from agent_loop.core import load_task, run_loop
from agent_loop.mock import scenario


TASK_ROOT = Path(__file__).resolve().parent / "tasks"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local mock agent loop")
    parser.add_argument("--task", required=True, help="Markdown filename inside agent_loop/tasks")
    parser.add_argument("--scenario", choices=(
        "success", "test_then_pass", "review_then_pass", "limit", "invalid_json", "human"
    ), default="success")
    parser.add_argument("--max-rounds", type=int, default=3)
    args = parser.parse_args()

    try:
        task = load_task(TASK_ROOT / args.task, TASK_ROOT)
        codex, tests, claude = scenario(args.scenario)
        report = run_loop(task, codex, tests, claude, max_rounds=args.max_rounds)
    except (OSError, ValueError):
        parser.error("Invalid task or loop configuration")
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.stop_reason == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
