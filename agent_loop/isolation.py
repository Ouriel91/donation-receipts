"""Diagnostic probes for a disposable fictional Windows guest.

Probe results cannot attest the VM, token, or canary setup. No result opens the
live gate. This module never includes paths or file contents in its report.
"""

import argparse
import errno
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from agent_loop.live import (APPROVED_RUNTIME_ROOTS, CHILD_ENV_KEYS,
                             WINDOWS_CHILD_ENV_KEYS, _run, _workspace)


PASS = "PASS"
FAIL = "FAIL"
NOT_VERIFIED = "NOT_VERIFIED"
CANARY_KINDS = frozenset({"absolute", "parent", "other_drive", "unc",
                          "symlink", "junction"})
MAX_SCAN_ENTRIES = 512
_READ_PROBE = (
    "import os,stat,sys\n"
    "p=sys.argv[1]\n"
    "if os.name=='nt':\n"
    " import ctypes\n"
    " from ctypes import wintypes\n"
    " k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
    " k.GetFileAttributesW.argtypes=[wintypes.LPCWSTR]\n"
    " k.GetFileAttributesW.restype=wintypes.DWORD\n"
    " k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,"
    "wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]\n"
    " k.CreateFileW.restype=wintypes.HANDLE\n"
    " k.CloseHandle.argtypes=[wintypes.HANDLE]\n"
    " k.CloseHandle.restype=wintypes.BOOL\n"
    " a=k.GetFileAttributesW(p)\n"
    " if a==0xffffffff: code=ctypes.get_last_error()\n"
    " elif a&0x10: print('directory'); code=None\n"
    " else:\n"
    "  h=k.CreateFileW(p,0x80000000,7,None,3,0,None)\n"
    "  if h==ctypes.c_void_p(-1).value: code=ctypes.get_last_error()\n"
    "  else: k.CloseHandle(h); print('readable'); code=None\n"
    " if code is not None:\n"
    "  print('access_denied_win5' if code==5 else 'sharing_violation' if code==32 "
    "else 'missing' if code in (2,3) else 'system_error')\n"
    "else:\n"
    " try:\n"
    "  s=os.stat(p)\n"
    "  if stat.S_ISDIR(s.st_mode): print('directory')\n"
    "  else:\n"
    "   fd=os.open(p,os.O_RDONLY); os.close(fd); print('readable')\n"
    " except FileNotFoundError: print('missing')\n"
    " except IsADirectoryError: print('directory')\n"
    " except PermissionError: print('permission_other')\n"
    " except OSError: print('system_error')\n"
)
_HANDLE_PROBE = (
    "import errno,os,sys\n"
    "try:\n"
    " h=int(sys.argv[1])\n"
    " if os.name=='nt':\n"
    "  import msvcrt\n"
    "  h=msvcrt.open_osfhandle(h,os.O_RDONLY)\n"
    " os.read(h,1); print('inherited')\n"
    "except OSError as e:\n"
    " print('not_inherited' if getattr(e,'winerror',None)==6 or "
    "e.errno==errno.EBADF else 'handle_error')\n"
    "except ValueError: print('handle_error')\n"
)
_ENV_PROBE = (
    "import json,os,sys\n"
    "allowed=set(json.loads(sys.argv[2])); root=os.path.normcase(sys.argv[1])\n"
    "try:\n"
    " paths=[os.path.normcase(os.path.abspath(os.environ[k])) for k in ('TEMP','TMP')]\n"
    " ok={k.casefold() for k in os.environ}=={k.casefold() for k in allowed} "
    "and all(os.path.commonpath([root,p])==root "
    "for p in paths)\n"
    " ok=ok and all(not os.path.normcase(os.path.abspath(p)).startswith(root+os.sep) "
    "for p in os.environ['PATH'].split(os.pathsep))\n"
    " print('allowlisted' if ok else 'unexpected_environment')\n"
    "except (KeyError,ValueError,OSError): print('environment_error')\n"
)


