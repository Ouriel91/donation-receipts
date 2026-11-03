# Real Agent Loop phase 2A: offline isolation preflight

## Decision and gate

The host reports **Windows 11 Home, build 26200, 64 bit**. Windows Sandbox is
not supported on Home. WMI reports a hypervisor present; CPU virtualization
fields are `False` under that hypervisor and do not establish whether a new VM
can be created. Reading the optional Windows Sandbox feature state requires
elevation and was not completed. No feature, hypervisor, registry, permission,
or installation setting was changed. Use a **dedicated disposable VM** only
after its availability and configuration have been reviewed separately.

`LIVE_ISOLATION_VERIFIED` remains `False`. `python -m agent_loop` is still the
Mock entry point. No preflight result changes the gate, starts Codex or Claude,
or authorizes a change. The loop only returns a report for a person to inspect;
it never commits or approves a merge.

## Guest architecture to build and verify

1. Create a new VM with a local, non-admin agent account. Disable the virtual
   network adapter for offline checks. Disable host folders, drives, OneDrive,
   clipboard, printers, camera, microphone, and unneeded devices. Do not attach
   the real repository, user profile, donor data, credentials, Gmail accounts,
   or reports. Check the VM's effective device and sharing settings, not only
   its configuration file.
2. Build a bootstrap image from an explicit allowlist of **fictional files**:
   `docs/fixtures/agent_loop_vm/` and the generic `agent_loop` preflight code.
   Inspect the image and record hashes before attaching it as read-only
   offline media. Copy the fictional project into `C:\AgentLab\work` and the
   preflight code into a separate, agent-read-only `C:\AgentLab\tooling`.
   Detach the media and verify no host path remains mounted. Obtain a clean
   guest Python and Git toolchain through a separately approved process; do
   not copy the host's user config, CLI auth state, or `.venv` wholesale.
3. Prepare a separate guest-only protected canary area owned by another local
   principal. Give the agent token write access to the fictional worktree but
   deny it read access to canaries and write access to `.git`. Prepare distinct
   canaries for absolute, `..`, another guest drive, UNC, symlink, and junction
   paths. A missing target is **inconclusive**, not a denied read. The UNC case
   needs a guest-only endpoint and an independently verified offline network
   boundary; a network error alone does not prove filesystem ACL denial.
4. A **separate trusted principal** must first open and validate every fake
   canary, record its existence, regular-file type, size or hash, and effective
   ACL, and seal the evidence outside the agent's reach. An Access Denied result
   from the agent token alone cannot prove that the target exists. Run the
   preflight under the **same token and launcher** intended for CLI children.
   Save its JSON result beside separately reviewed evidence of the VM
   configuration, effective ACLs, token, mounted devices, network routes, and
   canary setup. Repeat after any VM or toolchain change. The probe emits only
   status codes; it never reads or reports canary contents.

The illustrative `offline-example.wsb` disables networking, clipboard,
printers, audio/video input, and vGPU, enables Protected Client, and has no
mapped folders. It is **not runnable on this Home edition** and does not solve
bootstrap by itself. Windows Sandbox also uses an administrative container
account by default, so its guest filesystem ACL behavior would need separate
proof even on a supported edition. The VM design above is the path for this
host.

## Guest preflight

The fixture has only `code.py`, `.gitignore`, a marker, and a canary manifest
template. Copy it into the guest, replace the template paths with actual
guest-only canaries, initialize Git, and commit the fictional base with a fake
identity. Prepare OS ACLs **before** running the probe. The command refuses
ordinary host paths and requires an explicit acknowledgement; this is an
**accidental-use guard**, not VM attestation or a security boundary. From the
agent-read-only guest tooling directory:

```powershell
Set-Location C:\AgentLab\tooling
python -m agent_loop.isolation --workspace C:\AgentLab\work --manifest C:\AgentLab\work\canaries.json --acknowledge-disposable-guest
```

The Phase 2A command **always exits nonzero**: it reports `FAIL` for a proven
violation and `NOT_VERIFIED` when a required witness or proof is absent. A
probe-level `PASS` means only that its particular condition was observed under
the current process token. It does not attest the guest or open the live gate.
The canary read probes run in bounded local Python child processes through
the same sanitized environment and Windows Job Object runner as the adapters.
Windows file opens classify WinError 5 separately from WinError 32, missing
paths, directories, and other errors. Even WinError 5 remains `NOT_VERIFIED`
until separate-principal existence and ACL evidence is independently reviewed.

