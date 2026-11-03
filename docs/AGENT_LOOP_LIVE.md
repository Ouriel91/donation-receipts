# Real Agent Loop: CLI adapters, phase 1

## Scope and connection to the loop

`agent_loop/live.py` implements the existing `CodexAdapter.run(task, feedback)`,
`TestAdapter.run()`, and `ClaudeAdapter.review(task, implementation, tests)`
protocols. A future isolated harness can pass these objects to `run_loop`.
`python -m agent_loop` remains Mock only. No live command, environment toggle,
or automatic Git action is provided. `LIVE_ISOLATION_VERIFIED` is hard-coded to
`False`; all three adapters reject calls before starting a subprocess. Test
code temporarily patches this constant while mocking every subprocess.

The adapters expect a **separate fictional workspace** with a
`.fictional-agent-workspace` marker and its own Python environment. The marker
is an accidental-use check, not a security boundary. No real receipt, account,
report, credential, personal data, or copy of this repository's ignored files
belongs in that workspace.

Codex sends a JSON payload containing the task and previous feedback via stdin
to `codex exec`. It requests `workspace-write`, no approval escalation, an
ephemeral session, and a schema-constrained final answer. The final JSON file
must contain exactly one nonempty `summary` (at most 2 KiB); malformed, absent,
oversized, or unsuccessful output fails the adapter. This summary is the
existing loop's `implementation` field; the reviewer receives the actual
changes separately through the review package.

pytest runs the configured argument tuple through the Python executable inside
the fictional workspace, with a fresh UUID parent for every `--basetemp` and
`--junitxml` result. It validates JUnit counts and their agreement with the
exit code. Only counts enter `TestResult.detail`; no test names, failures, raw
stdout, or stderr enter loop reports. Exit code 1 with test failures returns
`failed`; CLI/setup errors and invalid or missing XML return `not_run`.

Before **every** Claude review, the adapter collects a fresh `ReviewPackage`
from the marked fictional workspace against a caller-supplied full base commit
ID. It includes the current Git diff and UTF-8 contents of new files, including
only nonignored untracked files selected by Git's standard ignore rules.
An ignored source file requires an explicit entry in `LiveClaude.explicit_ignored_files`;
each entry must be confirmed as ignored by Git and pass the same path, symlink,
size, and content checks. A separate directory-level Git scan checks each
reported ignored path or directory against a fixed runtime allowlist and the
explicit file list.
An unapproved ignored path, including a new `.gitignore` that hides itself,
stops the review with `ReviewIntegrityError` without reporting its name or
contents. Two consecutive snapshots must match; Git errors,
unsafe paths or content, oversized inputs, or a changing workspace abort the
review before Claude starts. Requirements, task,
implementation summary, and test counts are provided with the snapshot. This
filter is deliberately narrow and cannot prove that arbitrary text is free of
personal information. Only a fictional workspace is acceptable.

**N1 snapshot collection:** Git now excludes ignored directories such as
`.venv` while listing untracked files. A local Git test covers an ignored tree
whose names alone would exceed the runner's 64 KiB stdout cap. Excessive
nonignored output still fails closed. The ignored-path integrity scan collapses
ignored directories such as `.venv`, `tmp`, and `__pycache__` into directory
entries rather than listing their files. This change does not open the live gate.

**Remaining live-operation blockers:**

- **S1:** The fixed runtime allowlist excludes its directories from review.
  Source placed inside an approved runtime directory could be hidden. Before
  live operation, enforce and verify that these directories cannot contain
  task source or reviewable changes.
- **S2:** Git index metadata such as `assume-unchanged` or `skip-worktree` can
  hide modifications to tracked files from ordinary diff collection. Before
  live operation, detect or forbid these flags and verify snapshot completeness
  against the working tree.

Claude invokes
`claude -p` with `--restricted`, `--safe-mode`, `--tools Read,Glob,Grep`,
`--permission-mode dontAsk`, `--disallowedTools mcp__*`,
`--strict-mcp-config`, and `--no-session-persistence`. It requests a JSON schema
and validates the `structured_output` envelope with the core review contract.
Claude's tool restrictions need the documented CLI version and **do not replace
OS isolation**. Managed policy can still apply; verify the effective tool
surface in the guest. No shell tool is in its allowlist.

The process runner uses argument arrays (no shell), timeouts, an in-memory
64 KiB stdout cap, and discards stderr while draining it. It never includes CLI
exception messages in reports. Prompts are capped at 64 KiB and the Claude
review at 8 KiB. A fictional-only workspace remains essential: content supplied
by a model or caller cannot be reliably scrubbed after exposure.
On Windows, the runner creates each child suspended, attaches it to a Job
Object with kill-on-close, then resumes it. Timeout and cleanup waits and
stream-reader joins are bounded; failure to establish or clean up the job fails
the adapter. A local Python parent/child probe verifies that an ordinary child
holding stdout/stderr open is terminated. This does not prove control of every
process a model might start through Windows services or other out-of-job paths;
the isolated guest and actual CLI behavior still require separate verification.

## Windows isolation gate

The current `False` gate must remain closed until a separate verification task
records all evidence below. Merely copying code to another directory does not
change the process access token or prevent reads from the host filesystem.

1. Build a fresh Windows Sandbox or dedicated VM containing a tiny fictional
   project and its own toolchain. Do not map the real repository, home,
   OneDrive, drives, clipboard, printers, or credentials into it. For offline
   isolation checks, disable networking, clipboard, printer, audio, and video
   redirection. Windows Sandbox supports these controls in `.wsb` files; its
   defaults allow networking and clipboard redirection.
2. In the guest, run a local probe under the **same user token and process tree**
   that would launch the CLIs. Place non-sensitive canary files outside the
   allowed guest project and on the host. Test direct paths, `..`, symlinks or
   junctions, alternate drives, UNC paths, inherited handles, environment
   variables, and child processes. Record each attempted read and the denied
   result. Inspect the effective guest configuration, mounted folders, token,
   and network routes. A prompt saying “read only” is not evidence of denial.
3. For a first online model cycle, use a dedicated disposable VM with no host
   sharing and tightly controlled egress to the required model endpoints.
   Authentication must be provided outside the fictional project with the
   narrowest practical lifetime and scope; no credential may be copied into
   the project, prompts, reports, or review handoff. Verify the same denial
   probes again with network enabled and the actual CLI versions and flags.
4. Independently review the evidence and only then implement an explicit,
   versioned gate-opening change. This phase has no way to open the gate through
   command-line arguments or configuration. Run one fictional task, inspect
   process actions and file access, test and review outputs, then decide
   whether to permit additional cycles.

Windows Sandbox isolation controls: [Microsoft configuration reference](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file).
Claude CLI restrictions and structured output: [CLI reference](https://code.claude.com/docs/en/cli-reference),
[headless output](https://code.claude.com/docs/en/headless).
Codex CLI schema and sandbox flags: [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli).
