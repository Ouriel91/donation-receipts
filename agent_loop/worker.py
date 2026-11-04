"""Disposable Docker pytest adapter. No controller paths except staged inputs."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

IMAGE = 'sha256:defe3598b6ac9843ac3bc2dd374d1420c80a27ec487eec1c7a66489ae1600a4d'
PATHS = ('src/organization_cache.py', 'tests/test_organization_cache.py')
EXPECTED = json.loads(Path(__file__).with_name('acceptance_ids.json').read_text())


class DockerWorker:
    def __init__(self, command):
        self.command = command

    def run(self, inputs, timeout=300):
        inputs = inputs.resolve(strict=True)
        names = {p.relative_to(inputs).as_posix() for p in inputs.rglob('*')}
        if names != {*PATHS, 'src', 'tests'} or any(p.is_symlink() for p in inputs.rglob('*')):
            raise ValueError('Unexpected worker input')
        inputs.chmod(0o755)
        for path in inputs.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o444)
        name = 'product-worker-' + uuid4().hex
        # Never forward HOME, Docker context/config, login files, or model env.
        env = {'PATH': os.defpath, 'DOCKER_HOST': 'unix:///var/run/docker.sock'}

        def docker(*args, limit=30, exits=(0,)):
            return self.command(['docker', *args], cwd=inputs, prompt=None,
                timeout=limit, exit_codes=exits, child_env=env)

        try:
            docker('create', '--name', name, '--pull=never', '--network=none',
                '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--user=65534:65534', '--pids-limit=64', '--memory=256m',
                '--memory-swap=256m', '--cpus=1', '--ulimit=nofile=128:128',
                '--log-driver=none', '--ipc=none',
                '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777',
                '--mount', f'type=bind,source={inputs},target=/input,readonly', IMAGE)
            result = docker('start', '--attach', name, limit=timeout, exits=(0, 1, 2, 124, 137))
            state = json.loads(docker('inspect', '--format', '{{json .State}}', name).stdout)
            if state['Running'] or state['OOMKilled'] or state['ExitCode'] != 0:
                raise RuntimeError('Worker failed or exceeded limits; no test result accepted')
            if docker('diff', name).stdout.strip():
                raise ValueError('Unexpected worker filesystem changes')
            if len(result.stdout.encode()) > 192 * 1024:
                raise ValueError('Oversized worker result')
            return json.loads(result.stdout)
        finally:
            docker('rm', '--force', '--volumes', name, exits=(0, 1))
            if docker('ps', '-aq', '--filter', f'name=^/{name}$').stdout.strip():
                raise RuntimeError('Worker cleanup could not be verified')


def validate_result(data, code, tests):
    """Validate transport, identity, cases and counts; JUnit is not a security proof."""
    if not isinstance(data, dict) or set(data) != {'exit', 'before', 'after', 'xml', 'output'}:
        raise ValueError('Unexpected worker report')
    identity = {p: hashlib.sha256(b).hexdigest() for p, b in zip(PATHS, (code, tests))}
    if data['before'] != identity or data['after'] != identity:
        raise ValueError('Worker tested different inputs')
    detail = data['output']
    if not isinstance(detail, str) or len(detail) > 32768:
        raise ValueError('Invalid worker diagnostics')
    if type(data['exit']) is not int or data['exit'] not in (0, 1, 2):
        return 'not_run', 'pytest could not complete: ' + detail
    try:
        if (not isinstance(data['xml'], str) or len(data['xml']) > 128 * 1024
                or '<!DOCTYPE' in data['xml'] or '<!ENTITY' in data['xml']):
            raise ValueError('Missing or oversized XML')
        root = ET.fromstring(data['xml'])
        if root.tag != 'testsuites' or len(root) != 1 or root[0].tag != 'testsuite':
            raise ValueError('Invalid suite')
        suite = root[0]
        total, failed, errors, skipped = (int(suite.attrib[k]) for k in
                                         ('tests', 'failures', 'errors', 'skipped'))
        cases = list(suite)
        ids = [c.attrib['name'] for c in cases]
        if (min(total, failed, errors, skipped) < 0 or total != len(cases)
                or total < failed + errors + skipped or len(ids) != len(set(ids))
                or any(c.tag != 'testcase' for c in cases)):
            raise ValueError('Invalid cases/counts')
        actual = [sum(c.find(k) is not None for c in cases) for k in ('failure', 'error', 'skipped')]
        if actual != [failed, errors, skipped]:
            raise ValueError('Conflicting counts')
    except (ValueError, KeyError, ET.ParseError):
        return 'not_run', 'Invalid pytest report; ' + detail
    if data['exit'] == 0 and failed == errors == skipped == 0 and sorted(ids) == sorted(EXPECTED):
        return 'passed', '85 acceptance tests passed.\n' + detail
    if data['exit'] in (1, 2) and failed + errors > 0:
        return 'failed', f'{failed} failed, {errors} errors; acceptance incomplete.\n' + detail
    return 'not_run', 'Expected exactly all 85 acceptance cases without skips; ' + detail
