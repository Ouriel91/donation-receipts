"""Only local Python/Git and fictional temporary files are used."""

import json
import os
import subprocess
import sys

import pytest

from agent_loop import isolation
from agent_loop import live as live_module
from agent_loop.live import _child_env, _run


def _fictional(tmp_path):
    root = tmp_path / "fictional"
    root.mkdir()
    (root / ".fictional-agent-workspace").write_text("fiction only", encoding="utf-8")
    (root / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def test_child_environment_omits_parent_secrets_and_uses_workspace_temp(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-sensitive-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fictional-sensitive-value")
    monkeypatch.setenv("GITHUB_TOKEN", "fictional-sensitive-value")
    env = _child_env(root)
    allowed = {"TEMP", "TMP", "PATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE",
               "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_OPTIONAL_LOCKS"}
    if os.name == "nt":
        allowed |= {"SystemRoot", "WINDIR"}
    assert set(env) == allowed
    assert env["TEMP"].startswith(str(root))
    result = _run([sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
                  cwd=root, prompt=None, timeout=10)
    actual = json.loads(result.stdout)
    assert {key.casefold() for key in actual} == {key.casefold() for key in allowed}
    assert "fictional-sensitive-value" not in result.stdout


@pytest.mark.parametrize("name", ["tmp"])
def test_child_environment_rejects_workspace_path_link_to_outside(tmp_path, name):
    root = _fictional(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / name).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a local test symlink is unavailable")
    with pytest.raises(ValueError, match="unsafe"):
        _child_env(root)


def test_canary_readability_is_failure_and_contents_are_not_reported(tmp_path):
    root = _fictional(tmp_path)
    canary = tmp_path / "outside" / "agent-loop-canary.txt"
    canary.parent.mkdir()
    canary.write_text("fictional secret marker", encoding="utf-8")
    assert isolation._canary_status(root, str(canary))["status"] == "FAIL"
    missing = isolation._canary_status(root, str(tmp_path / "missing" / canary.name))
    assert missing == {"status": "NOT_VERIFIED", "reason": "missing"}
    assert isolation._canary_status(root, str(canary.parent)) == {
        "status": "NOT_VERIFIED", "reason": "directory"}


@pytest.mark.parametrize("observed,status", [
    ("not_inherited", "PASS"), ("inherited", "FAIL"), ("handle_error", "NOT_VERIFIED")
])
def test_inheritable_handle_classifier_with_mocked_subprocess(tmp_path, monkeypatch,
                                                              observed, status):
    root = _fictional(tmp_path)
    monkeypatch.setattr(isolation, "_run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, observed + "\n", ""))
    assert isolation._handle_status(root)["status"] == status


def test_runtime_and_git_writes_fail_preflight(tmp_path):
    root = _fictional(tmp_path)
    (root / "tmp").mkdir()
    assert isolation._runtime_status(root)["status"] == "FAIL"
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "code.py"], cwd=root, check=True, capture_output=True)
    assert isolation._git_write_status(root)["status"] == "FAIL"


def test_preflight_blocks_on_one_failed_check_without_canary_contents(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    manifest = root / "canaries.json"
    manifest.write_text(json.dumps({"canaries": [
        {"kind": kind, "path": str(tmp_path / kind / "agent-loop-canary.txt")}
        for kind in sorted(isolation.CANARY_KINDS)
    ]}), encoding="utf-8")
    monkeypatch.setattr(isolation, "_valid_canary_location", lambda *args: True)
    monkeypatch.setattr(isolation, "_canary_status", lambda root, path:
                        {"status": "NOT_VERIFIED", "reason": "access_denied_needs_separate_witness"})
    monkeypatch.setattr(isolation, "_git_write_status", lambda root:
                        {"status": "FAIL", "reason": "git_file_writable"})
    monkeypatch.setattr(isolation, "_runtime_status", lambda root:
                        {"status": "PASS", "reason": "runtime_writes_denied"})
    monkeypatch.setattr(isolation, "_handle_status", lambda root:
                        {"status": "PASS", "reason": "handle_not_inherited"})
    results = isolation.run_preflight(root, manifest)
    assert results["git_write"]["status"] == "FAIL"
    assert not all(value["status"] == "PASS" for value in results.values())
    assert "agent-loop-canary" not in json.dumps(results)


def test_preflight_cli_never_opens_live_gate(tmp_path, monkeypatch, capsys):
    root = _fictional(tmp_path)
    manifest = root / "canaries.json"
    manifest.write_text('{"canaries": []}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["preflight", "--workspace", str(root),
                                      "--manifest", str(manifest)])
    assert isolation.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_VERIFIED"

    monkeypatch.setattr(isolation, "_guest_guard", lambda *args: True)
    monkeypatch.setattr(isolation, "run_preflight", lambda *args:
                        {"all": {"status": "PASS", "reason": "test_only"}})
    assert isolation.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_VERIFIED"
    from agent_loop.live import LIVE_ISOLATION_VERIFIED
    assert LIVE_ISOLATION_VERIFIED is False


def test_preflight_rejects_unproved_link_and_missing_category(tmp_path):
    root = _fictional(tmp_path)
    manifest = root / "canaries.json"
    manifest.write_text(json.dumps({"canaries": [
        {"kind": kind, "path": str(tmp_path / kind / "agent-loop-canary.txt")}
        for kind in sorted(isolation.CANARY_KINDS)
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="canary path"):
        isolation.run_preflight(root, manifest)
    manifest.write_text('{"canaries": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="six canary"):
        isolation.run_preflight(root, manifest)


@pytest.mark.skipif(os.name != "nt", reason="Windows path categories")
def test_windows_canary_categories_require_distinct_path_forms(tmp_path):
    root = _fictional(tmp_path)
    same_drive = root.drive + "\\protected\\agent-loop-canary.txt"
    assert isolation._valid_canary_location("absolute", same_drive, root)
    assert not isolation._valid_canary_location("parent", same_drive, root)
    assert isolation._valid_canary_location(
        "parent", str(root / ".." / "protected" / "agent-loop-canary.txt"), root)
    other = "D:" if root.drive.upper() != "D:" else "E:"
    assert isolation._valid_canary_location(
        "other_drive", other + "\\protected\\agent-loop-canary.txt", root)
    assert isolation._valid_canary_location(
        "unc", "\\\\localhost\\canaries\\agent-loop-canary.txt", root)


def test_missing_runtime_roots_are_not_os_denials(tmp_path):
    root = _fictional(tmp_path)
    result = isolation._runtime_status(root)
    assert result["status"] == "FAIL"
    assert not (root / "node_modules").exists()  # Probe removed only its own fixture.


def test_runtime_link_outside_workspace_is_not_verified(tmp_path):
    root = _fictional(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "node_modules").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a local test symlink is unavailable")
    assert isolation._runtime_status(root)["status"] == "NOT_VERIFIED"


def test_sharing_violation_is_not_access_denied(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    monkeypatch.setattr(isolation, "_run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, "sharing_violation\n", ""))
    result = isolation._canary_status(root, str(tmp_path / "agent-loop-canary.txt"))
    assert result["status"] == "NOT_VERIFIED"
    assert result["reason"] == "sharing_violation"


def test_child_path_has_no_agent_writable_tool_directories(tmp_path):
    root = _fictional(tmp_path)
    child_path = _child_env(root)["PATH"].split(os.pathsep)
    assert not any(part.startswith(str(root)) for part in child_path)


def test_environment_probe_rejects_unlisted_child_variable(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    original = live_module._child_env
    monkeypatch.setattr(live_module, "_child_env", lambda where:
                        {**original(where), "EXTRA_FAKE_VALUE": "fictional"})
    assert isolation._env_status(root) == {
        "status": "NOT_VERIFIED", "reason": "environment_not_allowlisted"}


def test_runtime_checks_every_approved_root_and_nested_pycache_without_scanning_venv(
    tmp_path, monkeypatch
):
    root = _fictional(tmp_path)
    nested = root / "feature" / "inner"
    nested.mkdir(parents=True)
    venv = root / ".venv"
    venv.mkdir()
    for number in range(40):
        (venv / f"package_{number}.py").write_text("fictional", encoding="utf-8")
    seen = []
    monkeypatch.setattr(isolation, "MAX_SCAN_ENTRIES", 12)
    monkeypatch.setattr(isolation, "_probe_directory", lambda path, where: (
        seen.append(path.relative_to(where).as_posix()) or
        {"status": "PASS", "reason": "mocked_denial"}))
    result = isolation._runtime_status(root)
    assert result == {"status": "NOT_VERIFIED", "reason": "runtime_tree_not_fully_audited"}
    assert isolation.APPROVED_RUNTIME_ROOTS <= set(seen)
    assert "feature/__pycache__" in seen
    assert "feature/inner/__pycache__" in seen


@pytest.mark.parametrize("observed,expected", [
    ("access_denied_win5", "access_denied_needs_separate_witness"),
    ("sharing_violation", "sharing_violation"),
    ("directory", "directory"),
    ("missing", "missing"),
    ("system_error", "system_error"),
])
def test_canary_error_classes_do_not_claim_isolation(tmp_path, monkeypatch, observed, expected):
    root = _fictional(tmp_path)
    monkeypatch.setattr(isolation, "_run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, observed + "\n", ""))
    assert isolation._canary_status(root, str(tmp_path / "agent-loop-canary.txt")) == {
        "status": "NOT_VERIFIED", "reason": expected}


def test_git_command_failure_is_not_reported_as_acl_denial(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    (root / ".git").mkdir()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "ls-files" in args:
            return subprocess.CompletedProcess(args, 0, "H code.py\0", "")
        return subprocess.CompletedProcess(args, 128, "", "")

    monkeypatch.setattr(isolation, "_run", fake_run)
    result = isolation._git_flag_status(root)
    assert result["status"] == "NOT_VERIFIED"
    assert result["reason"] == "git_commands_failed_without_acl_proof"
    assert sum("update-index" in args for args in calls) == 2


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_git_index_flags_detected_in_disposable_repository(tmp_path, flag):
    root = _fictional(tmp_path)

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    git("add", "code.py")
    git("-c", "user.name=Fiction", "-c", "user.email=fiction@example.invalid",
        "commit", "-q", "-m", "fictional base")
    git("update-index", flag, "--", "code.py")
    result = isolation._git_flag_status(root)
    assert result == {"status": "FAIL", "reason": "git_index_flags_present"}


def test_git_index_lock_and_missing_metadata_are_not_verified(tmp_path):
    root = _fictional(tmp_path)
    git_dir = root / ".git"
    git_dir.mkdir()
    assert isolation._git_permissions_status(root)["reason"] == "git_required_file_missing"
    (git_dir / "index.lock").write_text("fictional", encoding="utf-8")
    assert isolation._git_permissions_status(root)["reason"] == "git_index_lock_present"


def test_git_permission_probe_visits_required_metadata_in_fake_repo(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "code.py"], cwd=root, check=True, capture_output=True)
    files, directories = set(), set()
    monkeypatch.setattr(isolation, "_probe_existing_file", lambda path: (
        files.add(path.relative_to(root / ".git").as_posix()) or
        {"status": "PASS", "reason": "mocked_denial"}))
    monkeypatch.setattr(isolation, "_probe_git_directory", lambda path: (
        directories.add(path.relative_to(root / ".git").as_posix()) or
        {"status": "PASS", "reason": "mocked_denial"}))
    result = isolation._git_permissions_status(root)
    assert result["status"] == "PASS"  # Only mocked traversal, not OS isolation.
    assert {"index", "config", "HEAD"} <= files
    assert {"hooks", "refs", "objects"} <= directories


def test_git_runtime_error_is_not_acl_proof(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    (root / ".git").mkdir()

    def fake_run(args, **kwargs):
        if "ls-files" in args:
            return subprocess.CompletedProcess(args, 0, "H code.py\0", "")
        raise RuntimeError("fictional Git failure")

    monkeypatch.setattr(isolation, "_run", fake_run)
    assert isolation._git_flag_status(root) == {
        "status": "NOT_VERIFIED", "reason": "git_command_failed"}


def test_guest_guard_rejects_host_workspace_even_with_acknowledgement(tmp_path):
    root = _fictional(tmp_path)
    assert isolation._guest_guard(root, True) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_locked_fictional_canary_is_not_access_denial(tmp_path):
    import ctypes
    from ctypes import wintypes

    root = _fictional(tmp_path)
    canary = tmp_path / "agent-loop-canary.txt"
    canary.write_text("fictional", encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(canary), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        pytest.skip("Exclusive local file handle unavailable")
    try:
        result = isolation._canary_status(root, str(canary))
        assert result == {"status": "NOT_VERIFIED", "reason": "sharing_violation"}
        assert isolation._probe_existing_file(canary) == {
            "status": "NOT_VERIFIED", "reason": "sharing_violation"}
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateFileW semantics")
def test_windows_write_errors_do_not_claim_acl_proof(tmp_path):
    root = _fictional(tmp_path)
    missing = root / "missing.txt"
    assert isolation._write_error(FileNotFoundError(2, "missing"), missing,
                                  "open")["reason"] == "missing"
    readonly = root / "readonly.txt"
    readonly.write_text("fictional", encoding="utf-8")
    readonly.chmod(0o444)
    try:
        assert isolation._probe_existing_file(readonly) == {
            "status": "NOT_VERIFIED", "reason": "readonly_attribute"}
        assert isolation._write_error(PermissionError(13, "fictional"), readonly,
                                      "open") == {
            "status": "NOT_VERIFIED", "reason": "readonly_attribute"}
    finally:
        readonly.chmod(0o666)
    assert isolation._probe_directory(root / "new-runtime", root)["status"] == "FAIL"
    assert isolation._probe_existing_file(root / "code.py")["status"] == "FAIL"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle inheritance")
def test_real_fictional_handle_inheritance_positive_and_negative(tmp_path):
    import msvcrt

    root = _fictional(tmp_path)
    path = root / "handle.txt"
    path.write_text("fictional", encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    try:
        os.set_inheritable(fd, True)
        handle = msvcrt.get_osfhandle(fd)
        command = [sys.executable, "-c", isolation._HANDLE_PROBE, str(handle)]
        for close_fds, expected in ((False, "inherited"), (True, "not_inherited")):
            child = subprocess.run(command, cwd=root, env=_child_env(root),
                                   close_fds=close_fds, capture_output=True,
                                   text=True, timeout=5, check=True)
            assert child.stdout.strip() == expected
    finally:
        os.close(fd)


def test_git_write_preserves_permission_and_flag_findings(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    seen = []
    monkeypatch.setattr(isolation, "_git_permissions_status", lambda path: {
        "status": "FAIL", "reason": "fictional_metadata_writable"})

    def flags(path, *, probe_mutations):
        seen.append(probe_mutations)
        return {"status": "FAIL", "reason": "git_index_flags_present"}

    monkeypatch.setattr(isolation, "_git_flag_status", flags)
    result = isolation._git_write_status(root)
    assert result["permissions"]["reason"] == "fictional_metadata_writable"
    assert result["flags"]["reason"] == "git_index_flags_present"
    assert result["status"] == "FAIL"
    assert seen == [False]


def test_git_flags_detected_even_with_other_permission_failure(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    git("init", "-q")
    git("add", "code.py")
    git("update-index", "--skip-worktree", "--", "code.py")
    monkeypatch.setattr(isolation, "_git_permissions_status", lambda path: {
        "status": "FAIL", "reason": "fictional_metadata_writable"})
    result = isolation._git_write_status(root)
    assert result["permissions"]["status"] == "FAIL"
    assert result["flags"] == {"status": "FAIL", "reason": "git_index_flags_present"}


@pytest.mark.skipif(os.name != "nt", reason="Windows case folding")
def test_runtime_name_matching_is_case_insensitive_on_windows(tmp_path, monkeypatch):
    root = _fictional(tmp_path)
    (root / "NODE_MODULES").mkdir()
    (root / "src").mkdir()
    (root / "src" / "__PYCACHE__").mkdir()
    visited = set()
    monkeypatch.setattr(isolation, "_probe_directory", lambda path, where: (
        visited.add(path.relative_to(root).as_posix()) or
        {"status": "PASS", "reason": "mocked"}))
    result = isolation._runtime_status(root)
    assert result == {"status": "NOT_VERIFIED", "reason": "runtime_tree_not_fully_audited"}
    assert "node_modules" in {name.casefold() for name in visited}
    assert "src/__PYCACHE__" in visited
