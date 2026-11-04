# Product Runner: Docker worker review handoff

Task: resolve the independent Claude BLOCKED verdict about generated Python
sharing controller credentials. Keep the existing `run_loop`, Codex and Claude
in the controller. Replace only ProductTests execution. No orchestrator, cache
implementation, model invocation, commit, push, or Live isolation change.

Starting branch/commit: main / e0319756e20d7b1d16a35b3d1b28cec0db328336.
Preserved local Linux edits: agent_loop/isolation.py,
docs/AGENT_LOOP_ISOLATION.md, tests/test_agent_loop_isolation.py. Existing
untracked product adapter, contract, acceptance tests and tests were preserved
and updated only within this task. Rules, task template, Claude workflow, and
testing/secrets/filesystem guidance were read. Recommended setting: Codex high
for the security boundary; Claude Sonnet for the next independent review.

## Execution contract

`python -m agent_loop.product --run-models` remains the explicitly opted-in
command. It has NOT been run with models. Existing CLI/version restrictions
remain. Models return text to the controller; only generated Python pytest
execution moves to Docker. Every attempt still requires a human to inspect
JSON-escaped complete source plus SHA-256 and type `RUN <sha256>`. The prompt
and code go to stderr; only the final JSON report goes to stdout.

Docker Engine 29.9.2 inside donation-loop was verified through the local Unix
socket (agent sandbox access required explicit escalation). There is no host
Docker endpoint fallback. The runtime adapter fixes DOCKER_HOST to the local
Unix socket and passes no HOME, CLI authentication, API keys or Docker config.

`agent_loop/worker.py` creates a fresh uniquely named container every attempt.
The sole bind mount is a separately staged allowlist containing exactly:

- src/organization_cache.py (human-approved candidate bytes)
- tests/test_organization_cache.py (fixed human acceptance tests)

No repo root, other project directory, controller HOME, model login, or Docker
socket is mounted. The inputs and root filesystem are read-only. Worker user
is 65534:65534; all capabilities are dropped, no-new-privileges is enabled,
network and IPC are none. Memory/swap are limited to 256 MiB, CPU to one core,
processes to 64 and open files to 128. The writable /tmp is a 32 MiB tmpfs,
noexec/nosuid/nodev. Python output files also have a 256 KiB file size limit.
Docker logging is disabled. No privileged flags or extra devices are used.

The controller removes the container forcibly, including anonymous volumes,
in a finally block on success, exceptions and timeout, and confirms the unique
name is absent. Docker removal kills processes in that container, including
children. A daemon outage or externally killed controller can prevent finally
from completing: cleanup verification then cannot be claimed. Containers have
unique product-worker-* names for manual recovery after controller crash.

## Trusted preparation and result validation

Build separately, BEFORE running models, using only the tiny dedicated context:

```sh
docker build --pull=false --tag donation-loop-pytest:review --iidfile /tmp/donation-loop-worker-image.id agent_loop/worker_image
```

The Dockerfile pins the official Python base digest and all pytest dependency
versions from HTTPS PyPI; pip is removed after preparation. Its .dockerignore
allows only Dockerfile and run.py. No candidate enters the build. The runtime
uses the immutable image digest in `worker.IMAGE`, with --pull=never, and never
builds, installs dependencies, or enables network. On a new machine, build in
this trusted preparation stage and independently review/update IMAGE to the
resulting local digest before use. The current verified digest is recorded in
worker.py. This image is local Docker state, not a committed binary artifact.
An attempted rebuild with --network=none missed the build cache and failed
DNS; rebuilding with the original preparation configuration reused the trusted
dependency layer. No worker network setting was changed.

The trusted image wrapper captures pytest diagnostics, limits report size and
returns JSON. It strips verbose XML text to keep 85 failures within the existing
controller's 64 KiB subprocess output limit; assertion/import diagnostics remain
in bounded output. It hashes both input files before and after pytest. The
controller verifies both hashes against approved bytes, report shape, process
state/exit, an empty Docker filesystem diff, XML counts and case consistency,
no duplicates, no skipped cases, and the exact 85 acceptance names in the fixed
acceptance_ids.json. The manifest was obtained by trusted collect-only execution
of the fixed tests, without importing any candidate. Partial or unexpected
successful reports cannot pass. Early collection/import failures return
focused traceback diagnostics to Codex through the existing feedback loop.
Source/test identity is checked again before/after Claude review.

