# Phase 2A handoff for Claude re-review

## Version and scope

Branch `feat/agent-loop-isolation`, base `ad640a3`. Review the entire working
tree against that base and list untracked files with
`git status --short --untracked-files=all`; `git diff` alone omits them.
Tracked edits: `agent_loop/live.py`, `tests/test_agent_loop_live.py`.
Untracked files to inspect:

- `agent_loop/isolation.py`
- `tests/test_agent_loop_isolation.py`
- `docs/AGENT_LOOP_ISOLATION.md`
- `docs/AGENT_LOOP_ISOLATION_HANDOFF.md`
- `docs/fixtures/agent_loop_vm/.fictional-agent-workspace`
- `docs/fixtures/agent_loop_vm/.gitignore`
- `docs/fixtures/agent_loop_vm/code.py`
- `docs/fixtures/agent_loop_vm/canaries.example.json`
- `docs/fixtures/agent_loop_vm/offline-example.wsb`

No commit, push, merge, VM launch, system permission change, model invocation,
Gmail access, or donor-data operation occurred. The live gate is still False.

## Response to the four Major findings

| Finding | Change | Supporting host check | Isolation verdict |
|---|---|---|---|
| M1 runtime write | Probe all fixed ignored runtime roots, absent-root creation plus file write, and nested `__pycache__` with bounded traversal. Existing skipped runtime trees remain `NOT_VERIFIED`. | Test confirms absent roots can be created (**FAIL**) and checks the full root set without enumerating 40 `.venv` files. | **NOT_VERIFIED** in Guest; writable `tmp` is an unresolved S1 conflict. |
| M2 canary read | Windows child uses `GetFileAttributesW` and `CreateFileW` to distinguish WinError 5, WinError 32, directory, missing file, and other errors. WinError 5 remains `NOT_VERIFIED` without a separate-principal witness. | A real fictional locked file returned sharing violation; real directory and missing-file tests plus simulated WinError 5 classification passed. | **NOT_VERIFIED**: no guest canary existence/ACL witness. |
| M3 Git metadata | Bounded direct write probes cover `.git` root, index, config, HEAD, hooks, refs, objects, and nested entries in a disposable repo. `index.lock`, missing paths, links, scan limits, and Git command failure return `NOT_VERIFIED`. Both index flags are tested only on fake `code.py`. | Disposable Git repos with each flag returned **FAIL**; generic Git failure returned `NOT_VERIFIED`; mocked traversal visited required metadata. | **NOT_VERIFIED** under final guest token; object/refs ACL evidence pending. |
| M4 matrix | `docs/AGENT_LOOP_ISOLATION.md` separates syntax classification, real access, mocked outcomes, skipped tests, and Guest evidence. | Review the matrix against tests and reports. | All unrun Guest access checks are **NOT_VERIFIED**. |

Additional changes: child `PATH` contains only system locations; agent-writable
`tools` and venv paths were removed. The local child test asserts the **exact**
environment key allowlist. Mocked subprocess tests cover inherited and denied
handle outcomes. A host-use guard requires the guest path and an explicit
acknowledgement before the CLI runs probes; it is clearly not VM attestation.
The CLI never exits successfully in Phase 2A, even if every probe reports
`PASS`, and it never changes `LIVE_ISOLATION_VERIFIED`.

Git can load local config, includes, filters, and hooks despite a sanitized
global environment. The code checks the fake `.git` for write access, but
effective Git behavior and trusted metadata still need Guest review.

## Test record and limits

- **N1 Windows write classification:** a failed Python `os.open` now triggers
  a fresh Windows `CreateFileW`/`GetLastError` check for file creation or
  opening; `CreateDirectoryW` does the same for directory creation. The result
  distinguishes sharing violation, missing path, readonly attribute, WinError
  5, and unknown system failure. WinError 5 returns `NOT_VERIFIED`, never ACL
  `PASS` without a separate-principal witness. A successful retry or original
  create/open is `FAIL`. Real Host tests used only disposable fictional files;
  readonly, missing, locked, and writable cases passed. Final Guest ACL proof
  remains `NOT_VERIFIED`.
- **N2 Git results:** `git_write` now keeps separate `permissions` and `flags`
  records and combines them fail closed. Read-only `ls-files -v` checks for
  existing suspicious flags even when permission probes found a different
  failure. It skips flag mutation after a permission `FAIL`; independent
  mutating flag tests run only in disposable temporary Git repos. Tests found
  both flag kinds and preserved both result records. Final Guest Git ACL
  protection remains `NOT_VERIFIED`.
- **Complements:** Windows runtime names use case-insensitive matching. A real
  local subprocess proved positive and negative handle inheritance controls;
  the actual launcher still uses `close_fds=True`. The evidence matrix now
  distinguishes UNIT/mock, real Host, and unverified Guest. Child environment
  filtering cannot protect authentication files in an accessible user profile.
  Bare CLI defaults and inherited-path Git lookup are not pinned to trusted
  absolute immutable executables, so live execution remains blocked.
- **Expected pre-fix failures:** three safe regression tests failed on the old
  string-only result and agent-writable `PATH` (missing runtime root, sharing
  violation, PATH). The tests pass after the changes.
- **PASS:** focused isolation and live adapter pytest **68 passed, 2 skipped**;
  full pytest **408 passed,
  2 skipped**. Both used the project `.venv` Python and a new short
  `tmp/p-<UUID8>` basetemp, with plugin autoload/cache disabled. Mock CLI
  `python -m agent_loop --task example.md --scenario success` returned success.
- **SKIPPED:** two host symlink-creation tests because this host test context
  did not permit creating links. No junction access test ran.
- **FAIL (expected security findings in fake workspaces):** outside canary was
  readable; runtime roots and `.git` were writable; index flags could be set
  in disposable host repos. These are detection checks, not guest proofs.
- **NOT_VERIFIED:** guest VM, Windows ACLs under the final agent token, canary
  existence from a separate principal, UNC and second-drive access, guest
  symlink/junction behavior, effective CLI permissions, trusted Git config,
  and handle inheritance under the final launcher.

Test command pattern:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$b=Join-Path (Get-Location) ('tmp/p-'+[guid]::NewGuid().ToString('N').Substring(0,8))
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=line --basetemp $b
```

For the focused run, add `tests/test_agent_loop_isolation.py
tests/test_agent_loop_live.py` and use `--tb=short`.

## Decision for next stage

The preflight is suitable for a **diagnostic trial in a separately prepared,
disposable VM** after manual verification of the fictional bootstrap and
guest-only paths. It does **not** verify isolation or authorize a live agent
cycle. Phase 2B must resolve S1's writable runtime layout, establish trusted
canary witnesses and exact-token ACL evidence, verify S2 and Git config
behavior, and review host sharing and network controls. Keep the live gate
closed and request a separate independent review before any gate change.
