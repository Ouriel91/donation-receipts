"""Opt-in fictional CLI demo using the existing orchestration core.

This is a supervised text exchange on the host, not the gated live adapters or
an OS isolation boundary. No model is started by importing this module.
"""

import argparse
import ast
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from agent_loop.core import Report, TestResult, parse_review, run_loop
from agent_loop.live import _run


TASK = ("Implement normalize_username(username: str) in example.py. "
        "For strings, strip surrounding whitespace and use Unicode casefold. "
        "An all-whitespace string becomes an empty string. Raise TypeError for "
        "non-string input. Return only Python source for one function, with no "
        "imports, decorators, commands, tests, or Markdown fences.")
TEST_SOURCE = '''import pytest
from example import normalize_username

def test_normalization():
    assert normalize_username("  ALICE  ") == "alice"
    assert normalize_username("Straße") == "strasse"
    assert normalize_username("  ") == ""

def test_non_string():
    with pytest.raises(TypeError):
        normalize_username(None)
'''
CODEX_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"code": {"type": "string"}}, "required": ["code"]}
REVIEW_SCHEMA = {"type": "object", "additionalProperties": False,
                 "properties": {
                     "status": {"type": "string", "enum": ["approved", "changes_requested"]},
                     "findings": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False,
                         "properties": {"id": {"type": "string"},
                                        "kind": {"type": "string", "enum": ["fix", "non_blocking"]},
                                        "summary": {"type": "string"}},
                         "required": ["id", "kind", "summary"]}}},
                 "required": ["status", "findings"]}
MAX_CODE = 8192
MAX_PROMPT = 16 * 1024
CODEX_VERSION = "codex-cli 0.163.0"
CLAUDE_VERSION = "2.1.280 (Claude Code)"