JUnit and worker-reported hashes are NOT security attestations: candidate Python
shares the pytest interpreter and can manipulate pytest, output or reports.
The read-only mount, immutable image and Docker OS boundary protect controller
credentials; XML validation detects malformed/incomplete reports, not arbitrary
malicious in-process forgery. Human source approval and independent review remain
required. Docker shares a kernel; this is not a VM boundary. No suggestion to
accept credential exposure remains. LIVE_ISOLATION_VERIFIED is unchanged.

## Review version and validation

Review complete uncommitted contents of agent_loop/product.py,
agent_loop/worker.py, agent_loop/acceptance_ids.json,
agent_loop/worker_image/{Dockerfile,.dockerignore,run.py},
tests/test_agent_loop_product.py, tests/test_agent_loop_worker.py and this file.
Also read the unchanged untracked agent_loop/tasks/organization_cache.md and
tests/test_organization_cache.py as the fixed contract. Exclude the preserved
Linux diff from this change. No new independent review has run yet.

Synthetic Docker checks use developer-authored source only: controller-only
sentinel unreadable; no socket or pip; UID 65534; loopback only; external connect
blocked; input/root/image writes fail; timeout with a sleeping subprocess cleaned;
early import error preserved; 85 synthetic passes transported; actual acceptance
tests against `pass` return 85 errors with useful diagnostics. Integration tests
are opt-in so ordinary tests do not silently require Docker or elevated access.

Exact final validation commands and counts are recorded below.

Next step: independent Claude review of this defined local version, especially
the image trust/digest, cleanup and report trust limits. The runner is ready for
that review. A first model cycle remains pending that review and explicit user
authorization; no claim that Organization Cache is implemented or accepted.
Commit/push/PR/merge/deploy: not performed.

Final validation (Linux, Python 3.14; models never started):

- **Passed:** `DONATION_WORKER_TESTS=1 PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_agent_loop_product.py tests/test_agent_loop_worker.py -q -p no:cacheprovider --basetemp /tmp/product-worker-docker-02`: **37 passed**, including all five real Docker checks, using the final image.
- **Failed (expected absent product implementation):** `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider --tb=line --basetemp /tmp/product-worker-full-final`: **820 passed, 12 skipped, 85 errors**, one existing fitz warning. Every error is ModuleNotFoundError for src.organization_cache. Full log: /tmp/product-worker-full-final.log. No implementation was added and these tests were not skipped to obtain a full-suite pass.
- **Passed:** regression suite excluding the explicitly unimplemented acceptance module: `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider --ignore=tests/test_organization_cache.py --basetemp /tmp/product-worker-regression-final`: **820 passed, 12 skipped**, one existing fitz warning. Five skips are opt-in Docker tests (executed separately above); seven are existing skips.
- **Passed:** `python -m agent_loop.product --help`; new/updated review files checked for trailing whitespace. Existing Linux diff was not normalized.
- **Not run:** authenticated model calls, independent re-review, first real task cycle.

Review source SHA-256 manifest (documentation is this handoff; all listed files
are untracked and require their complete contents, not just git diff):

```text
fc036af3d827cc3c4f4d5b1201269946db36d9d269fbb0deffe92bbbbb70fcbc  agent_loop/product.py
46e2a843cf6c387d2d41b5b21b246ac475ace2e241fb5871b65292badd326c5a  agent_loop/worker.py
ecd82f5016ec4b60a80c4247a01bc14b325e38d30ae0419a39d03939c82d5e7b  agent_loop/acceptance_ids.json
937feaea2f93c7b99fa0fcf2c33a77a9efd5bf9799d036ed45c9ebee58816c80  agent_loop/worker_image/Dockerfile
30c2f47af61046daa8579918b80fecd8683777650b62c0a992c63ac2ec6078e6  agent_loop/worker_image/.dockerignore
4ab9a544ab857d65eda27b17ed564c32bf9f0ebff1c5bfaf8d74774a543d969e  agent_loop/worker_image/run.py
64d871db10abd9b1ef1a936d30a3d03857ecb1a5c8dc70638379b8f9d4f625d6  tests/test_agent_loop_product.py
795cb210128f4cb323f86db82345dcfc34ca3ca7fb5b416ec897ef13c4e3c746  tests/test_agent_loop_product_resume.py
349ecccafe1892c07ceed597ec7c1648fe40308a35fd32db863cd765651b2446  tests/test_agent_loop_worker.py
2eee4b737c6d8fcc6ad6bcc63466db342073468bac3e464aea3a182350baa5cd  agent_loop/tasks/organization_cache.md
7116a341d39f35ca47966977f72cc325b9fb46d382aa7a64a395fa8caedaa14e  tests/test_organization_cache.py
```