The handle check uses a fictional inheritable file handle; local unit tests
mock both outcomes and a local child test checks positive and negative
inheritance controls. The Git check tries
unique entries in the **disposable fictional `.git` only**, and tests write
access to existing `index`, `config`, `HEAD`, hooks, refs, and objects with a
bounded scan. It rejects a stale `index.lock`, missing metadata, symlinks,
resource exhaustion, and unexplained Git failures as `NOT_VERIFIED`. It checks
existing index flags with a read-only Git listing even if another permission
probe fails. When permission probes have not returned `FAIL`, it also tries
`assume-unchanged` and `skip-worktree` on fictional `code.py`. A nonzero Git
exit is not treated as
ACL proof. Discard the disposable VM after a failed Git write test; its fake
index may have changed.

The runtime check attempts directory creation and a fictional write in every
fixed ReviewPackage runtime root: `tmp`, `.venv`, `venv`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, and `node_modules`. It also checks root and
nested `__pycache__` candidates while bounding traversal and skipping large
runtime trees. An absent directory that can be created is `FAIL`, never
evidence of denial. Scan limits, links, and unexplained errors are
`NOT_VERIFIED`. Existing runtime trees are deliberately not enumerated; a
root-level write denial cannot prove that every nested child is protected, so
those trees also remain `NOT_VERIFIED` without an ACL inheritance audit.
Current pytest and schema paths use workspace `tmp`, which
must be writable for the current adapters. **S1 therefore remains unresolved**:
Phase 2B must move runtime writes to a separately controlled layout, with a
different token or equivalent OS control, and prove that task source cannot be
hidden there. Reclassifying writable `tmp` cannot close S1.
The snapshot's ignored-directory allowlist and Git index flags (S2) remain
live-operation blockers. Path normalization, a marker file, Git filtering,
and CLI permission flags do not enforce OS isolation.

Git commands can also load repository config, includes, filters, and hooks from
material the agent may influence. The child environment disables system/global
Git config inheritance, but local `.git/config` and repository content still
need a trusted immutable baseline and separate review. Phase 2B must verify
this effective Git surface, including writes to existing objects and refs,
under the final guest token. Use absolute paths to trusted, read-only guest
executables: the sanitized child `PATH` contains only Windows system folders,
so bare `codex` and `claude` defaults are deliberately not ready for live use.

## Host observations, 2026-10-15 to 2026-10-16

- Windows: WMI read returned Windows 11 Home, build 26200, 64 bit, and
  `HypervisorPresent=True`. Windows Sandbox feature query returned an elevation
  requirement; no feature state was asserted. [Microsoft lists Home as
  unsupported](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-overview).
- Codex: `codex-cli 0.155.0-alpha.16`. `codex exec --help` lists the sandbox,
  schema, ephemeral, and output flags. `--ask-for-approval` is a **global**
  flag: `codex exec --ask-for-approval never --help` failed parsing, while
  `codex --ask-for-approval never exec --help` parsed. The adapter now places
  it before `exec`. No model request was made.
- Claude Code: `2.1.280`. `claude --help` lists `-p`, `--restricted`,
  `--safe-mode`, `--tools`, `--disallowedTools`, `--permission-mode dontAsk`,
  `--strict-mcp-config`, `--no-session-persistence`, `--output-format json`,
  and `--json-schema`. Effective managed policy and read restriction in a VM
  have **not** been tested.
- CLI children now receive an exact allowlist: `SystemRoot` and `WINDIR` on
  Windows, system-only `PATH`, workspace `TEMP`/`TMP`, two Python switches,
  and three fixed Git config switches. No agent-writable tools or venv path is
  in `PATH`; trusted tools need explicit absolute paths. The current bare
  `codex` and `claude` adapter defaults do not enforce trusted absolute CLI
  paths and block live use. Git lookup via the inherited launcher `PATH` also
  needs a trusted absolute path and immutable executable in Phase 2B. Filtering
  environment variables does not
  prevent a process from opening authentication files in an accessible user
  profile. The guest must have no host profile mapping and must use a separate
  token and OS-enforced filesystem permissions. `close_fds=True` is explicit.
  Local fictional subprocesses verify the allowlist and both positive and
  negative handle inheritance controls; final guest behavior is unverified.
- Focused pytest after the N1/N2 fixes: **68 passed, 2 skipped** for isolation
  and live adapter tests; full pytest: **408 passed, 2 skipped**, each with a fresh short
  `--basetemp`. The two
  skipped tests attempt to create local symlinks; this host test context does
  not permit it. The full suite result is recorded in the review handoff.
  A direct Mock CLI scenario completed successfully after the fixes. The earlier restricted pytest
  attempt failed during temporary-directory setup with `PermissionError`;
  reported passes are from permitted local reruns.
