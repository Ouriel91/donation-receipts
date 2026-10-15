"""CLI adapters prepared for an isolated fictional workspace.

Phase 1 deliberately has no live entry point. The gate stays closed until an
independent Windows isolation check is implemented and verified.
"""

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent_loop.core import TestResult, parse_review


LIVE_ISOLATION_VERIFIED = False
MAX_PROMPT = 64 * 1024
MAX_OUTPUT = 64 * 1024
MAX_SUMMARY = 2048
MAX_REVIEW = 8192
PROCESS_CLEANUP_SECONDS = 3
MAX_DIFF = 48 * 1024
MAX_NEW_FILE = 16 * 1024
ALLOWED_SUFFIXES = frozenset({".py", ".md", ".txt", ".toml", ".json", ".yaml",
                              ".yml", ".ini", ".cfg", ".js", ".ts", ".html", ".css"})
BLOCKED_PARTS = frozenset({"accounts", "receipts", "reports", "secrets", "credentials",
                           "data", ".env", ".venv", "venv", "tmp", ".git"})
BLOCKED_NAMES = frozenset({"token.json", "credentials.json", "config.local.json"})
APPROVED_RUNTIME_ROOTS = frozenset({".venv", "venv", "tmp", ".pytest_cache",
                                    ".mypy_cache", ".ruff_cache", "node_modules"})
APPROVED_IGNORED_FILES = frozenset({".fictional-agent-workspace"})
SENSITIVE_CONTENT = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,})\b|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)


class OutputLimitError(ValueError):
    """A child produced more stdout than the adapter permits."""


class ReviewIntegrityError(ValueError):
    """Ignored workspace content makes the review snapshot incomplete."""

CODEX_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"}}, "required": ["summary"],
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["approved", "changes_requested", "human_decision"]},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string", "enum": ["fix", "non_blocking", "human_decision"]},
                "summary": {"type": "string"},
            }, "required": ["id", "kind", "summary"],
        }},
    }, "required": ["status", "findings"],
}


def _workspace(path: Path) -> Path:
    root = path.resolve(strict=True)
    if not root.is_dir() or not (root / ".fictional-agent-workspace").is_file():
        raise ValueError("A marked fictional workspace is required")
    return root


def _gate(path: Path) -> Path:
    if not LIVE_ISOLATION_VERIFIED:
        raise PermissionError("Live CLI execution is blocked pending Windows isolation verification")
    return _workspace(path)


