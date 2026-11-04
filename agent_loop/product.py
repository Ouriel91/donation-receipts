"""Supervised single-file product runner; never an unattended isolation boundary."""
import argparse
import ast
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Callable
import xml.etree.ElementTree as ET
from agent_loop.worker import DockerWorker, EXPECTED, validate_result

from agent_loop.core import Report, TestResult, parse_review, run_loop
from agent_loop.demo import CODEX_SCHEMA, _small
from agent_loop.live import REVIEW_SCHEMA, MAX_OUTPUT, OutputLimitError, _run

TARGET = 'src/organization_cache.py'
REQUIREMENTS = 'agent_loop/tasks/organization_cache.md'
TEST = 'tests/test_organization_cache.py'
VERSIONS = ('codex-cli 0.166.0', '2.1.318 (Claude Code)')
MAX_CODE = 24 * 1024


def progress(message):
    print(message, file=sys.stderr, flush=True)


def quiet(message):
    pass


class ClaudeFailure(ValueError):
    """Only a fixed category, never model output or exception text."""
    def __init__(self, category):
        self.category = category
        super().__init__(category)


class ResumeFailure(ValueError):
    pass


@dataclass
class ProductReport(Report):
    inputs: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    run_kind: str = 'generated'


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def input_hashes(task, tests):
    return {REQUIREMENTS: digest(task), TEST: digest(tests)}


def load_resume(path, inputs, manifest=None):
    """Legacy provenance must be independently supplied, never inferred now."""
    try:
        return _load_resume(path, inputs, manifest)
    except ResumeFailure:
        raise
    except (OSError, ValueError, TypeError, SyntaxError, RecursionError):
        raise ResumeFailure('invalid_resume_report') from None


def bounded_file(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Invalid report file')
    with path.open('rb') as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Oversized report file')
    return raw


def read_candidate_report(path):
    raw = bounded_file(path, 256 * 1024)
    data = strict_json(raw)
    if (not isinstance(data, dict) or not {'stop_reason', 'next_step', 'rounds'} <= set(data)
            or set(data) - {'stop_reason', 'next_step', 'rounds', 'error', 'inputs', 'diagnostics', 'run_kind'}
            or not isinstance(data['next_step'], str) or data.get('stop_reason') not in {
            'tool_error', 'tests_not_run', 'max_rounds', 'human_decision', 'success'}):
        raise ValueError('Invalid resume report')
    return raw, data


def _load_resume(path, inputs, manifest):
    raw, data = read_candidate_report(path)
    recorded = data.get('inputs')
    if manifest is not None:
        proof = strict_json(bounded_file(manifest, 4096))
        if (not isinstance(proof, dict) or set(proof) != {'report_sha256', 'inputs'}
                or proof['report_sha256'] != hashlib.sha256(raw).hexdigest()):
            raise ValueError('Resume provenance does not match report')
        if recorded is not None and recorded != proof['inputs']:
            raise ValueError('Conflicting resume provenance')
        recorded = proof['inputs']
    if recorded != inputs:
        raise ResumeFailure('original_input_hashes_missing_or_changed')
    code = extract_candidate(data)
    tests = data['rounds'][-1].get('tests')
    if (not isinstance(tests, dict) or set(tests) != {'status', 'detail'}
            or tests['status'] != 'passed' or not isinstance(tests['detail'], str)
            or not tests['detail'].startswith('85 acceptance tests passed.\n')):
        raise ValueError('Resume requires previously passed acceptance tests')
    return code


def extract_candidate(data):
    rounds = data.get('rounds')
    if not isinstance(rounds, list) or not 1 <= len(rounds) <= 3:
        raise ValueError('Invalid resume rounds')
    for number, item in enumerate(rounds, 1):
        if (not isinstance(item, dict)
                or set(item) != {'number', 'implementation', 'sha256', 'tests', 'review'}
                or type(item.get('number')) is not int or item['number'] != number):
            raise ValueError('Invalid resume round')
    candidate = rounds[-1]
    code = _small(candidate.get('implementation'), MAX_CODE)
    if digest(code) != candidate.get('sha256'):
        raise ValueError('Resume code hash mismatch')
    ast.parse(code)  # Never execute/import the saved candidate in the controller.
    return code


def load_untrusted_candidate(path):
    """Import source only; historical inputs, tests and reviews confer no trust."""
    try:
        _, data = read_candidate_report(path)
        return extract_candidate(data)
    except (OSError, ValueError, TypeError, SyntaxError, RecursionError):
        raise ValueError('Invalid candidate report') from None


class ProductCandidate:
    """Round progress and optional first candidate; orchestration stays in run_loop."""
    def __init__(self, codex, saved, limit, emit):
        self.codex, self.saved, self.limit, self.emit = codex, saved, limit, emit
        self.round = 0
        self.code = None

    def run(self, task, feedback):
        self.round += 1
        self.emit(f'Round {self.round}/{self.limit}')
        if self.round == 1 and self.saved is not None:
            self.code = self.saved
            self.emit('Saved candidate loaded; generation skipped')
            if self.codex is not None:
                self.codex.previous = self.code
        else:
            self.emit('Codex started')
            self.code = self.codex.run(task, feedback)
            self.emit('Codex completed')
        return self.code


def digest(code: str) -> str:
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def model_env(workspace: Path) -> dict:
    # Existing CLI login is allowed, application secrets and API keys are not.
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'SystemRoot', 'WINDIR',
            'USERPROFILE', 'APPDATA', 'LOCALAPPDATA') if key in os.environ}
    env.update(TMPDIR=str(workspace), TEMP=str(workspace), TMP=str(workspace))
    return env