## Minor-1 follow-up (2026-11-04)

The task owner reports Claude's independent re-review verdict: READY FOR
SUPERVISED FIRST RUN, no Critical or Major findings. This supersedes the earlier
pending-review status above. Minor-1 is fixed: obsolete preparation/handoff and
unresolved-isolation claims were removed from the model task. All text before
its Acceptance and review section (the complete business/API/persistence
specification) is byte-for-byte unchanged. Current acceptance text retains
fixed tests, exact source approval and safety requirements. The task hash in
the review manifest above is updated.

Only agent_loop/tasks/organization_cache.md and this handoff changed in this
follow-up. Runner, Worker, acceptance tests/IDs, core, adapters and existing
Linux changes were preserved. No product implementation or model cycle ran;
no commit or push occurred. Recommended implementation setting remains Codex
medium for the supervised product task.

Validation:

- **Passed:** UTF-8/task formatting, balanced code fences, final newline, no
  trailing whitespace, unchanged specification prefix and removal of obsolete
  instructions; task SHA-256 matches the handoff manifest.
- **Passed:** task 6,382 bytes and fixed acceptance file 15,686 bytes, each below
  the 16,384-byte input limit. Actual adapter prompt construction with mocked
  commands and 24 KiB ordinary source: Codex retry 48,322 bytes (17,014-byte
  failure feedback), Claude review 47,693 bytes (normal passing-test output),
  each below 61,440 bytes. No real CLI or model was invoked.
- **Passed:** oversized synthetic review combining 24 KiB source with 16 KiB
  diagnostic output is rejected by the unchanged prompt guard. Such unusually
  large combinations remain an existing capacity limit, not an isolation bypass.
- **Passed:** trusted collect-only check: exactly 85 acceptance IDs, matching
  acceptance_ids.json; no candidate imported.
- **Passed:** `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python
  -m pytest tests/test_agent_loop_product.py tests/test_agent_loop_worker.py -q
  -p no:cacheprovider --basetemp /tmp/minor1-tests-01`: **32 passed, 5 skipped**.
  Skips are explicit Docker integration opt-ins, previously verified above;
  Docker runtime code was unchanged and those checks were not rerun here.
- **Not run:** full suite (documentation-only follow-up), model cycle.

Minor-1 is resolved. The supervised first run can proceed when explicitly
started by the task owner, retaining per-candidate execution approval.

## Focused Claude diagnostics and resume follow-up (2026-11-04)

Goal: finish review of the already generated/tested candidate without replacing
it unnecessarily. Scope is product.py, a new mocked test file and this handoff.
Starting branch/base remain main / e0319756e20d7b1d16a35b3d1b28cec0db328336.
Preserve all starting local changes, including the existing CLI version contract
2.1.318 (Claude Code). Rules, task template, Claude workflow and testing/secrets
guidance were read. Recommended setting: Codex high for focused implementation;
Claude Sonnet for independent review of this local diff.

No core, CLI adapter, Docker Worker/image, acceptance tests/IDs, task contract,
LIVE_ISOLATION_VERIFIED or product implementation changed. The existing
run_loop still performs orchestration. New ProductCandidate only supplies the
saved source on the first round and emits round progress; default resume has
one round, so no Codex lookup, version call or generation occurs. Explicit
--allow-repair permits existing Codex repair behavior in later rounds and seeds
previous_code with the saved candidate. Fresh human approval and a fresh
Docker acceptance run remain mandatory before Claude. No model was invoked
and no saved source was executed during this implementation.

### Safe failures and progress