- **Not run:** a VM, real CLI model cycle, guest ACL/UNC/alternate-drive
  probes, host canary probes, and actual isolation verification. No Gmail or
  donor data was accessed.

## Evidence matrix

`PASS` applies only to the stated check, `FAIL` means a demonstrated security
condition failed, `SKIPPED` means the test could not run, and `NOT_VERIFIED`
means the needed condition or evidence is absent. Host test assertions that
pass do not make host isolation `PASS`. Path classification is listed separately
from actual file access.

| Condition | UNIT/mock evidence | Real Host observation | Guest isolation |
|---|---|---|---|
| Absolute and `..` path forms | PASS: syntax classification | FAIL: fictional outside canary readable | NOT_VERIFIED: no ACL witness |
| Missing file / directory canary | PASS: expected classes | PASS: local child returned `NOT_VERIFIED` for both | NOT_VERIFIED |
| Windows write error classes | PASS: `errno=13` without `winerror` handled; result stays `NOT_VERIFIED` absent ACL witness | PASS: real fictional readonly, missing, sharing-violation, and writable paths classified; writable paths yielded `FAIL` | NOT_VERIFIED: no final token or independent ACL witness |
| Other-drive / UNC | PASS: syntax classification | NOT_VERIFIED: no access attempt | NOT_VERIFIED |
| Symlink / junction | PASS: syntax checks | SKIPPED: two host symlink-creation tests; junction not exercised | NOT_VERIFIED |
| Child environment | PASS: key-set assertion | PASS: exact allowlist observed in local child | NOT_VERIFIED under final guest token |
| Inherited file handle | PASS: mocked outcome classes | PASS: positive and negative local child controls observed | NOT_VERIFIED under final guest launcher |
| `.git` index/config/HEAD/hooks/refs/objects | PASS: mocked traversal coverage | FAIL: disposable host Git repo writable | NOT_VERIFIED: no guest ACL or object-tree audit |
| `assume-unchanged` / `skip-worktree` | PASS: independent permission/flag result retention | PASS: real disposable Git flags detected, including when another permission finding is injected | NOT_VERIFIED under guest ACL/token |
| S1 runtime roots and nested `__pycache__` | PASS: complete root-set and case-insensitive Windows name checks | FAIL: absent roots created in fictional workspace; `tmp` writable | NOT_VERIFIED until Phase 2B layout and OS enforcement |

The Host had no trusted second-principal witness for protected canaries, and
no guest was created. A probe-level WinError 5 alone therefore stays
`NOT_VERIFIED`. No Phase 2A run can promote this matrix to verified isolation.

## Conditions to open the live gate

All must hold, evidenced and independently reviewed, before a separate change
sets `LIVE_ISOLATION_VERIFIED = True`:

1. Phase 2B supplies a separately reviewed evidence workflow; Phase 2A
   intentionally returns nonzero even if its local probes all pass.
2. Every matrix row becomes PASS in the Guest column, including UNC, other
   drive, symlink and junction, with canaries independently proven to exist.
3. S1 and S2 closed: OS-enforced read-only `.git`, and no writable
   unreviewed runtime directory.
4. No host share, clipboard, printer, or credential route; effective VM
   device settings inspected.
5. Installed Codex/Claude effective permissions tested in the guest.
6. Controlled egress and short-lived guest-only auth reviewed for the first
   online cycle.
7. Human review of every change; no auto-approval.

## Steps requiring the user's approval or manual action

- Choose and approve a VM product; enable any Windows feature or hypervisor
  setting (nothing was changed here).
- Create the VM, local non-admin agent account, and a separate canary-owner
  principal; set ACLs.
- Approve the guest Python/Git toolchain source and the bootstrap media hashes.
- Run the guest preflight and keep its JSON and configuration evidence.
- Approve any commit, push, PR, or merge; none was made.

## Review and next gate

Independent review should inspect the exact diff including new files, the
fixture and preflight's fail-closed behavior, the environment allowlist, and
the CLI flag adjustment. Before a separate gate-opening change, prove OS
denials from the final guest token, close S1 and S2, verify that no host share
or credential route exists, and test the installed CLIs' effective permissions
in that guest. The first online cycle needs a new review of controlled egress
and short-lived guest-only authentication; this offline phase supplies none.

Windows Sandbox configuration fields are described in [Microsoft's WSB
reference](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file).