def _small(value: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError("Demo input or output is empty or too large")
    return value


def _validate_code(code: str) -> str:
    """Accept only a small, inspectable function language before execution."""
    _small(code, MAX_CODE)
    tree = ast.parse(code)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Expected one function only")
    function = tree.body[0]
    if (function.name != "normalize_username" or function.decorator_list or
        function.args.posonlyargs or function.args.kwonlyargs or function.args.vararg or
        function.args.kwarg or function.args.defaults or function.args.kw_defaults or
        len(function.args.args) != 1 or function.args.args[0].arg != "username"):
        raise ValueError("Unexpected function signature")
    allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
               ast.If, ast.Raise, ast.Assign, ast.Name, ast.Load, ast.Store,
               ast.Call, ast.Attribute, ast.Constant, ast.Compare, ast.UnaryOp,
               ast.Not, ast.Is, ast.IsNot, ast.Eq, ast.BoolOp, ast.And, ast.Or,
               ast.Expr)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("Code uses syntax outside the fictional task")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("Private names are not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in {"strip", "casefold", "lower"}:
            raise ValueError("Only string normalization methods are allowed")
        if isinstance(node, ast.Call):
            target = node.func
            if node.keywords or not (
                isinstance(target, ast.Attribute) and target.attr in {"strip", "casefold", "lower"}
                or isinstance(target, ast.Name) and target.id in {"isinstance", "TypeError"}
            ):
                raise ValueError("Call outside the fictional task")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in {
            "eval", "exec", "open", "__import__", "compile", "globals", "locals"
        }:
            raise ValueError("Unsafe name")
    return code


def _demo_env(workspace: Path, node: Path) -> dict[str, str]:
    # CLIs may use their existing login. Do not pass application/API credentials.
    keys = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
            "SystemRoot", "WINDIR")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update({"PATH": os.pathsep.join((str(node.parent),
                                         str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32"))),
                "TEMP": str(workspace), "TMP": str(workspace),
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return env


def _commands() -> tuple[list[str], list[str], Path]:
    node_name = shutil.which("node.exe" if os.name == "nt" else "node")
    codex_name = shutil.which("codex.cmd" if os.name == "nt" else "codex")
    claude_name = shutil.which("claude.exe" if os.name == "nt" else "claude")
    if not all((node_name, codex_name, claude_name)):
        raise RuntimeError("Required CLIs are unavailable")
    node = Path(node_name).resolve(strict=True)
    codex_script = Path(codex_name).resolve(strict=True).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not codex_script.is_file():
        raise RuntimeError("Codex launcher layout is unsupported")
    return [str(node), str(codex_script)], [str(Path(claude_name).resolve(strict=True))], node


def _check_versions(command: Callable, codex: list[str], claude: list[str],
                    workspace: Path, env: dict[str, str]) -> None:
    for args, expected in ((codex, CODEX_VERSION), (claude, CLAUDE_VERSION)):
        actual = command([*args, "--version"], cwd=workspace, prompt=None,
                         timeout=10, child_env=env).stdout.strip()
        if actual != expected:
            raise RuntimeError("CLI version changed; verify demo flags before use")


@dataclass
class DemoCodex:
    workspace: Path
    command: Callable
    executable: list[str]
    env: dict[str, str]
    code: str | None = None

    def run(self, task: str, feedback: str | None) -> str:
        prompt = _small(json.dumps({"task": task, "previous_feedback": feedback,
                                    "instructions": "Use no tools or commands. Return JSON containing only code."},
                                   ensure_ascii=False), MAX_PROMPT)
        schema = self.workspace / "codex-schema.json"
        schema.write_text(json.dumps(CODEX_SCHEMA), encoding="utf-8")
        result = self.command([*self.executable, "--strict-config",
                               "--disable", "shell_tool",
                               "--disable", "code_mode_host", "--disable", "apps",
                               "--disable", "browser_use", "--disable", "computer_use",
                               "--disable", "plugins", "--disable", "multi_agent",
                               "--ask-for-approval", "never",
                               "exec", "--sandbox", "read-only", "--ignore-user-config",
                               "--ephemeral", "--skip-git-repo-check", "--output-schema",
                               str(schema), "-"], cwd=self.workspace, prompt=prompt,
                              timeout=180, child_env=self.env)
        try:
            answer = json.loads(_small(result.stdout, MAX_CODE + 1024))
            if not isinstance(answer, dict) or set(answer) != {"code"}:
                raise ValueError("Unexpected Codex answer")
            self.code = _validate_code(answer["code"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid Codex answer") from None
        return self.code


@dataclass
class DemoTests:
    workspace: Path
    codex: DemoCodex
    command: Callable
    env: dict[str, str]
    approve: Callable[[str], bool]

    def run(self) -> TestResult:
        code = self.codex.code
        if code is None:
            raise ValueError("No fictional code to test")
        if not self.approve(code):
            return TestResult("not_run", "Human declined to execute fictional code")
        target = self.workspace / "example.py"
        if target.is_symlink():
            raise ValueError("Fictional target is a link")
        target.write_text(code, encoding="utf-8")  # Only the runner persists model code.
        parent = self.workspace / ("pytest-" + uuid4().hex)
        parent.mkdir()
        result_path = parent / "result.xml"
        try:
            result = self.command([sys.executable, "-I", "-m", "pytest", "-q",
                                   "-p", "no:cacheprovider", "--tb=short",
                                   "test_example.py", "--basetemp", str(parent / "run"),
                                   "--junitxml", str(result_path)],
                                  cwd=self.workspace, prompt=None, timeout=30,
                                  exit_codes=(0, 1), child_env=self.env)
            if result_path.stat().st_size > 64 * 1024:
                raise ValueError("Test report is too large")
            node = ET.parse(result_path).getroot()
            if node.tag == "testsuites" and len(node) == 1:
                node = node[0]
            if node.tag != "testsuite":
                raise ValueError("Invalid test report")
            total, failed, errors, skipped = (int(node.attrib[key]) for key in
                                              ("tests", "failures", "errors", "skipped"))
            passed = total - failed - errors - skipped
            if total < 1 or min(passed, failed, errors, skipped) < 0:
                raise ValueError("Invalid test counts")
            detail = f"{passed} passed, {failed} failed, {errors} errors, {skipped} skipped"
            if result.returncode == 0 and failed == errors == 0:
                return TestResult("passed", detail)
            if result.returncode == 1 and failed + errors > 0:
                return TestResult("failed", detail)
        except (OSError, ValueError, RuntimeError, ET.ParseError):
            pass
        return TestResult("not_run", "Fictional pytest did not produce a valid result")


@dataclass
class DemoClaude:
    workspace: Path
    command: Callable
    executable: list[str]
    env: dict[str, str]

    def review(self, task: str, implementation: str, tests: TestResult) -> str:
        prompt = _small(json.dumps({"instruction": "Review only the supplied fictional function and test result. Use no tools.",
                                    "task": task, "code": implementation,
                                    "tests": {"status": tests.status, "detail": tests.detail}},
                                   ensure_ascii=False), MAX_PROMPT)
        result = self.command([*self.executable, "-p", "--restricted", "--safe-mode",
                               "--tools", "", "--permission-mode", "dontAsk",
                               "--permission-prompts", "none", "--disallowedTools", "mcp__*",
                               "--strict-mcp-config", "--no-session-persistence",
                               "--output-format", "json", "--json-schema",
                               json.dumps(REVIEW_SCHEMA)], cwd=self.workspace,
                              prompt=prompt, timeout=180, child_env=self.env)
        try:
            envelope = json.loads(_small(result.stdout, 16 * 1024))
            if not isinstance(envelope, dict) or envelope.get("is_error") is not False:
                raise ValueError("Claude response failed")
            review = envelope["structured_output"]
            raw = _small(json.dumps(review, ensure_ascii=False), 8192)
            parsed = parse_review(raw)
            if parsed["status"] not in {"approved", "changes_requested"}:
                raise ValueError("Unsupported demo review status")
            return raw
        except (KeyError, TypeError, json.JSONDecodeError, ValueError):
            raise ValueError("Invalid Claude review") from None


def run_demo(*, run_models: bool = False, approve: Callable[[str], bool], command: Callable = _run,
             commands: tuple[list[str], list[str], Path] | None = None) -> Report:
    """Run one fixed task. The caller must opt in before calling this function."""
    if run_models is not True:
        raise PermissionError("Explicit model opt-in is required")
    repo = Path(__file__).resolve().parent.parent
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if temp_root.is_relative_to(repo):
        raise ValueError("System temporary directory must be outside the repository")
    codex_args, claude_args, node = commands if commands is not None else _commands()
    with tempfile.TemporaryDirectory(prefix="fictional-agent-demo-") as name:
        workspace = Path(name).resolve(strict=True)
        if workspace.is_relative_to(repo):
            raise ValueError("Demo workspace must be outside the repository")
        (workspace / "test_example.py").write_text(TEST_SOURCE, encoding="utf-8")
        env = _demo_env(workspace, node)
        _check_versions(command, codex_args, claude_args, workspace, env)
        codex = DemoCodex(workspace, command, codex_args, env)
        tests = DemoTests(workspace, codex, command, env, approve)
        claude = DemoClaude(workspace, command, claude_args, env)
        return run_loop(TASK, codex, tests, claude, max_rounds=3)


def _approve_code(code: str) -> bool:
    print("\nFictional code proposed for execution:\n" + code + "\n", file=sys.stderr)
    return input("Type RUN to execute these fictional Python tests: ").strip() == "RUN"


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervised fictional agent loop demo")
    parser.add_argument("--run-models", action="store_true", help="explicitly start the two CLI models")
    args = parser.parse_args()
    if not args.run_models:
        parser.error("models are off by default; pass --run-models to opt in")
    print("This Windows-user run is not OS isolation. The CLIs use their existing login.",
          file=sys.stderr)
    if input("Type START to run the fictional CLI demo: ").strip() != "START":
        print("Demo cancelled before any model call.", file=sys.stderr)
        return 1
    try:
        report = run_demo(run_models=True, approve=_approve_code)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Demo setup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.stop_reason == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
