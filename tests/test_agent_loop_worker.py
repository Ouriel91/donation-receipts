"""Synthetic worker checks; Docker integration requires explicit local opt-in."""
import json
import os
from pathlib import Path

import pytest

from agent_loop.live import _run
from agent_loop.worker import DockerWorker, PATHS, validate_result


def stage(root, code=b'value = 1\n', tests=b'def test_value():\n    assert True\n'):
    for name, content in zip(PATHS, (code, tests)):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def test_cleanup_after_start_timeout(tmp_path):
    calls = []
    def command(args, **kwargs):
        from types import SimpleNamespace
        calls.append(args)
        if args[1] == 'start':
            raise RuntimeError('timeout')
        return SimpleNamespace(stdout='', returncode=0)
    with pytest.raises(RuntimeError, match='timeout'):
        DockerWorker(command).run(stage(tmp_path))
    assert calls[-2][1:4] == ['rm', '--force', '--volumes']
    assert calls[-1][1] == 'ps'


def test_extra_input_rejected_before_docker(tmp_path):
    stage(tmp_path)
    (tmp_path / 'private').write_text('dummy')
    with pytest.raises(ValueError, match='Unexpected worker input'):
        DockerWorker(lambda *a, **kw: pytest.fail('must not run')).run(tmp_path)


def test_report_identity_rejected():
    with pytest.raises(ValueError, match='different inputs'):
        validate_result(dict(exit=0, before={}, after={}, xml='', output=''), b'x', b'y')


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'skip', 'unexpected', 'counts'])
def test_report_cannot_claim_partial_or_changed_success(tmp_path, change):
    from test_agent_loop_product import Commands
    import xml.etree.ElementTree as ET
    command = Commands()
    result = DockerWorker(command).run(stage(tmp_path))
    root = ET.fromstring(result['xml'])
    suite = root[0]
    if change == 'missing':
        suite.remove(suite[-1])
        suite.set('tests', '84')
    elif change == 'duplicate':
        suite[-1].set('name', suite[0].get('name'))
    elif change == 'skip':
        ET.SubElement(suite[-1], 'skipped')
        suite.set('skipped', '1')
    elif change == 'unexpected':
        suite[-1].set('name', 'unexpected')
    else:
        suite.set('failures', '1')
    result['xml'] = ET.tostring(root, encoding='unicode')
    assert validate_result(result, b'value = 1\n', b'def test_value():\n    assert True\n')[0] == 'not_run'


docker = pytest.mark.skipif(os.environ.get('DONATION_WORKER_TESTS') != '1',
                            reason='explicit Docker integration opt-in required')


@docker
def test_real_isolation_and_readonly_inputs(tmp_path):
    sentinel = tmp_path / 'controller-only'
    sentinel.write_text('synthetic-controller-secret')
    code = f'''import os, socket, importlib.util
from pathlib import Path
assert importlib.util.find_spec('pip') is None
assert not Path({str(sentinel)!r}).exists()
assert not Path('/var/run/docker.sock').exists()
assert os.getuid() == 65534
assert socket.if_nameindex() == [(1, 'lo')]
s = socket.socket()
s.settimeout(1)
assert s.connect_ex(('1.1.1.1', 443)) != 0
for name in ('/input/src/organization_cache.py', '/input/tests/test_organization_cache.py', '/opt/worker.py', '/etc/probe'):
    try:
        Path(name).write_text('changed')
    except OSError:
        pass
    else:
        raise AssertionError('writable input/root: ' + name)
'''.encode()
    tests = b'from src import organization_cache\ndef test_isolation():\n    assert True\n'
    inputs = stage(tmp_path / 'input', code, tests)
    result = DockerWorker(_run).run(inputs)
    assert result['exit'] == 0, result['output']
    assert '1 passed' in result['output']
    assert result['before'] == result['after']
    assert sentinel.read_text() == 'synthetic-controller-secret'


@docker
def test_real_timeout_removes_descendants(tmp_path):
    code = b'import subprocess,time\nsubprocess.Popen(["python", "-c", "import time; time.sleep(600)"])\ntime.sleep(600)\n'
    tests = b'from src import organization_cache\ndef test_wait(): pass\n'
    with pytest.raises(RuntimeError):
        DockerWorker(_run).run(stage(tmp_path, code, tests), timeout=2)
    # Adapter checks that its uniquely named container was removed in finally.


@docker
def test_real_early_failure_has_diagnostic(tmp_path):
    result = DockerWorker(_run).run(stage(tmp_path, b'raise ValueError("synthetic import failure")\n',
        b'from src import organization_cache\ndef test_value(): pass\n'))
    status, detail = validate_result(result, (tmp_path / PATHS[0]).read_bytes(),
                                      (tmp_path / PATHS[1]).read_bytes())
    assert status == 'failed'
    assert 'synthetic import failure' in detail


@docker
def test_real_85_pass_transport(tmp_path, monkeypatch):
    import agent_loop.worker as worker
    code = b'value = 1\n'
    tests = b'import pytest\nfrom src.organization_cache import value\n@pytest.mark.parametrize("n", range(85))\ndef test_value(n): assert value == 1\n'
    monkeypatch.setattr(worker, 'EXPECTED', [f'test_value[{n}]' for n in range(85)])
    result = DockerWorker(_run).run(stage(tmp_path, code, tests))
    status, detail = validate_result(result, code, tests)
    assert status == 'passed', detail


@docker
def test_real_acceptance_failure_diagnostics_fit_transport(tmp_path):
    code = b'pass\n'  # No OrganizationCache implementation is supplied.
    tests = Path('tests/test_organization_cache.py').read_bytes()
    result = DockerWorker(_run).run(stage(tmp_path, code, tests))
    status, detail = validate_result(result, code, tests)
    assert status == 'failed'
    assert '85 errors' in detail
    assert 'OrganizationCache' in detail
