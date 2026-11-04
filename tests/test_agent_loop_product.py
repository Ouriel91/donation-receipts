"""All model calls are fake; generated source is never executed here."""
import json
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_loop.core import run_loop, TestResult as LoopTestResult
from agent_loop.product import (ProductCodex, ProductTests, ProductClaude,
                                TARGET, TEST, digest, run_product)

CODE = 'value = 1\n'
TEST_SOURCE = b'def test_contract():\n    assert True\n'


class Commands:
    def __init__(self, failures=0, reviews=None):
        self.calls = []
        self.failures = failures
        self.reviews = list(reviews or ['approved'])
        self.payloads = []
        self.mutate = None
        self.code = CODE

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == 'codex':
            self.payloads.append(json.loads(kwargs['prompt']))
            return SimpleNamespace(stdout=json.dumps({'code': self.code}), returncode=0)
        if args[0] == 'claude':
            self.payloads.append(json.loads(kwargs['prompt']))
            status = self.reviews.pop(0)
            findings = [] if status == 'approved' else [
                {'id': 'R1', 'kind': 'fix' if status == 'changes_requested' else 'human_decision',
                 'summary': 'Fix cache behavior'}]
            return SimpleNamespace(stdout=json.dumps({'is_error': False,
                'structured_output': {'status': status, 'findings': findings}}), returncode=0)
        assert args[0] == 'docker'
        action = args[1]
        if action == 'create':
            self.inputs = Path(kwargs['cwd'])
            return SimpleNamespace(stdout='container-id', returncode=0)
        if action == 'inspect':
            return SimpleNamespace(stdout=json.dumps({'Running': False, 'OOMKilled': False, 'ExitCode': 0}), returncode=0)
        if action != 'start':
            return SimpleNamespace(stdout='', returncode=0)
        failed = int(self.failures > 0)
        self.failures -= failed
        from agent_loop.worker import EXPECTED, PATHS
        identity = {p: hashlib.sha256((self.inputs / p).read_bytes()).hexdigest() for p in PATHS}
        suite = ET.Element('testsuite', tests='85', failures=str(failed), errors='0', skipped='0')
        for i, name in enumerate(EXPECTED):
            case = ET.SubElement(suite, 'testcase', name=name)
            if failed and i == 0:
                ET.SubElement(case, 'failure')
        root = ET.Element('testsuites')
        root.append(suite)
        if self.mutate:
            self.mutate(self.inputs)
        after = {p: hashlib.sha256((self.inputs / p).read_bytes()).hexdigest() for p in PATHS}
        return SimpleNamespace(stdout=json.dumps({'exit': failed, 'before': identity, 'after': after,
            'xml': ET.tostring(root, encoding='unicode'),
            'output': 'assert value == 2' if failed else '85 passed'}), returncode=0)



def adapters(tmp_path, command, approve=lambda code, sha: True):
    codex = ProductCodex(tmp_path, command, 'codex', {})
    tests = ProductTests(tmp_path, codex, command, approve, TEST_SOURCE)
    claude = ProductClaude(tmp_path, command, 'claude', {}, tests)
    return codex, tests, claude


def test_success_identity_and_fixed_commands(tmp_path):
    command = Commands()
    approved = []
    def approve(code, sha):
        approved.append((code, sha))
        return True
    report = run_loop('cache requirements', *adapters(tmp_path, command, approve))
    assert report.stop_reason == 'success'
    payload = command.payloads[-1]
    assert approved == [(CODE, digest(CODE))]
    assert payload['code'] == report.rounds[0].implementation == CODE
    assert payload['sha256'] == digest(CODE)
    assert payload['requirements'] == 'cache requirements'
    assert payload['trusted_tests'] == TEST_SOURCE.decode()
    codex_args = command.calls[0][0]
    pytest_args = command.calls[1][0]
    claude_args = next(args for args, _ in command.calls if args[0] == 'claude')
    assert '--ignore-user-config' in codex_args
    assert codex_args[codex_args.index('--sandbox') + 1] == 'read-only'
    for feature in ('shell_tool', 'unified_exec', 'code_mode_host', 'multi_agent', 'plugins'):
        assert codex_args[codex_args.index(feature) - 1] == '--disable'
    assert '--network=none' in pytest_args and '--read-only' in pytest_args
    assert claude_args[claude_args.index('--tools') + 1] == ''
    assert set(command.calls[1][1]['child_env']).isdisjoint({'CODEX_HOME', 'ANTHROPIC_API_KEY'})


@pytest.mark.parametrize('failures,reviews,rounds', [
    (1, ['approved'], 2), (0, ['changes_requested', 'approved'], 2),
    (3, ['approved'], 3)])
def test_feedback_and_limit(tmp_path, failures, reviews, rounds):
    command = Commands(failures, reviews)
    report = run_loop('cache', *adapters(tmp_path, command))
    assert len(report.rounds) == rounds
    assert report.stop_reason == ('max_rounds' if failures == 3 else 'success')
    feedback = [p['previous_feedback'] for p in command.payloads if 'previous_feedback' in p]
    assert feedback[0] is None and feedback[1]
    if failures:
        assert 'assert value == 2' in feedback[1]
    else:
        assert 'Fix cache behavior' in feedback[1]


def test_declined_never_runs_or_writes_code(tmp_path):
    command = Commands()
    report = run_loop('cache', *adapters(tmp_path, command, lambda *args: False))
    assert report.stop_reason == 'tests_not_run'
    assert len(command.calls) == 1
    assert not (tmp_path / TARGET).exists()


