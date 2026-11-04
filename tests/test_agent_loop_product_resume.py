"""Mock-only review diagnostics and candidate recovery; no model is invoked."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_loop.product as product
from agent_loop.core import run_loop
from agent_loop.live import MAX_OUTPUT, OutputLimitError
from test_agent_loop_product import CODE, TEST_SOURCE, Commands, adapters


def prepared(tmp_path):
    commands = Commands()
    codex, tests, claude = adapters(tmp_path, commands)
    code = codex.run('task', None)
    result = tests.run()
    return code, result, claude


@pytest.mark.parametrize('response,category', [
    ('{broken private-marker', 'invalid_json'),
    ('{"is_error":false,"is_error":false}', 'invalid_json'),
    (json.dumps({'is_error': True, 'result': 'private-marker'}), 'cli_reported_error'),
    (json.dumps({'is_error': False}), 'missing_structured_output'),
    (json.dumps({'is_error': False, 'structured_output': None}), 'invalid_review_contract'),
    (json.dumps({'is_error': False, 'structured_output': {'status': 'approved',
        'findings': [{'id': 'x', 'kind': 'fix', 'summary': 'private-marker'}]}}), 'invalid_review_contract'),
    ('x' * (MAX_OUTPUT + 1), 'output_size_overflow'),
    (json.dumps({'is_error': False, 'structured_output': {'status': 'approved',
        'findings': [], 'extra': 'x' * 17000}}), 'review_size_overflow'),
])
def test_safe_review_categories(tmp_path, capsys, response, category):
    code, result, claude = prepared(tmp_path)
    claude.emit = product.progress
    claude.command = lambda *a, **kw: SimpleNamespace(stdout=response, returncode=0)
    with pytest.raises(product.ClaudeFailure, match=category):
        claude.review('task', code, result)
    assert claude.diagnostics == [{'stage': 'claude_review', 'category': category}]
    captured = capsys.readouterr()
    assert captured.out == ''
    assert category in captured.err and 'private-marker' not in captured.err


@pytest.mark.parametrize('error,category', [
    (RuntimeError('private-marker'), 'cli_failure'),
    (OSError('private-marker'), 'cli_failure'),
    (OutputLimitError('private-marker'), 'output_size_overflow'),
    (ValueError('private-marker'), 'invalid_json'),
])
def test_cli_error_categories(tmp_path, error, category):
    code, result, claude = prepared(tmp_path)
    def fail(*a, **kw):
        raise error
    claude.command = fail
    with pytest.raises(product.ClaudeFailure, match=category) as caught:
        claude.review('task', code, result)
    assert 'private-marker' not in str(caught.value)


def test_legitimate_large_envelope_under_existing_transport_cap(tmp_path):
    code, result, claude = prepared(tmp_path)
    raw = json.dumps({'is_error': False, 'result': 'x' * 20000,
                     'structured_output': {'status': 'approved', 'findings': []}})
    assert 16384 < len(raw.encode()) < MAX_OUTPUT
    claude.command = lambda *a, **kw: SimpleNamespace(stdout=raw, returncode=0)
    assert json.loads(claude.review('task', code, result))['status'] == 'approved'


def test_nonzero_cli_exit_is_not_accepted(tmp_path):
    code, result, claude = prepared(tmp_path)
    claude.command = lambda *a, **kw: SimpleNamespace(stdout='private-marker', returncode=1)
    with pytest.raises(product.ClaudeFailure, match='cli_failure'):
        claude.review('task', code, result)


def test_prompt_size_failure_is_safe_and_starts_no_cli(tmp_path):
    code, result, claude = prepared(tmp_path)
    claude.command = lambda *a, **kw: pytest.fail('oversized prompt must not reach CLI')
    with pytest.raises(product.ClaudeFailure, match='prompt_size_overflow'):
        claude.review('x' * (61 * 1024), code, result)


def test_identity_failure_is_categorized(tmp_path):
    code, result, claude = prepared(tmp_path)
    with pytest.raises(product.ClaudeFailure, match='code_identity_mismatch'):
        claude.review('task', code + '\n', result)
    (tmp_path / product.TARGET).write_text('changed')
    with pytest.raises(product.ClaudeFailure, match='code_identity_mismatch'):
        claude.review('task', code, result)


def setup_resume(tmp_path, monkeypatch, *, metadata=True):
    repo = tmp_path / 'repo'
    (repo / product.REQUIREMENTS).parent.mkdir(parents=True)
    (repo / product.TEST).parent.mkdir()
    (repo / product.REQUIREMENTS).write_text('fixed task')
    (repo / product.TEST).write_bytes(TEST_SOURCE)
    monkeypatch.setattr(product, '__file__', str(repo / 'agent_loop' / 'product.py'))
    monkeypatch.setattr(product.shutil, 'which', lambda name: name)
    hashes = product.input_hashes('fixed task', TEST_SOURCE.decode())
    data = {'stop_reason': 'tool_error', 'next_step': 'Inspect review failure', 'error': 'ValueError', 'rounds': [
        {'number': 1, 'implementation': CODE, 'sha256': product.digest(CODE),
         'tests': {'status': 'passed', 'detail': '85 acceptance tests passed.\n85 passed'}, 'review': None}]}
    if metadata:
        data['inputs'] = hashes
    path = tmp_path / 'saved.json'
    path.write_text(json.dumps(data))
    commands = Commands()
    def command(args, **kwargs):
        if args[-1] == '--version':
            return SimpleNamespace(stdout=product.VERSIONS[0 if args[0] == 'codex' else 1])
        return commands(args, **kwargs)
    return repo, path, hashes, data, commands, command


def test_resume_reapproves_retests_and_reviews_same_source_without_codex(tmp_path, monkeypatch, capsys):
    repo, path, hashes, data, commands, command = setup_resume(tmp_path, monkeypatch)
    approved = []
    def approve(code, sha):
        approved.append((code, sha))
        return True
    report = product.run_product(run_models=True, approve=approve, command=command, resume_from_report=path)
    assert report.stop_reason == 'success'
    assert report.inputs == hashes
    assert approved == [(CODE, product.digest(CODE))]
    assert not any(args[0] == 'codex' for args, _ in commands.calls)
    assert [args[1] for args, _ in commands.calls if args[0] == 'docker'].count('create') == 1
    assert commands.payloads[0]['code'] == CODE
    assert report.rounds[0].implementation == CODE
    assert not (repo / product.TARGET).exists()
    assert json.loads(path.read_text()) == data
    captured = capsys.readouterr()
    assert captured.out == ''
    expected = ['Run started', 'Round 1/1', 'Human approval requested', 'Human approval accepted',
        'Docker acceptance tests started', 'Docker acceptance tests completed: 85/85 passed',
        'Claude review started', 'Claude review completed', 'Final stop reason: success', 'Run finished']
    positions = [captured.err.index(item) for item in expected]
    assert positions == sorted(positions)
    assert CODE.strip() not in captured.err


@pytest.mark.parametrize('decline,failures,reviews,reason', [
    (True, 0, ['approved'], 'tests_not_run'),
    (False, 1, ['approved'], 'max_rounds'),
    (False, 0, ['changes_requested'], 'max_rounds'),
])
def test_resume_has_no_implicit_generation(tmp_path, monkeypatch, capsys, decline, failures, reviews, reason):
    _, path, _, _, commands, command = setup_resume(tmp_path, monkeypatch)
    commands.failures, commands.reviews = failures, reviews
    report = product.run_product(run_models=True, approve=lambda *a: not decline,
        command=command, resume_from_report=path)
    assert report.stop_reason == reason and len(report.rounds) == 1
    assert not any(args[0] == 'codex' for args, _ in commands.calls)
    if decline:
        assert commands.calls == []
        assert 'Human approval rejected' in capsys.readouterr().err
    elif failures:
        assert 'Docker acceptance tests completed: failed, 84/85 passed' in capsys.readouterr().err


def test_explicit_repair_uses_saved_source_and_feedback(tmp_path, monkeypatch, capsys):
    _, path, _, _, commands, command = setup_resume(tmp_path, monkeypatch)
    commands.reviews = ['changes_requested', 'approved']
    report = product.run_product(run_models=True, approve=lambda *a: True,
        command=command, resume_from_report=path, allow_repair=True)
    assert report.stop_reason == 'success' and len(report.rounds) == 2
    codex_payload = next(p for p in commands.payloads if 'previous_feedback' in p)
    assert codex_payload['previous_code'] == CODE
    assert 'Fix cache behavior' in codex_payload['previous_feedback']
    events = capsys.readouterr().err
    assert events.index('Round 1/3') < events.index('Round 2/3') < events.index('Codex started')


def test_normal_run_progress_and_input_metadata(tmp_path, monkeypatch, capsys):
    _, _, hashes, _, _, command = setup_resume(tmp_path, monkeypatch)
    report = product.run_product(run_models=True, approve=lambda *a: True, command=command)
    assert report.stop_reason == 'success' and report.to_dict()['inputs'] == hashes
    captured = capsys.readouterr()
    assert captured.out == ''
    expected = ['Round 1/3', 'Codex started', 'Codex completed', 'Human approval requested',
                'Claude review completed', 'Final stop reason: success']
    positions = [captured.err.index(item) for item in expected]
    assert positions == sorted(positions)


@pytest.mark.parametrize('change', ['task', 'tests', 'sha', 'code', 'syntax', 'status', 'count', 'rounds', 'duplicate', 'provenance'])
def test_invalid_resume_starts_no_process(tmp_path, monkeypatch, change):
    repo, path, _, data, _, _ = setup_resume(tmp_path, monkeypatch)
    if change in ('task', 'tests'):
        (repo / (product.REQUIREMENTS if change == 'task' else product.TEST)).write_text('changed')
    elif change == 'sha':
        data['rounds'][0]['sha256'] = '0' * 64
    elif change in ('code', 'syntax'):
        data['rounds'][0]['implementation'] = 'def broken('
        if change == 'syntax':
            data['rounds'][0]['sha256'] = product.digest('def broken(')
    elif change == 'status':
        data['rounds'][0]['tests']['status'] = 'failed'
    elif change == 'count':
        data['rounds'][0]['tests']['detail'] = '1 passed'
    elif change == 'rounds':
        data['rounds'] = []
    elif change == 'provenance':
        del data['inputs']
    if change == 'duplicate':
        path.write_text('{"inputs":{},"inputs":{}}')
    else:
        path.write_text(json.dumps(data))
    def forbidden(*a, **kw):
        pytest.fail('No subprocess or approval before resume validation')
    with pytest.raises((ValueError, SyntaxError)):
        product.run_product(run_models=True, approve=forbidden, command=forbidden, resume_from_report=path)


def test_legacy_report_requires_independent_report_bound_provenance(tmp_path, monkeypatch):
    _, path, hashes, _, _, command = setup_resume(tmp_path, monkeypatch, metadata=False)
    with pytest.raises(ValueError, match='original_input_hashes_missing_or_changed'):
        product.load_resume(path, hashes)
    manifest = tmp_path / 'provenance.json'
    proof = {'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'inputs': hashes}
    manifest.write_text(json.dumps(proof))
    assert product.load_resume(path, hashes, manifest) == CODE
    report = product.run_product(run_models=True, approve=lambda *a: True, command=command,
        resume_from_report=path, resume_input_manifest=manifest)
    assert report.stop_reason == 'success'
    proof['report_sha256'] = '0' * 64
    manifest.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match='invalid_resume_report'):
        product.load_resume(path, hashes, manifest)


def test_resume_rejects_symlink_and_oversized_files(tmp_path, monkeypatch):
    _, path, hashes, _, _, _ = setup_resume(tmp_path, monkeypatch)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(product.ResumeFailure):
        product.load_resume(link, hashes)
    path.write_bytes(b' ' * (256 * 1024 + 1))
    with pytest.raises(product.ResumeFailure):
        product.load_resume(path, hashes)


def test_diagnostics_survive_core_error_without_raw_response(tmp_path, monkeypatch):
    _, path, _, _, commands, original = setup_resume(tmp_path, monkeypatch)
    def command(args, **kwargs):
        if args[0] == 'claude' and args[-1] != '--version':
            return SimpleNamespace(stdout='private-marker', returncode=0)
        return original(args, **kwargs)
    report = product.run_product(run_models=True, approve=lambda *a: True, command=command, resume_from_report=path)
    assert report.stop_reason == 'tool_error'
    assert report.diagnostics == [{'stage': 'claude_review', 'category': 'invalid_json'}]
    assert 'private-marker' not in json.dumps(report.to_dict())


def test_cli_keeps_stdout_json(tmp_path, monkeypatch, capsys):
    _, path, _, _, _, command = setup_resume(tmp_path, monkeypatch)
    original = product.run_product
    monkeypatch.setattr(product, 'run_product', lambda **kw: original(**{**kw, 'command': command}))
    monkeypatch.setattr(product, 'approve_code', lambda *a: True)
    monkeypatch.setattr('sys.argv', ['product', '--run-models', '--resume-from-report', str(path)])
    assert product.main() == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data['rounds'][0]['sha256'] == product.digest(CODE)
    assert 'Run started' in captured.err and 'Run finished' in captured.err


def test_new_candidate_uses_current_inputs_without_historical_trust(tmp_path, monkeypatch, capsys):
    repo, path, _, original, commands, command = setup_resume(tmp_path, monkeypatch, metadata=False)
    (repo / product.REQUIREMENTS).write_text('current trusted task')
    current_tests = TEST_SOURCE + b'\n# current trusted acceptance\n'
    (repo / product.TEST).write_bytes(current_tests)
    hashes = product.input_hashes('current trusted task', current_tests.decode())
    with pytest.raises(product.ResumeFailure):
        product.load_resume(path, hashes)
    approvals, calls = [], []
    def approve(code, sha):
        approvals.append((code, sha))
        return True
    def recorded_command(args, **kwargs):
        calls.append(args)
        return command(args, **kwargs)
    report = product.run_product(run_models=True, approve=approve,
        command=recorded_command, candidate_from_report=path)
    assert report.stop_reason == 'success' and report.run_kind == 'new_candidate'
    assert report.inputs == hashes and report.to_dict()['run_kind'] == 'new_candidate'
    assert report.rounds[0].implementation == CODE
    assert approvals == [(CODE, product.digest(CODE))]
    assert not any(args[0] == 'codex' for args in calls)  # Includes version calls.
    assert sum(args[:2] == ['docker', 'create'] for args in calls) == 1
    assert next(i for i, args in enumerate(calls) if args[:2] == ['docker', 'start']) < next(
        i for i, args in enumerate(calls) if args[0] == 'claude' and '--version' not in args)
    assert commands.payloads[0]['requirements'] == 'current trusted task'
    assert commands.payloads[0]['trusted_tests'] == current_tests.decode()
    assert commands.payloads[0]['code'] == CODE
    assert json.loads(path.read_text()) == original
    assert not (repo / product.TARGET).exists()
    with pytest.raises(product.ResumeFailure):
        product.load_resume(path, hashes)  # Historical validation is unchanged.
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'New candidate-based run against current trusted inputs; not a historical resume' in captured.err
    assert '85/85 passed' in captured.err and 'Claude review completed' in captured.err


@pytest.mark.parametrize('decline,failures,reviews,reason', [
    (True, 0, ['approved'], 'tests_not_run'),
    (False, 1, ['approved'], 'max_rounds'),
    (False, 0, ['changes_requested'], 'max_rounds'),
])
def test_new_candidate_stops_without_automatic_generation(tmp_path, monkeypatch, decline, failures, reviews, reason):
    _, path, _, _, commands, command = setup_resume(tmp_path, monkeypatch, metadata=False)
    commands.failures, commands.reviews = failures, reviews
    report = product.run_product(run_models=True, approve=lambda *a: not decline,
        command=command, candidate_from_report=path)
    assert report.stop_reason == reason and len(report.rounds) == 1
    assert not any(args[0] == 'codex' for args, _ in commands.calls)
    if decline:
        assert commands.calls == []
    elif failures:
        assert commands.payloads == []  # No Claude call without fresh passing tests.


@pytest.mark.parametrize('bad', ['sha', 'syntax', 'size', 'shape', 'duplicate'])
def test_new_candidate_validation_precedes_approval_and_subprocess(tmp_path, monkeypatch, bad):
    _, path, _, data, _, _ = setup_resume(tmp_path, monkeypatch, metadata=False)
    if bad == 'sha':
        data['rounds'][0]['sha256'] = '0' * 64
    elif bad in ('syntax', 'size'):
        code = 'def broken(' if bad == 'syntax' else '#' + 'x' * product.MAX_CODE
        data['rounds'][0]['implementation'] = code
        data['rounds'][0]['sha256'] = product.digest(code)
    elif bad == 'shape':
        data['rounds'] = []
    path.write_text(json.dumps(data) if bad != 'duplicate' else '{"rounds":[],"rounds":[]}')
    def forbidden(*a, **kw):
        pytest.fail('Invalid source must not reach approval or subprocess')
    with pytest.raises(ValueError, match='Invalid candidate report'):
        product.run_product(run_models=True, approve=forbidden, command=forbidden,
                            candidate_from_report=path)


@pytest.mark.parametrize('other', ['resume_from_report', 'resume_input_manifest', 'allow_repair'])
def test_new_candidate_options_cannot_weaken_resume(tmp_path, monkeypatch, other):
    _, path, _, _, _, _ = setup_resume(tmp_path, monkeypatch, metadata=False)
    def forbidden(*a, **kw):
        pytest.fail('Conflicting modes must fail before subprocess or approval')
    with pytest.raises(ValueError, match='cannot use historical resume or repair options'):
        product.run_product(run_models=True, approve=forbidden, command=forbidden,
            candidate_from_report=path, **{other: True if other == 'allow_repair' else path})


def test_new_candidate_cli_reapproves_full_hash_and_emits_clean_json(tmp_path, monkeypatch, capsys):
    _, path, _, _, _, command = setup_resume(tmp_path, monkeypatch, metadata=False)
    original = product.run_product
    monkeypatch.setattr(product, 'run_product', lambda **kw: original(**{**kw, 'command': command}))
    monkeypatch.setattr('builtins.input', lambda: 'RUN ' + product.digest(CODE))
    monkeypatch.setattr('sys.argv', ['product', '--run-models', '--candidate-from-report', str(path)])
    assert product.main() == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data['run_kind'] == 'new_candidate'
    assert data['rounds'][0]['sha256'] == product.digest(CODE)
    assert f'Type RUN {product.digest(CODE)}' in captured.err
    assert 'Run finished' in captured.err