class ProductCodex:
    def __init__(self, workspace, command, executable, env):
        self.workspace, self.command, self.executable, self.env = workspace, command, executable, env
        self.code = None

    def run(self, task, feedback):
        self.code = None
        schema = self.workspace / 'schema.json'
        schema.write_text(json.dumps(CODEX_SCHEMA), encoding='utf-8')
        args = [self.executable, '--strict-config', '--no-daemon']
        for feature in ('shell_tool', 'unified_exec', 'code_mode_host', 'apps',
                        'browser_use', 'computer_use', 'plugins', 'multi_agent'):
            args += ['--disable', feature]
        args += ['--config', 'web_search="disabled"', '--ask-for-approval', 'never',
                 'exec', '--sandbox', 'read-only', '--ignore-user-config', '--ignore-rules',
                 '--ephemeral', '--skip-git-repo-check', '--output-schema', str(schema), '-']
        prompt = _small(json.dumps({'task': task, 'previous_feedback': feedback,
            'previous_code': getattr(self, 'previous', None),
            'instruction': f'Return only JSON with code: complete Python source for {TARGET}. '
                           'No tools, commands, other files or Markdown.'}), 60 * 1024)
        result = self.command(args, cwd=self.workspace, prompt=prompt, timeout=600, child_env=self.env)
        answer = json.loads(_small(result.stdout, MAX_CODE + 1024))
        if not isinstance(answer, dict) or set(answer) != {'code'}:
            raise ValueError('Expected code only')
        code = _small(answer['code'], MAX_CODE)
        ast.parse(code)  # Syntax validation only; deliberately no exec/import.
        self.code = self.previous = code
        return code


class ProductTests:
    def __init__(self, workspace, codex, command, approve, test_source, emit=quiet):
        self.workspace, self.codex, self.command = workspace, codex, command
        self.approve, self.test_source = approve, test_source
        self.emit = emit
        self.tested = None
        (workspace / 'src').mkdir()
        (workspace / 'tests').mkdir()
        (workspace / TEST).write_bytes(test_source)

    def intact(self, code):
        for name, expected in ((TARGET, code.encode('utf-8')), (TEST, self.test_source)):
            path = self.workspace / name
            if path.is_symlink() or path.parent.is_symlink() or path.read_bytes() != expected:
                raise ValueError('Source or trusted tests changed')

    def run(self):
        self.tested = None
        code = self.codex.code
        if code is None:
            raise ValueError('Missing source')
        # Approval precedes writing/importing the proposed implementation.
        self.emit('Human approval requested')
        if self.approve(code, digest(code)) is not True:
            self.emit('Human approval rejected')
            return TestResult('not_run', 'Human execution approval required')
        self.emit('Human approval accepted')
        target = self.workspace / TARGET
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError('Unsafe target')
        target.write_bytes(code.encode('utf-8'))
        self.intact(code)
        self.emit('Docker acceptance tests started')
        with tempfile.TemporaryDirectory(prefix='product-input-') as name:
            inputs = Path(name)
            (inputs / 'src').mkdir()
            (inputs / 'tests').mkdir()
            (inputs / TARGET).write_bytes(code.encode('utf-8'))
            (inputs / TEST).write_bytes(self.test_source)
            try:
                result = DockerWorker(self.command).run(inputs)
            except Exception:
                self.emit('Docker acceptance tests failed: worker_error')
                raise
        self.intact(code)
        status, detail = validate_result(result, code.encode('utf-8'), self.test_source)
        if status == 'passed':
            self.emit('Docker acceptance tests completed: 85/85 passed')
        elif status == 'failed':
            # validate_result has already checked XML shape, counts and entities.
            passed = sum(case.attrib['name'] in EXPECTED and not any(
                child.tag in ('failure', 'error', 'skipped') for child in case)
                for case in ET.fromstring(result['xml'])[0])
            self.emit(f'Docker acceptance tests completed: failed, {passed}/85 passed')
        else:
            self.emit('Docker acceptance tests completed: not_run; pass count unverified')
        if status == 'passed':
            self.tested = (code, digest(code), detail)
        return TestResult(status, detail)