@pytest.mark.parametrize('name', [TARGET, TEST])
def test_mutation_during_tests_blocks_review(tmp_path, name):
    command = Commands()
    command.mutate = lambda root: (root / name).write_text('changed')
    report = run_loop('cache', *adapters(tmp_path, command))
    assert report.stop_reason == 'tool_error'
    assert not any(args[0] == 'claude' for args, _ in command.calls)


def test_changed_review_code_rejected(tmp_path):
    command = Commands()
    codex, tests, claude = adapters(tmp_path, command)
    code = codex.run('cache', None)
    result = tests.run()
    with pytest.raises(ValueError):
        claude.review('cache', code + '\n', result)
    (tmp_path / TARGET).write_text('changed')
    with pytest.raises(ValueError):
        claude.review('cache', code, result)


def test_target_symlink_rejected(tmp_path):
    command = Commands()
    codex, tests, _ = adapters(tmp_path, command)
    other = tmp_path / 'untouched'
    other.write_text('original')
    (tmp_path / TARGET).symlink_to(other)
    codex.run('cache', None)
    with pytest.raises(ValueError):
        tests.run()
    assert other.read_text() == 'original'


@pytest.mark.parametrize('code', ['', '```python\nx=1\n```', 'def broken('])
def test_invalid_code_never_runs(tmp_path, code):
    command = Commands()
    command.code = code
    report = run_loop('cache', *adapters(tmp_path, command))
    assert report.stop_reason == 'tool_error'
    assert len(command.calls) == 1


def test_opt_in_required():
    with pytest.raises(PermissionError):
        run_product(approve=lambda *args: True)


def test_human_review_stops(tmp_path):
    report = run_loop('cache', *adapters(tmp_path, Commands(reviews=['human_decision'])))
    assert report.stop_reason == 'human_decision'




def test_setup_version_gate_and_full_wiring(tmp_path, monkeypatch):
    import agent_loop.product as product
    repo = tmp_path / 'repo'
    (repo / 'agent_loop' / 'tasks').mkdir(parents=True)
    (repo / 'tests').mkdir()
    (repo / product.REQUIREMENTS).write_text('Implement cache contract')
    (repo / TEST).write_bytes(TEST_SOURCE)
    monkeypatch.setattr(product, '__file__', str(repo / 'agent_loop' / 'product.py'))
    monkeypatch.setattr(product.shutil, 'which', lambda name: name)
    commands = Commands()
    versions = []
    def command(args, **kwargs):
        if args[-1] == '--version':
            versions.append(args[0])
            return SimpleNamespace(stdout=product.VERSIONS[0 if args[0] == 'codex' else 1])
        return commands(args, **kwargs)
    report = product.run_product(run_models=True, approve=lambda *args: True, command=command)
    assert report.stop_reason == 'success'
    assert versions == ['codex', 'claude']
    assert not (repo / TARGET).exists()
    assert not Path(commands.calls[0][1]['cwd']).exists()
    with pytest.raises(RuntimeError, match='version changed'):
        product.run_product(run_models=True, approve=lambda *args: True,
                            command=lambda *args, **kwargs: SimpleNamespace(stdout='unknown'))


def test_missing_contract_starts_no_process(tmp_path, monkeypatch):
    import agent_loop.product as product
    monkeypatch.setattr(product, '__file__', str(tmp_path / 'agent_loop' / 'product.py'))
    def command(*args, **kwargs):
        pytest.fail('No subprocess may start before contract exists')
    with pytest.raises(FileNotFoundError):
        product.run_product(run_models=True, approve=lambda *args: True, command=command)


@pytest.mark.parametrize('xml,returncode', [
    ('<testsuite tests="1" failures="0" errors="0" skipped="1"/>', 0),
    ('<testsuite tests="1" failures="1" errors="0" skipped="0"/>', 0),
    ('<testsuite tests="0" failures="0" errors="0" skipped="0"/>', 0),
    ('not xml', 0),
])
def test_invalid_or_skipped_test_results_cannot_pass(tmp_path, xml, returncode):
    commands = Commands()
    def command(args, **kwargs):
        result = commands(args, **kwargs)
        if args[:2] == ['docker', 'start']:
            data = json.loads(result.stdout)
            data['xml'] = xml
            data['exit'] = returncode
            result.stdout = json.dumps(data)
        return result
    report = run_loop('cache', *adapters(tmp_path, command))
    assert report.stop_reason in {'tool_error', 'tests_not_run'}
    assert not any(args[0] == 'claude' for args, _ in commands.calls)


def test_mutation_during_review_prevents_success(tmp_path):
    commands = Commands()
    def command(args, **kwargs):
        result = commands(args, **kwargs)
        if args[0] == 'claude':
            (tmp_path / TARGET).write_text('changed')
        return result
    assert run_loop('cache', *adapters(tmp_path, command)).stop_reason == 'tool_error'


def test_extra_model_paths_rejected(tmp_path):
    def command(*args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({'code': CODE, 'files': {'other.py': CODE}}))
    report = run_loop('cache', *adapters(tmp_path, command))
    assert report.stop_reason == 'tool_error'
    assert not (tmp_path / TARGET).exists()


def test_approval_only_on_stderr(monkeypatch, capsys):
    from agent_loop.product import approve_code
    monkeypatch.setattr('builtins.input', lambda: 'RUN ' + digest(CODE))
    assert approve_code(CODE, digest(CODE))
    streams = capsys.readouterr()
    assert streams.out == ''
    assert 'Type RUN' in streams.err


def test_unexpected_controller_file_not_in_worker(tmp_path):
    commands = Commands()
    codex, tests, _ = adapters(tmp_path, commands)
    (tmp_path / 'controller-only').write_text('dummy')
    codex.run('cache', None)
    assert tests.run().status == 'passed'
    assert not (commands.inputs / 'controller-only').exists()