ProductClaude now reports a fixed stage/category in ProductReport.diagnostics
and stderr rather than relying on the core's exception type alone. Categories:
cli_failure, invalid_json, output_size_overflow, cli_reported_error,
missing_structured_output, invalid_review_contract, code_identity_mismatch,
prompt_size_overflow, review_size_overflow. No exception text, raw Claude
envelope, prompt or environment values are emitted or persisted. Duplicate
JSON keys are rejected. Code identity is checked before and after the CLI call.

The previous 16 KiB whole-envelope check was stricter than _run's existing
64 KiB output limit. CLI envelopes can contain substantial extra result text
while structured_output is small. Envelopes now use that existing 64 KiB cap;
structured review JSON is separately capped at 16 KiB and must pass the unchanged
parse_review contract. Oversized output still fails closed; no subprocess or
prompt safety limit was increased. Mocked 20 KiB envelopes prove legitimate
extra metadata is accepted. The original ValueError cannot be diagnosed
retrospectively from its type alone: the old report contains no safe stage
metadata or preserved Claude response. Envelope size is a possible cause,
not an established diagnosis of that particular call.

Progress is flushed to stderr: run start/finish, current round, Codex start/end,
approval request/accept/reject, Docker start/end with validated pass count,
Claude start/end/failure category and final stop reason. Stdout remains one
final JSON document. The existing mandatory approval display still shows the
JSON-escaped candidate source and hash to the approving human; progress adds no
source, test diagnostics or model response text.

### Resume and original-run provenance

New reports record input SHA-256 values under inputs, keyed by
agent_loop/tasks/organization_cache.md and tests/test_organization_cache.py.
Resume checks bounded report structure, duplicate keys, candidate syntax/size
and SHA-256, previously passed acceptance status, and exact current input hashes
before starting any subprocess or requesting approval. Reports and manifests
must be regular non-symlink files and are read with bounded retention. The
selected original report is never modified. Results should be redirected to a
NEW file to preserve it.

The existing /tmp/organization-cache-report.json is 10,428 bytes; its candidate
is 9,438 bytes and its recorded SHA-256 matches. It records passed acceptance
and tool_error / ValueError, but has NO input hashes. Current resume therefore
fails closed with original_input_hashes_missing_or_changed. No retrospective
input hashes or provenance manifest were invented. Original-run evidence is
still needed; the user was asked for that evidence.

For that legacy report, explicitly supply a trusted provenance JSON file with
exactly this shape (placeholders must come from original-run evidence):

```json
{
  "report_sha256": "<SHA-256 of the original report bytes>",
  "inputs": {
    "agent_loop/tasks/organization_cache.md": "<original-run task SHA-256>",
    "tests/test_organization_cache.py": "<original-run acceptance SHA-256>"
  }
}
```

The manifest is an operator-supplied trust input, not a signed attestation. It
must be established from trusted original-run records, not a fresh hash of
current task/tests presented as historical evidence. Its report hash binds that
provenance to the selected report. Conflicting embedded hashes are rejected.
Do not create it by assumption. Until that evidence is supplied, this particular
legacy candidate cannot safely resume under the requested provenance rule.

Once the original inputs are established and that manifest is supplied:

```sh
python -m agent_loop.product --run-models --resume-from-report /tmp/organization-cache-report.json --resume-input-manifest /tmp/organization-cache-original-inputs.json > /tmp/organization-cache-review-resumed.json
```

This command was NOT run. It reuses the candidate, requests fresh human
execution approval, reruns all 85 tests in the existing Docker Worker, then
requests Claude review. A failure stops without generating a replacement.
Future reports with embedded inputs need only --resume-from-report. Add
--allow-repair only if Codex repair rounds are explicitly desired.

### Validation and review version

- **Passed:** focused core/product/resume/worker tests, **93 passed, 5 skipped**:
  `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
  tests/test_agent_loop.py tests/test_agent_loop_product.py
  tests/test_agent_loop_product_resume.py tests/test_agent_loop_worker.py -q
  -p no:cacheprovider --basetemp /tmp/product-resume-focused-final-03`.
  All model/CLI calls in new tests are mocked. Five skips are the explicit
  Docker integration opt-ins. Docker isolation code/image were unchanged;
  their previously successful real integration checks were not rerun here.