class ProductClaude:
    def __init__(self, workspace, command, executable, env, tests, emit=quiet):
        self.workspace, self.command, self.executable = workspace, command, executable
        self.env, self.tests = env, tests
        self.emit, self.diagnostics = emit, []

    def review(self, task, implementation, tests):
        self.emit('Claude review started')
        try:
            raw = self._review(task, implementation, tests)
        except ClaudeFailure as exc:
            self.diagnostics.append({'stage': 'claude_review', 'category': exc.category})
            self.emit('Claude review failed: ' + exc.category)
            raise
        self.emit('Claude review completed')
        return raw

    def check_identity(self, implementation):
        try:
            self.tests.intact(implementation)
        except (OSError, ValueError):
            raise ClaudeFailure('code_identity_mismatch') from None

    def _review(self, task, implementation, tests):
        identity = (implementation, digest(implementation), tests.detail)
        if tests.status != 'passed' or self.tests.tested != identity:
            raise ClaudeFailure('code_identity_mismatch')
        self.check_identity(implementation)
        prompt = json.dumps({'instruction': 'Independently review supplied implementation. '
            'Use no tools. Return structured findings.', 'requirements': task,
            'path': TARGET, 'code': implementation, 'sha256': identity[1],
            'trusted_tests': self.tests.test_source.decode('utf-8'),
            'tests': {'status': tests.status, 'detail': tests.detail}})
        try:
            _small(prompt, 60 * 1024)
        except ValueError:
            raise ClaudeFailure('prompt_size_overflow') from None
        try:
            result = self.command([self.executable, '-p', '--restricted', '--safe-mode',
            '--tools', '', '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
            '--disallowedTools', 'mcp__*', '--strict-mcp-config', '--no-session-persistence',
            '--output-format', 'json', '--json-schema', json.dumps(REVIEW_SCHEMA)],
                cwd=self.workspace, prompt=prompt, timeout=600, child_env=self.env)
        except OutputLimitError:
            raise ClaudeFailure('output_size_overflow') from None
        except (OSError, RuntimeError):
            raise ClaudeFailure('cli_failure') from None
        except ValueError:
            raise ClaudeFailure('invalid_json') from None
        self.check_identity(implementation)
        if result.returncode != 0:
            raise ClaudeFailure('cli_failure')
        if not isinstance(result.stdout, str):
            raise ClaudeFailure('invalid_json')
        if len(result.stdout.encode('utf-8')) > MAX_OUTPUT:
            raise ClaudeFailure('output_size_overflow')
        try:
            envelope = strict_json(result.stdout)
        except (ValueError, RecursionError):
            raise ClaudeFailure('invalid_json') from None
        if not isinstance(envelope, dict) or envelope.get('is_error') is not False:
            raise ClaudeFailure('cli_reported_error')
        if 'structured_output' not in envelope:
            raise ClaudeFailure('missing_structured_output')
        raw = json.dumps(envelope['structured_output'])
        try:
            _small(raw, 16 * 1024)
        except ValueError:
            raise ClaudeFailure('review_size_overflow') from None
        try:
            parse_review(raw)
        except ValueError:
            raise ClaudeFailure('invalid_review_contract') from None
        return raw


def run_product(*, run_models=False, approve: Callable, command=_run,
                resume_from_report=None, resume_input_manifest=None, allow_repair=False,
                candidate_from_report=None, emit=progress):
    if run_models is not True:
        raise PermissionError('Explicit model opt-in required')
    emit('Run started')
    try:
        report = _run_product(approve, command, resume_from_report,
                              resume_input_manifest, allow_repair, candidate_from_report, emit)
    except Exception:
        emit('Final stop reason: setup_error')
        raise
    else:
        emit('Final stop reason: ' + report.stop_reason)
        return report
    finally:
        emit('Run finished')