def _result(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _write_error(exc: OSError, path: Path, operation: str) -> dict[str, str]:
    """Classify a failed write probe; a denial alone never attests an ACL."""
    code = getattr(exc, "winerror", None)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if operation == "mkdir":
            kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
            kernel.CreateDirectoryW.restype = wintypes.BOOL
            if kernel.CreateDirectoryW(str(path), None):
                os.rmdir(path)
                return _result(FAIL, "directory_created_on_retry")
            code = ctypes.get_last_error()
        else:
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            disposition = 1 if operation == "create" else 3  # CREATE_NEW / OPEN_EXISTING
            handle = kernel.CreateFileW(str(path), 0x40000000, 7, None,
                                        disposition, 0, None)
            if handle != ctypes.c_void_p(-1).value:
                kernel.CloseHandle(handle)
                if operation == "create":
                    path.unlink()
                return _result(FAIL, "file_write_access_on_retry")
            code = ctypes.get_last_error()
            if code == 5 and operation == "open":
                kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
                kernel.GetFileAttributesW.restype = wintypes.DWORD
                attributes = kernel.GetFileAttributesW(str(path))
                if attributes != 0xffffffff and attributes & 1:
                    return _result(NOT_VERIFIED, "readonly_attribute")
        reasons = {5: "access_denied_needs_acl_witness", 32: "sharing_violation",
                   2: "missing", 3: "missing", 19: "write_protected"}
        return _result(NOT_VERIFIED, reasons.get(code, "write_system_error"))
    if isinstance(exc, FileNotFoundError):
        return _result(NOT_VERIFIED, "missing")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return _result(NOT_VERIFIED, "access_denied_needs_acl_witness")
    return _result(NOT_VERIFIED, "write_system_error")


def _fictional_root(path: Path) -> Path:
    root = _workspace(path)
    project = Path(__file__).resolve().parents[1]
    if root == project or project.is_relative_to(root):
        raise ValueError("Preflight requires a separate fictional workspace")
    return root


def _canary_status(workspace: Path, path: str) -> dict[str, str]:
    try:
        result = _run([sys.executable, "-c", _READ_PROBE, path], cwd=workspace,
                      prompt=None, timeout=5)
    except (OSError, RuntimeError, ValueError):
        return _result(NOT_VERIFIED, "read_probe_failed")
    observed = result.stdout.strip()
    if observed == "readable":
        return _result(FAIL, "canary_readable")
    if observed == "access_denied_win5":
        # The agent token cannot prove that the protected file exists or is valid.
        return _result(NOT_VERIFIED, "access_denied_needs_separate_witness")
    reasons = {"sharing_violation", "directory", "missing", "system_error",
               "permission_other"}
    return _result(NOT_VERIFIED, observed if observed in reasons else "read_probe_invalid")


def _valid_canary_location(kind: str, path: str, root: Path) -> bool:
    candidate = Path(path)
    if kind not in {"symlink", "junction"} and Path(os.path.normpath(path)).is_relative_to(root):
        return False
    if kind == "absolute":
        return ".." not in candidate.parts and not path.startswith("\\\\")
    if kind == "parent":
        return ".." in candidate.parts
    if kind == "other_drive":
        return bool(candidate.drive and candidate.drive.lower() != root.drive.lower())
    if kind == "unc":
        return path.startswith("\\\\")
    if kind == "symlink":
        linked = candidate.parent.is_symlink()
    if kind == "junction":
        linked = candidate.parent.is_junction() if hasattr(candidate.parent, "is_junction") else False
    if kind in {"symlink", "junction"}:
        if not linked or not candidate.parent.parent.is_relative_to(root):
            return False
        target = os.readlink(candidate.parent)
        if target.startswith("\\\\?\\"):
            target = target[4:]
        return (Path(target).is_absolute() and
                not Path(os.path.normpath(target)).is_relative_to(root))
    return False


def _probe_directory(path: Path, root: Path) -> dict[str, str]:
    """Try only unique fictional entries and remove only entries we created."""
    if not path.is_relative_to(root) or path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    ):
        return _result(NOT_VERIFIED, "unsafe_probe_directory")
    if not path.exists():
        try:
            os.mkdir(path)
        except OSError as exc:
            return _write_error(exc, path, "mkdir")
        try:
            marker = path / ("agent-loop-probe-" + uuid4().hex)
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError:
                pass  # Creating this directory already disproves the restriction.
            else:
                os.close(fd)
                marker.unlink()
        finally:
            os.rmdir(path)
        return _result(FAIL, "runtime_directory_created")
    if not path.is_dir():
        return _result(NOT_VERIFIED, "runtime_path_not_directory")
    marker = path / ("agent-loop-probe-" + uuid4().hex)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return _write_error(exc, marker, "create")
    os.close(fd)
    marker.unlink()
    return _result(FAIL, "runtime_directory_writable")