- **Failed (absent checkout product implementation):** full suite,
  **856 passed, 12 skipped, 85 errors**, one existing fitz warning:
  `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
  -q -p no:cacheprovider --tb=line --basetemp /tmp/product-resume-full-final-02`.
  Log: /tmp/product-resume-full-final-02.log. All 85 errors are the existing
  ModuleNotFoundError for src.organization_cache; the saved candidate was not
  installed into the checkout to make this suite green.
- **Passed:** product --help, actual legacy report refusal with no subprocess,
  report hash consistency, changed-file scope and whitespace checks.
- **Not run:** real models, saved-candidate execution, fresh Docker regression,
  independent review of this follow-up. No commit/push/PR/merge/deploy.

Review exactly the changes to agent_loop/product.py,
tests/test_agent_loop_product_resume.py and this handoff relative to the
session-start local version (all remain untracked). Source hashes in the
manifest above are updated for the current review files. Other local files
were verified unchanged by byte hashes. Next step: supply original-run input
provenance, independently review the focused change, then explicitly resume.

## New execution from an untrusted saved candidate (2026-11-04)

The accepted historical-resume implementation remains intact. Original input
hashes for the first real run were never recorded, so no historical manifest
was fabricated. A separate --candidate-from-report option now starts a NEW
supervised execution using saved source and CURRENT trusted task/test inputs.
It is mutually exclusive with historical resume and rejects resume provenance
and --allow-repair options. Stdout records run_kind=new_candidate and current
input hashes; stderr explicitly labels the new execution, not historical resume.

The shared bounded report reader and source extractor validate report/round
structure, duplicate keys, source SHA-256, the 24 KiB source limit and Python
syntax. No historical test/review claim or input hash establishes trust in this
mode. The existing candidate is only parsed, never imported in the controller.
Fresh full-SHA human approval, all 85 acceptance passes in a fresh Docker Worker
and the existing strict Claude review/diagnostics path are required again.

This minimal mode uses one existing run_loop round and makes NO Codex calls,
including version calls. Claude-requested fixes are retained in the report for
an explicitly authorized follow-up. The core's existing repair path also reacts
to test failures; this new mode therefore does not enable automatic repairs or
risk generating a replacement before Claude has requested one. No core, Worker,
image, CLI adapter, task, acceptance tests/IDs or Live isolation flag changed.
The original report and candidate are never rewritten by the Runner.

Command to perform the fresh candidate-based execution (NOT run here):

```sh
python -m agent_loop.product --run-models --candidate-from-report /tmp/organization-cache-report.json > /tmp/organization-cache-candidate-run.json
```

Use a new output filename; do not redirect over the source report. No historical
input manifest is needed or claimed. All existing stderr progress and clean
stdout JSON behavior apply. A valid Claude review requesting changes ends this
single round with max_rounds and preserves the findings without calling Codex.

Session scope/review: only agent_loop/product.py,
tests/test_agent_loop_product_resume.py and this handoff changed relative to
the accepted session-start local contents. Review their complete untracked
contents using the updated SHA-256 manifest above. Starting branch/base: main /
e0319756e20d7b1d16a35b3d1b28cec0db328336. Project rules, task template,
Claude workflow and testing/secrets guidance were read. Suggested setting:
Codex medium for this focused change; Claude Sonnet for independent review.
Existing Linux edits and all other local changes are preserved.

Validation:

- **Passed:** focused core/product/resume/worker suite, **106 passed, 5 skipped**:
  `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
  tests/test_agent_loop.py tests/test_agent_loop_product.py
  tests/test_agent_loop_product_resume.py tests/test_agent_loop_worker.py -q
  -p no:cacheprovider --basetemp /tmp/product-candidate-focused-01`.
  New cases cover current input hashing, historical resume refusal, candidate
  identity, approval/denial, fresh test execution before Claude, invalid reports,
  option conflicts, failed-test/review stops without Codex, progress and CLI JSON.
  Models and Docker calls are mocked; five opt-in Docker checks were not rerun
  because the isolation implementation and image are unchanged.
- **Passed:** --help; read-only validation of the actual 9,438-byte saved
  candidate without execution; historical resume still rejects missing hashes;
  original report SHA-256 unchanged; scope, review hashes and whitespace checks.