def _run_product(approve, command, resume_from_report, resume_input_manifest, allow_repair,
                 candidate_from_report, emit):
    if candidate_from_report is not None and (resume_from_report is not None
            or resume_input_manifest is not None or allow_repair):
        raise ValueError('New candidate runs cannot use historical resume or repair options')
    if resume_from_report is None and (resume_input_manifest is not None or allow_repair):
        raise ValueError('Resume options require a report')
    repo = Path(__file__).resolve().parent.parent
    # Fixed human-authored contract and acceptance tests. Never accept model paths.
    inputs = []
    for name in (REQUIREMENTS, TEST):
        path = repo / name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(repo):
            raise ValueError('Unsafe task input')
        inputs.append(_small(path.read_bytes().decode('utf-8'), 16 * 1024))
    task, test_source = inputs
    hashes = input_hashes(task, test_source)
    saved = None
    run_kind = 'generated'
    if resume_from_report is not None:
        saved = load_resume(resume_from_report, hashes, resume_input_manifest)
        run_kind = 'historical_resume'
    elif candidate_from_report is not None:
        saved = load_untrusted_candidate(candidate_from_report)
        run_kind = 'new_candidate'
        emit('New candidate-based run against current trusted inputs; not a historical resume')
    need_codex = saved is None or allow_repair
    executables = [shutil.which('codex') if need_codex else None, shutil.which('claude')]
    if not executables[1] or (need_codex and not executables[0]):
        raise RuntimeError('Required CLIs unavailable')
    with tempfile.TemporaryDirectory(prefix='product-agent-') as name:
        workspace = Path(name).resolve()
        if workspace.is_relative_to(repo):
            raise ValueError('Temporary workspace must be outside repository')
        env = model_env(workspace)
        for executable, expected in zip(executables, VERSIONS):
            if executable is None:
                continue
            actual = command([executable, '--version'], cwd=workspace, prompt=None,
                             timeout=10, child_env=env).stdout.strip()
            if actual != expected:
                raise RuntimeError('CLI version changed; recheck flags')
        codex = ProductCodex(workspace, command, executables[0], env) if need_codex else None
        limit = 3 if need_codex else 1
        candidate = ProductCandidate(codex, saved, limit, emit)
        tests = ProductTests(workspace, candidate, command, approve, test_source.encode('utf-8'), emit)
        claude = ProductClaude(workspace, command, executables[1], env, tests, emit)
        report = run_loop(task, candidate, tests, claude, max_rounds=limit)
        return ProductReport(**vars(report), inputs=hashes, diagnostics=claude.diagnostics,
                             run_kind=run_kind)


def approve_code(code, sha256):
    # JSON escapes terminal control sequences in model output.
    print(json.dumps({'path': TARGET, 'code': code, 'sha256': sha256}, ensure_ascii=True), file=sys.stderr)
    print(f'Type RUN {sha256} to approve this exact code in the isolated Worker: ',
          end='', file=sys.stderr, flush=True)
    return input() == f'RUN {sha256}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-models', action='store_true')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--resume-from-report', type=Path)
    source.add_argument('--candidate-from-report', type=Path,
                        help='Start a NEW single-round run from untrusted saved source using current inputs')
    parser.add_argument('--resume-input-manifest', type=Path,
                        help='Trusted original-run provenance for a legacy report without input hashes')
    parser.add_argument('--allow-repair', action='store_true',
                        help='Explicitly allow Codex repair rounds after resuming the saved candidate')
    args = parser.parse_args()
    if not args.run_models:
        parser.error('Models off by default; explicit --run-models required')
    try:
        report = run_product(run_models=True, approve=approve_code,
            resume_from_report=args.resume_from_report,
            resume_input_manifest=args.resume_input_manifest, allow_repair=args.allow_repair,
            candidate_from_report=args.candidate_from_report)
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        print(json.dumps({'setup_error': type(exc).__name__,
            'category': str(exc) if isinstance(exc, ResumeFailure) else 'setup_failure',
            'stage': ('candidate_validation_or_setup' if args.candidate_from_report else
                      'resume_validation_or_setup' if args.resume_from_report else 'setup')}))
        return 1
    output = report.to_dict()
    for item in output['rounds']:
        if item['implementation'] is not None:
            item['sha256'] = digest(item['implementation'])
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0 if report.stop_reason == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