def _combine(results: list[dict[str, str]], success_reason: str) -> dict[str, str]:
    for status in (FAIL, NOT_VERIFIED):
        for result in results:
            if result["status"] == status:
                return result
    return _result(PASS, success_reason)


def _runtime_status(workspace: Path) -> dict[str, str]:
    root = _fictional_root(workspace)
    candidates = {root / name for name in APPROVED_RUNTIME_ROOTS}
    stack = [root]
    scanned = 0
    unscanned_runtime_tree = False
    while stack:
        parent = stack.pop()
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        return _result(NOT_VERIFIED, "runtime_scan_limit")
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    child = Path(entry.path)
                    if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                        return _result(NOT_VERIFIED, "runtime_link_unverified")
                    name = child.name.casefold() if os.name == "nt" else child.name
                    if name == "__pycache__":
                        candidates.add(child)
                        unscanned_runtime_tree = True
                    elif parent == root and name in APPROVED_RUNTIME_ROOTS | {".git"}:
                        if name != ".git":
                            candidates.add(child)
                            unscanned_runtime_tree = True
                        continue
                    elif name != ".git":
                        stack.append(child)
        except OSError:
            return _result(NOT_VERIFIED, "runtime_scan_error")
        candidates.add(parent / "__pycache__")
    results = [_probe_directory(path, root) for path in sorted(candidates)]
    if unscanned_runtime_tree:
        results.append(_result(NOT_VERIFIED, "runtime_tree_not_fully_audited"))
    return _combine(results, "runtime_writes_denied")