def _small(value: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError("Input or output is empty or too large")
    return value


class _ManagedProcess:
    def __init__(self, process: subprocess.Popen, job=None):
        self.process = process
        self.job = job
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr

    def wait(self, timeout: float) -> int:
        return self.process.wait(timeout=timeout)

    def terminate_tree(self) -> None:
        if self.job is not None:
            self.job.terminate()
        else:
            os.killpg(self.process.pid, signal.SIGKILL)

    def close_tree(self) -> None:
        if self.job is not None:
            self.job.close()


def _start_process(args: list[str], cwd: Path) -> _ManagedProcess:
    options = {"cwd": cwd, "stdin": subprocess.PIPE,
               "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name != "nt":
        return _ManagedProcess(subprocess.Popen(args, start_new_session=True, **options))
    from agent_loop.windows_job import WindowsJob

    # Suspend before any model or test code can spawn a child outside the job.
    process = subprocess.Popen(args, creationflags=0x00000004, **options)
    try:
        job = WindowsJob(process.pid)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=PROCESS_CLEANUP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise RuntimeError("Windows process tree setup failed") from None
    return _ManagedProcess(process, job)


def _run(args: list[str], *, cwd: Path, prompt: str | None, timeout: int,
         exit_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """Drain both streams with bounded retention; never expose stderr or prompts."""
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise ValueError("CLI timeout must be between 1 and 3600 seconds")
    try:
        process = _start_process(args, cwd)
    except OSError:
        raise RuntimeError("CLI could not complete") from None
    output = bytearray()
    overflow = threading.Event()
    stream_error = threading.Event()

    def drain(stream, keep: bool) -> None:
        try:
            while chunk := stream.read(8192):
                if keep:
                    if len(output) + len(chunk) > MAX_OUTPUT:
                        overflow.set()
                    else:
                        output.extend(chunk)
        except OSError:
            stream_error.set()
        finally:
            stream.close()

    def send_prompt() -> None:
        try:
            if prompt is not None:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            stream_error.set()
        finally:
            try:
                process.stdin.close()
            except OSError:
                stream_error.set()

    threads = [threading.Thread(target=drain, args=(process.stdout, True), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, False), daemon=True),
               threading.Thread(target=send_prompt, daemon=True)]
    for thread in threads:
        thread.start()
    failed = False
    code = None
    try:
        code = process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed:
        try:
            process.terminate_tree()
        except (OSError, RuntimeError):
            failed = True
    try:
        process.close_tree()  # Kill-on-close also ends descendants after normal exit.
    except (OSError, RuntimeError):
        failed = True
    try:
        process.wait(timeout=PROCESS_CLEANUP_SECONDS)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        failed = True
    cleanup_deadline = time.monotonic() + PROCESS_CLEANUP_SECONDS
    for thread in threads:
        thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
    if failed or any(thread.is_alive() for thread in threads):
        raise RuntimeError("CLI process tree or streams could not be cleaned up")
    if overflow.is_set():
        raise OutputLimitError("CLI output is too large")
    if stream_error.is_set():
        raise RuntimeError("CLI stream failed")
    try:
        stdout = output.decode("utf-8")
    except UnicodeError:
        raise ValueError("CLI output is invalid UTF-8") from None
    result = subprocess.CompletedProcess(args, code, stdout, "")
    if result.returncode not in exit_codes:
        raise RuntimeError("CLI exited unsuccessfully")
    return result


@dataclass(frozen=True)
class LiveCodex:
    workspace: Path
    executable: str = "codex"
    timeout: int = 600

    def run(self, task: str, feedback: str | None) -> str:
        root = _gate(self.workspace)
        request = {"task": _small(task, MAX_PROMPT), "previous_feedback": feedback}
        prompt = _small(json.dumps(request, ensure_ascii=False), MAX_PROMPT)
        with tempfile.TemporaryDirectory(prefix="agent-schema-") as temp:
            schema = Path(temp) / "codex-schema.json"
            answer = Path(temp) / "codex-answer.json"
            schema.write_text(json.dumps(CODEX_SCHEMA), encoding="utf-8")
            _run([self.executable, "exec", "--sandbox", "workspace-write",
                  "--ask-for-approval", "never", "--ignore-user-config",
                  "--ephemeral", "--skip-git-repo-check", "--output-schema",
                  str(schema), "--output-last-message", str(answer), "-"],
                 cwd=root, prompt=prompt, timeout=self.timeout)
            try:
                if answer.stat().st_size > MAX_OUTPUT:
                    raise ValueError("Codex answer too large")
                data = json.loads(answer.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("Codex answer is missing or invalid") from None
        if not isinstance(data, dict) or set(data) != {"summary"}:
            raise ValueError("Codex answer has an invalid shape")
        return _small(data["summary"], MAX_SUMMARY)


@dataclass(frozen=True)
class LivePytest:
    workspace: Path
    python: Path
    args: tuple[str, ...] = ("-q",)
    timeout: int = 300

    def run(self) -> TestResult:
        root = _gate(self.workspace)
        python = self.python.resolve(strict=True)
        if not python.is_file() or not python.is_relative_to(root):
            raise ValueError("pytest must use the fictional workspace Python")
        if any(arg.startswith(("--basetemp", "--junitxml", "--junit-xml")) for arg in self.args):
            raise ValueError("pytest result paths are managed by the adapter")
        parent = root / "tmp" / "agent-loop-pytest" / uuid4().hex
        parent.mkdir(parents=True, exist_ok=False)
        results = parent / "results.xml"
        try:
            result = _run([str(python), "-m", "pytest", *self.args,
                           "--basetemp", str(parent / "run"),
                           "--junitxml", str(results)],
                          cwd=root, prompt=None, timeout=self.timeout,
                          exit_codes=(0, 1))
        except RuntimeError:
            return TestResult("not_run", "pytest could not complete; inspect the isolated environment")
        except OutputLimitError:
            return TestResult("not_run", "pytest output exceeded the limit")
        except ValueError:
            return TestResult("not_run", "pytest output or timeout configuration is invalid")
        try:
            if results.stat().st_size > 1024 * 1024:
                raise ValueError("Large test report")
            tree = ET.parse(results)
            node = tree.getroot()
            if node.tag == "testsuites" and len(node) == 1:
                node = node[0]
            if node.tag != "testsuite":
                raise ValueError("Invalid test report")
            counts = [int(node.attrib[key]) for key in ("tests", "failures", "errors", "skipped")]
            total, failed, errors, skipped = counts
            passed = total - failed - errors - skipped
            if min(counts + [passed]) < 0 or total == 0:
                raise ValueError("Invalid test counts")
        except (OSError, ValueError, KeyError, ET.ParseError):
            return TestResult("not_run", "pytest JUnit result was missing or invalid")
        detail = f"{passed} passed, {failed} failed, {errors} errors, {skipped} skipped"
        if result.returncode == 0 and failed == errors == 0:
            return TestResult("passed", detail)
        if result.returncode == 1 and failed + errors > 0:
            return TestResult("failed", detail)
        return TestResult("not_run", "pytest exit status conflicts with JUnit result")


@dataclass(frozen=True)
class ReviewPackage:
    requirements: str
    changes: str
    new_files: dict[str, str]


def _git(root: Path, *args: str) -> str:
    return _run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                cwd=root, prompt=None, timeout=30).stdout


def _git_names(raw: str) -> list[str]:
    if raw and not raw.endswith("\0"):
        raise ValueError("Invalid Git path list")
    names = raw[:-1].split("\0") if raw else []
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ValueError("Duplicate Git paths")
    return names


def _paths(raw: str, root: Path) -> list[str]:
    names = _git_names(raw)
    for name in names:
        parts = name.replace("\\", "/").split("/")
        if (not name or name.startswith(("/", "\\")) or ":" in name or
            any(part in ("", ".", "..") for part in parts) or
            any(part.lower() in BLOCKED_PARTS for part in parts) or
            parts[-1].lower() in BLOCKED_NAMES or
            (Path(name).suffix.lower() not in ALLOWED_SUFFIXES and
             name != ".fictional-agent-workspace" and parts[-1].lower() != ".gitignore")):
            raise ValueError("Review path is not allowed")
        path = root / name
        if path.is_symlink() or (path.exists() and not path.resolve(strict=True).is_relative_to(root)):
            raise ValueError("Review path leaves the workspace")
    return names


def _safe_content(value: str) -> str:
    if "\0" in value or SENSITIVE_CONTENT.search(value):
        raise ValueError("Review content is not allowed")
    return value


def _check_ignored_integrity(root: Path, explicit: list[str]) -> None:
    # --directory folds an ignored tree into one path, so .venv never floods
    # stdout. A non-runtime ignored path is an incomplete review snapshot.
    raw = _git(root, "ls-files", "-z", "--others", "--ignored",
               "--exclude-standard", "--directory")
    try:
        names = _git_names(raw)
    except ValueError:
        raise ReviewIntegrityError("Review ignored path listing is invalid") from None
    for name in names:
        parts = name.replace("\\", "/").rstrip("/").split("/")
        if (name.startswith(("/", "\\")) or ":" in name or
            any(part in ("", ".", "..") for part in parts)):
            raise ReviewIntegrityError("Review snapshot contains an unsafe ignored path")
        path = root.joinpath(*parts)
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ReviewIntegrityError("Review snapshot contains an unsafe ignored path")
        relative = "/".join(parts)
        if relative in APPROVED_IGNORED_FILES and path.is_file():
            continue
        runtime = (relative in APPROVED_RUNTIME_ROOTS or
                   (parts[-1] == "__pycache__" and
                    not any(part.lower() in BLOCKED_PARTS for part in parts[:-1])))
        if runtime and path.is_dir():
            continue
        if relative in explicit and path.is_file():
            continue
        raise ReviewIntegrityError("Review snapshot contains unapproved ignored paths")


def _snapshot(root: Path, base_commit: str,
              explicit_ignored_files: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    changed = _paths(_git(root, "diff", "--no-ext-diff", "--no-textconv",
                          "--no-renames", "--name-only", "-z", base_commit, "--"), root)
    untracked = _paths(_git(root, "ls-files", "-z", "--others", "--exclude-standard"), root)
    untracked = [name for name in untracked if name != ".fictional-agent-workspace"]
    if not isinstance(explicit_ignored_files, tuple) or any(
        not isinstance(name, str) for name in explicit_ignored_files
    ):
        raise ValueError("Explicit review paths must be a tuple of strings")
    explicit = _paths("".join(name + "\0" for name in explicit_ignored_files), root)
    if tuple(explicit) != explicit_ignored_files or ".fictional-agent-workspace" in explicit:
        raise ValueError("Invalid explicit review path")
    for name in explicit:
        ignored = _git(root, "ls-files", "-z", "--others", "--ignored",
                       "--exclude-standard", "--", ":(literal)" + name)
        if ignored != name + "\0":
            raise ValueError("Explicit review file is not ignored by Git")
    _check_ignored_integrity(root, explicit)
    untracked.extend(explicit)
    if len(untracked) != len(set(untracked)):
        raise ValueError("Duplicate review paths")
    if set(changed) & set(untracked):
        raise ValueError("Overlapping review paths")
    diff = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                "--binary", base_commit, "--")
    if len(diff.encode("utf-8")) > MAX_DIFF or "GIT binary patch" in diff:
        raise ValueError("Review diff is too large or binary")
    _safe_content(diff)
    new_files = {}
    for name in untracked:
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("New review file is not a local regular file")
        if path.stat().st_size > MAX_NEW_FILE:
            raise ValueError("New review file is too large")
        try:
            new_files[name] = _safe_content(path.read_text(encoding="utf-8"))
        except UnicodeError:
            raise ValueError("New review file is not UTF-8 text") from None
    if not diff and not new_files:
        raise ValueError("Review snapshot has no changes")
    return diff, new_files


def collect_review_package(root: Path, requirements: str, base_commit: str,
                           explicit_ignored_files: tuple[str, ...] = ()) -> ReviewPackage:
    """Take two matching read-only snapshots; fail closed on change or Git error."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base_commit):
        raise ValueError("Review base must be a full commit ID")
    if Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve(strict=True) != root:
        raise ValueError("Review workspace is not the Git root")
    if _git(root, "rev-parse", "--verify", f"{base_commit}^{{commit}}").strip().lower() != base_commit.lower():
        raise ValueError("Review base is not an exact commit")
    first = _snapshot(root, base_commit, explicit_ignored_files)
    second = _snapshot(root, base_commit, explicit_ignored_files)
    if first != second:
        raise ValueError("Review workspace changed during snapshot")
    return ReviewPackage(_small(_safe_content(requirements), MAX_PROMPT), *second)


@dataclass(frozen=True)
class LiveClaude:
    workspace: Path
    requirements: str
    base_commit: str
    executable: str = "claude"
    timeout: int = 600
    explicit_ignored_files: tuple[str, ...] = ()

    def review(self, task: str, implementation: str, tests: TestResult) -> str:
        root = _gate(self.workspace)
        package = collect_review_package(root, self.requirements, self.base_commit,
                                         self.explicit_ignored_files)
        payload = {
            "task": _safe_content(_small(task, MAX_PROMPT)),
            "requirements": package.requirements,
            "changes": package.changes, "new_files": package.new_files,
            "base_commit": self.base_commit,
            "implementation": _safe_content(_small(implementation, MAX_SUMMARY)),
            "tests": {"status": tests.status,
                      "detail": _safe_content(_small(tests.detail, MAX_SUMMARY))},
        }
        prompt = _small("Review the supplied fictional changes. Return only findings.\n" +
                        json.dumps(payload, ensure_ascii=False), MAX_PROMPT)
        result = _run([self.executable, "-p", "--restricted", "--safe-mode",
                       "--permission-mode", "dontAsk", "--tools", "Read,Glob,Grep",
                       "--disallowedTools", "mcp__*", "--strict-mcp-config",
                       "--no-session-persistence", "--output-format", "json",
                       "--json-schema", json.dumps(REVIEW_SCHEMA)],
                      cwd=root, prompt=prompt, timeout=self.timeout)
        try:
            envelope = json.loads(result.stdout)
            if not isinstance(envelope, dict) or envelope.get("is_error") is not False:
                raise ValueError("Claude result failed")
            review = envelope["structured_output"]
            raw = _small(json.dumps(review, ensure_ascii=False), MAX_REVIEW)
            parse_review(raw)
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Claude review is missing or invalid") from None
        return raw