- **Not run:** actual candidate execution, real models, independent review,
  commit/push/PR/merge/deploy. No Organization Cache implementation was created.
- **Failed (existing absent checkout implementation):** full suite,
  **869 passed, 12 skipped, 85 errors**, one existing fitz warning:
  `PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
  -q -p no:cacheprovider --tb=line --basetemp /tmp/product-candidate-full-final`.
  All 85 errors are ModuleNotFoundError for src.organization_cache. Full log:
  /tmp/product-candidate-full-final.log. The saved candidate was not installed
  into the checkout or executed to make this run green.

The new candidate-based execution is ready to be explicitly started using the
command above. Historical resume remains blocked without original provenance.


## Approved product integration (2026-11-04)

Goal: integrate the approved Organization Cache increment, validate the existing
work, and deliver a feature branch. No Agent Loop redesign, merge or deployment.
User explicitly authorized commits and push in this session. Starting base:
main / e0319756e20d7b1d16a35b3d1b28cec0db328336. All existing local Linux
compatibility fixes and infrastructure files are preserved.

Verified the supplied local candidate report: stop_reason=success,
tests.status=passed, review.status=approved. The UTF-8 implementation SHA-256 is
29a184709f7997de2755c5e3d84ed292ec4c0373d6a1b13a98b0d92a994ecd56.
The already present untracked src/organization_cache.py was byte-identical;
no overwrite or implementation modification was needed. Both task and fixed
acceptance-test input hashes matched the report. Claude approval applies to
that exact implementation. Three nonblocking review observations remain:
cache publication uses mode 0600; special files such as FIFOs are not rejected;
and temporary-file cleanup errors may mask an earlier OS error. No changes
were made for those observations. The local report is excluded from Git.

Offline demonstration used TemporaryDirectory, a fixed aware UTC clock and
only the documented constructor/put/save/lookup API. Fictional ID 000000001
had approval for calendar 2020 and explicit denial for calendar 2021. After
save and construction of a new cache instance, actual output was:

```text
Fictional Example Organization (000000001); saved and reloaded: True
2020-06-15: {'exists': True, 'section46_status': 'approved', 'stale': False}
2021-06-15: {'exists': True, 'section46_status': 'denied', 'stale': False}
```

Validation used repository .venv/bin/python (Python 3.14.10):

- **Passed:** `.venv/bin/python -m pytest tests/test_organization_cache.py -q -p no:cacheprovider --basetemp /tmp/org-integration-acceptance-1104`: 85 passed, 0 failed, 0 skipped.
- **Passed:** `.venv/bin/python -m pytest -q -p no:cacheprovider --tb=line --basetemp /tmp/org-integration-full-1104`: 954 passed, 0 failed, 12 skipped, one existing fitz deprecation warning.
- **Environment-blocked first attempt:** `DONATION_WORKER_TESTS=1 .venv/bin/python -m pytest tests/test_agent_loop_worker.py -q -p no:cacheprovider --tb=short --basetemp /tmp/org-integration-docker-1104`: 9 passed, 4 failed, 0 skipped. Docker socket permission denied; the timeout case's pass does not establish Docker execution in that attempt.
- **Passed after approved sandbox escalation:** same Docker command with fresh `--basetemp /tmp/org-integration-docker-escalated-1104`: 13 passed, 0 failed, 0 skipped, including all five real Docker checks.
- **Passed:** `git -c core.whitespace=cr-at-eol diff --check`. Plain diff --check flags the preserved pre-existing mixed CRLF endings as trailing whitespace; bytes were preserved.

No Gmail, donor data, registry requests or model calls were used. The synthetic
Docker isolation test intentionally attempts an external connection inside a
network-disabled container and verifies failure; no registry/network data is
fetched. Push is the separately authorized external GitHub operation.

Delivery grouping: preserved Linux compatibility fixes; existing supervised
product runner/Worker infrastructure with task, fixed tests and handoff; exact
approved cache implementation. Review the resulting commits against the base
above; the candidate approval is scoped to its source hash, not a new claim of
Claude approval of every infrastructure file. Recommended next review setting:
Claude Sonnet for repository integration. Merge/deploy remain unauthorized.