def _probe_existing_file(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        return _result(NOT_VERIFIED, "git_file_missing_or_link")
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as exc:
        return _write_error(exc, path, "open")
    os.close(fd)
    return _result(FAIL, "git_file_writable")


def _probe_git_directory(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_dir() or (
        hasattr(path, "is_junction") and path.is_junction()
    ):
        return _result(NOT_VERIFIED, "git_directory_missing_or_link")
    results = []
    marker = path / ("agent-loop-probe-" + uuid4().hex)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        results.append(_write_error(exc, marker, "create"))
    else:
        os.close(fd)
        marker.unlink()
        results.append(_result(FAIL, "git_file_created"))
    directory = path / ("agent-loop-probe-" + uuid4().hex)
    try:
        os.mkdir(directory)
    except OSError as exc:
        results.append(_write_error(exc, directory, "mkdir"))
    else:
        os.rmdir(directory)
        results.append(_result(FAIL, "git_directory_created"))
    return _combine(results, "git_directory_writes_denied")


def _git_permissions_status(workspace: Path) -> dict[str, str]:
    root = _fictional_root(workspace)
    git_dir = root / ".git"
    if (not git_dir.is_dir() or git_dir.is_symlink() or
        (hasattr(git_dir, "is_junction") and git_dir.is_junction())):
        return _result(NOT_VERIFIED, "git_repository_missing_or_link")
    if (git_dir / "index.lock").exists():
        return _result(NOT_VERIFIED, "git_index_lock_present")
    if any(not (git_dir / name).is_file() for name in ("index", "config", "HEAD")):
        return _result(NOT_VERIFIED, "git_required_file_missing")
    if any(not (git_dir / name).is_dir() for name in ("hooks", "refs", "objects")):
        return _result(NOT_VERIFIED, "git_required_directory_missing")
    stack = [git_dir]
    scanned = 0
    results = []
    while stack:
        parent = stack.pop()
        results.append(_probe_git_directory(parent))
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        return _result(NOT_VERIFIED, "git_scan_limit")
                    path = Path(entry.path)
                    if entry.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                        return _result(NOT_VERIFIED, "git_link_unverified")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        results.append(_probe_existing_file(path))
                    else:
                        return _result(NOT_VERIFIED, "git_special_path")
        except OSError:
            return _result(NOT_VERIFIED, "git_scan_error")
    return _combine(results, "git_metadata_writes_denied")


def _git_flag_status(workspace: Path, *, probe_mutations: bool = True) -> dict[str, str]:
    root = _fictional_root(workspace)
    git = shutil.which("git")
    if git is None:
        return _result(NOT_VERIFIED, "git_executable_missing")
    prefix = [git, "--no-optional-locks", "-c", "core.fsmonitor=false"]
    try:
        tracked = _run([*prefix, "ls-files", "-v", "-z"], cwd=root,
                       prompt=None, timeout=5).stdout
    except (OSError, RuntimeError, ValueError):
        return _result(NOT_VERIFIED, "git_listing_failed")
    entries = tracked.rstrip("\0").split("\0") if tracked.endswith("\0") else []
    if not any(entry[2:] == "code.py" for entry in entries):
        return _result(NOT_VERIFIED, "git_fixture_not_tracked")
    if any(not entry.startswith("H ") for entry in entries):
        return _result(FAIL, "git_index_flags_present")
    if not probe_mutations:
        return _result(NOT_VERIFIED, "git_flag_mutations_not_probed")
    if (root / ".git" / "index.lock").exists():
        return _result(NOT_VERIFIED, "git_index_lock_present")
    for flag in ("--assume-unchanged", "--skip-worktree"):
        try:
            result = _run([*prefix, "update-index", flag, "--", "code.py"],
                          cwd=root, prompt=None, timeout=5,
                          exit_codes=(0, 1, 128, 129))
        except (OSError, RuntimeError, ValueError):
            return _result(NOT_VERIFIED, "git_command_failed")
        if result.returncode != 0:
            continue
        return _result(FAIL, "git_index_flag_changed")
    return _result(NOT_VERIFIED, "git_commands_failed_without_acl_proof")


def _git_write_status(workspace: Path) -> dict[str, object]:
    permissions = _git_permissions_status(workspace)
    flags = _git_flag_status(workspace, probe_mutations=permissions["status"] != FAIL)
    overall = _combine([permissions, flags], "git_metadata_protected")
    return {**overall, "permissions": permissions, "flags": flags}


def _handle_status(workspace: Path) -> dict[str, str]:
    root = _fictional_root(workspace)
    path = root / ("handle-canary-" + uuid4().hex)
    path.write_text("fictional", encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    try:
        os.set_inheritable(fd, True)
        handle = fd
        if os.name == "nt":
            import msvcrt
            handle = msvcrt.get_osfhandle(fd)
        try:
            result = _run([sys.executable, "-c", _HANDLE_PROBE, str(handle)],
                          cwd=root, prompt=None, timeout=5)
        except (OSError, RuntimeError, ValueError):
            return _result(NOT_VERIFIED, "handle_probe_failed")
        observed = result.stdout.strip()
        if observed == "not_inherited":
            return _result(PASS, "handle_not_inherited")
        return (_result(FAIL, "handle_inherited") if observed == "inherited" else
                _result(NOT_VERIFIED, "handle_probe_invalid"))
    finally:
        os.close(fd)
        path.unlink()


def _env_status(workspace: Path) -> dict[str, str]:
    root = _fictional_root(workspace)
    try:
        expected = WINDOWS_CHILD_ENV_KEYS if os.name == "nt" else CHILD_ENV_KEYS
        allowed = json.dumps(sorted(expected))
        result = _run([sys.executable, "-c", _ENV_PROBE, str(root), allowed],
                      cwd=root, prompt=None, timeout=5)
    except (OSError, RuntimeError, ValueError):
        return _result(NOT_VERIFIED, "environment_probe_failed")
    return (_result(PASS, "environment_allowlisted") if result.stdout.strip() == "allowlisted"
            else _result(NOT_VERIFIED, "environment_not_allowlisted"))


def run_preflight(workspace: Path, manifest: Path) -> dict[str, dict[str, str]]:
    root = _fictional_root(workspace)
    if not manifest.resolve(strict=True).is_relative_to(root):
        raise ValueError("Canary manifest must be in the fictional workspace")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"canaries"} or not isinstance(data["canaries"], list):
        raise ValueError("Invalid canary manifest")
    canaries = data["canaries"]
    if len(canaries) != len(CANARY_KINDS):
        raise ValueError("All six canary kinds are required")
    kinds = set()
    results = {}
    for item in canaries:
        if not isinstance(item, dict) or set(item) != {"kind", "path"}:
            raise ValueError("Invalid canary entry")
        kind, path = item["kind"], item["path"]
        if (not isinstance(kind, str) or kind not in CANARY_KINDS or kind in kinds or
            not isinstance(path, str) or not path.endswith("agent-loop-canary.txt") or
            len(path) > 512 or not Path(path).is_absolute() or
            not _valid_canary_location(kind, path, root)):
            raise ValueError("Invalid fictional canary path")
        kinds.add(kind)
        results[kind] = _canary_status(root, path)
    if kinds != CANARY_KINDS:
        raise ValueError("Canary kinds are incomplete")
    results["environment"] = _env_status(root)
    results["git_write"] = _git_write_status(root)
    results["runtime_write"] = _runtime_status(root)
    results["handle_inheritance"] = _handle_status(root)
    return results


def _guest_guard(workspace: Path, acknowledged: bool) -> bool:
    """Accidental-host-use guard only; a flag and path cannot attest a VM."""
    if os.name != "nt" or not acknowledged:
        return False
    if str(workspace).casefold().rstrip("\\/") != r"C:\AgentLab\work".casefold():
        return False
    try:
        root = _fictional_root(workspace)
    except (OSError, ValueError):
        return False
    project = Path(__file__).resolve().parents[1]
    return not root.is_relative_to(project) and not Path.home().is_relative_to(root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline fictional VM preflight; never run on host")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--acknowledge-disposable-guest", action="store_true",
                        help="Confirm manual VM inspection; this flag is not isolation proof")
    args = parser.parse_args()
    if not _guest_guard(args.workspace, args.acknowledge_disposable_guest):
        print(json.dumps({"status": "NOT_VERIFIED", "reason": "guest_guard_not_satisfied"}))
        return 1
    try:
        results = run_preflight(args.workspace, args.manifest)
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "NOT_VERIFIED", "reason": "preflight_configuration_invalid"}))
        return 1
    statuses = {item["status"] for item in results.values()}
    overall = FAIL if FAIL in statuses else NOT_VERIFIED if NOT_VERIFIED in statuses else PASS
    # PASS still describes only local probes; manual guest evidence is required.
    if overall == PASS:
        overall = NOT_VERIFIED
    print(json.dumps({"status": overall, "checks": results,
                      "reason": "independent_guest_evidence_required" if overall == NOT_VERIFIED else
                                "security_condition_failed"}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
