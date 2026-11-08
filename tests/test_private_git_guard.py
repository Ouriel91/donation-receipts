import errno
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest
from src.private_git_guard import check_blob, audit


@pytest.mark.parametrize('path', ['runtime/registry/organizations.json', 'docs/data/copy.json',
    'cache/official.json', 'official.csv', 'copy.xlsx', 'tax/section46_evidence/e.json',
    'credentials.json', '.env.local', 'nested/token.json'])
def test_private_path_blocked(path):
    assert check_blob(path, b'{}')


@pytest.mark.parametrize('payload', [b'{"organizations":{}}', b'{"tax_evidence":{}}',
    b'{"nested":{"refresh_token":"fake"}}', b'{"donor_name":"Synthetic"}'])
def test_disguised_operational_json_blocked(payload):
    assert check_blob('docs/innocent.json', payload) in {'operational_json_content', 'operational_text_content'}


def test_code_synthetic_and_docs_allowed():
    assert check_blob('tests/test_synthetic.py', b'assert 1 == 1\n') is None
    assert check_blob('docs/guide.md', 'הוראות ללא נתונים'.encode()) is None
    assert check_blob('examples/account-config.example.json', b'{"display_name":"Example account"}') is None
    assert check_blob('src/pretend.py', b'print(1)', '120000')
    assert check_blob('source.py', b'\x00binary')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)


def test_force_added_and_previously_tracked_private_data_rejected(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / '.gitignore').write_text('runtime/\n')
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/state.json').write_text('{}')
    git(tmp_path, 'add', '-f', '.gitignore', 'runtime/state.json')
    assert audit(tmp_path)['status'] == 'blocked'
    # Test-only isolated repository commits, never the actual project.
    git(tmp_path, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Synthetic private history')
    git(tmp_path, 'rm', 'runtime/state.json')
    assert audit(tmp_path)['status'] == 'passed'
    assert audit(tmp_path, history=True)['status'] == 'blocked'


def test_index_blobs_not_worktree_are_checked(tmp_path):
    git(tmp_path, 'init', '-q')
    path = tmp_path / 'innocent.json'
    path.write_text('{"organizations":{}}')
    git(tmp_path, 'add', 'innocent.json')
    path.write_text('{}')
    assert audit(tmp_path)['status'] == 'blocked'


def test_git_failure_is_not_a_pass(tmp_path):
    from src.private_git_guard import main
    assert main(['--repo', str(tmp_path)]) == 1


@pytest.mark.parametrize('path,raw', [
    ('copy.json', b'\xef\xbb\xbf{"organizations":{}}'),
    ('copy.txt', b'"{\\"organizations\\":{}}"'),
    ('copy.txt', b'{"organizations":{}} trailing'),
    ('copy.yaml', b'Organizations: {}'),
    ('copy.txt', b'id,name\n123456789,Synthetic'),
    ('copy.json', b'not json'),
    ('token-prod.json', b'{}'),
])
def test_content_and_filename_bypasses_blocked(path, raw):
    assert check_blob(path, raw)


@pytest.mark.parametrize('prefix', ['GOCSPX-', 'ya29.', '1//', 'AIza', 'github_pat_'])
def test_secret_families_blocked(prefix):
    assert check_blob('notes.md', (prefix + 'A' * 40).encode())


def test_hook_install_is_explicit_and_preserves_configuration(tmp_path):
    from scripts.install_private_hooks import configure
    git(tmp_path, 'init', '-q')
    (tmp_path / 'code.py').write_text('print(1)\n')
    git(tmp_path, 'add', 'code.py')
    git(tmp_path, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Synthetic setup')
    hooks = tmp_path / '.githooks'
    hooks.mkdir()
    with pytest.raises(ValueError, match='Missing, non-executable or linked hook'):
        configure(tmp_path, install=True)
    hook_files = [hooks / name for name in ('pre-commit', 'pre-push')]
    for path in hook_files:
        path.write_text('#!/bin/sh\nexit 1\n')
    assert not configure(tmp_path)['hooks_active']
    if os.name != 'nt':
        for mode in (0o644, 0o755):
            try:
                for path in hook_files:
                    path.chmod(mode)
            except NotImplementedError:
                break
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.ENOTSUP):
                    raise
                break
            if mode == 0o644 and not any(os.access(path, os.X_OK) for path in hook_files):
                with pytest.raises(ValueError):
                    configure(tmp_path, install=True)
    # Windows checks presence, links and configuration, not POSIX execute bits.
    # A POSIX filesystem without executable hooks must still fail closed.
    if os.name == 'nt' or all(os.access(path, os.X_OK) for path in hook_files):
        assert configure(tmp_path, install=True)['hooks_active']
    else:
        with pytest.raises(ValueError):
            configure(tmp_path, install=True)
    git(tmp_path, 'config', 'core.hooksPath', 'other-hooks')
    with pytest.raises(ValueError):
        configure(tmp_path, install=True)
    assert git(tmp_path, 'config', '--get', 'core.hooksPath').stdout.strip() == b'other-hooks'


@pytest.mark.parametrize('platform,executable,can_install', [
    ('nt', False, True), ('posix', False, False), ('posix', True, True),
])
def test_hook_install_platform_policy(tmp_path, monkeypatch, platform, executable, can_install):
    import scripts.install_private_hooks as installer
    hooks = tmp_path / '.githooks'
    hooks.mkdir()
    for name in ('pre-commit', 'pre-push'):
        (hooks / name).write_text('#!/bin/sh\nexit 1\n')
    changes = []
    def command(args, **kwargs):
        if args[-3:] == ['config', '--get', 'core.hooksPath']:
            return SimpleNamespace(returncode=1, stdout='')
        assert args[-4:] == ['config', '--local', 'core.hooksPath', '.githooks']
        changes.append(args)
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(installer, 'audit', lambda *a, **kw: {'status': 'passed'})
    # Replace only this module's OS facade; do not change pathlib's host platform.
    monkeypatch.setattr(installer, 'os', SimpleNamespace(
        name=platform, X_OK=os.X_OK, access=lambda *args: executable))
    monkeypatch.setattr(installer, 'subprocess', SimpleNamespace(run=command))
    assert not installer.configure(tmp_path)['hooks_active']
    assert not changes  # Inspection never installs hooks on either platform.
    if can_install:
        assert installer.configure(tmp_path, install=True)['hooks_active']
        assert len(changes) == 1
    else:
        with pytest.raises(ValueError, match='non-executable'):
            installer.configure(tmp_path, install=True)
        assert not changes


def test_synthetic_pdf_is_pinned_to_exact_bytes():
    assert check_blob('harness/attachments/fake_receipt.pdf', b'%PDF a different real receipt')


@pytest.mark.parametrize('raw', [b'name,012345678', b'012345678;name', b'| 012345678 | name |',
    b'{"012345678":"name"}', b'<organizations>data</organizations>', b'organizations = []'])
def test_more_text_encodings_blocked(raw):
    assert check_blob('copy.txt', raw)


def test_outgoing_blob_and_malformed_protocol_are_blocked(tmp_path, monkeypatch):
    import io
    from src.private_git_guard import main
    git(tmp_path, 'init', '-q')
    (tmp_path / 'code.py').write_text('print(1)')
    git(tmp_path, 'add', 'code.py')
    git(tmp_path, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Synthetic commit')
    oid = git(tmp_path, 'hash-object', '-w', 'code.py').stdout.decode().strip()
    monkeypatch.setattr('sys.stdin', io.StringIO(f'refs/tags/synthetic {oid} refs/tags/synthetic {"0"*40}\n'))
    assert main(['--repo', str(tmp_path), '--pre-push-input']) == 1
    monkeypatch.setattr('sys.stdin', io.StringIO('malformed'))
    assert main(['--repo', str(tmp_path), '--pre-push-input']) == 1


@pytest.mark.parametrize('path,raw', [
    ('copy.json', b'[580000000,580000001]'),
    ('copy.json', b'{"rows":"580000000,name"}'),
    ('copy.txt', b'580000000\n580000001'),
    ('tests/unreviewed.py', b'DATA = {"organizations":{"580000000":{}}}'),
    ('src/unreviewed.py', b'DATA = {"organizations":{}}'),
])
def test_unreviewed_code_and_numeric_exports_blocked(path, raw):
    assert check_blob(path, raw)


def test_push_deletion_and_explicit_sha_forwarded(monkeypatch):
    import io
    import src.private_git_guard as module
    received = []
    def audited(repo, **kwargs):
        received.append(kwargs['extra_revisions'])
        return dict(status='passed', index_entries=0, violations=[])
    monkeypatch.setattr(module, 'audit', audited)
    for oid, expected in [('0'*40, ()), ('a'*40, ('a'*40,))]:
        local = '(delete)' if oid == '0' * 40 else 'refs/heads/synthetic'
        remote_oid = 'b' * 40 if local == '(delete)' else '0' * 40
        monkeypatch.setattr('sys.stdin', io.StringIO(f'{local} {oid} refs/heads/synthetic {remote_oid}\n'))
        assert module.main(['--pre-push-input']) == 0
        assert received[-1] == expected


def test_current_task2_sources_can_be_added_without_guard_self_block():
    root = Path(__file__).resolve().parents[1]
    paths = ['src/private_git_guard.py', 'src/registry_sync.py', 'src/section46.py',
        'src/section46_cli.py', 'src/private_runtime.py', 'src/organization_cache.py',
        'src/organization_enrichment.py', 'tests/test_private_git_guard.py',
        'tests/test_section46.py', 'tests/test_weekly_registry_sync.py',
        '.githooks/pre-commit', '.githooks/pre-push', '.github/workflows/private-data.yml',
        'scheduling/task-scheduler.ps1', 'scheduling/weekly-registry.ps1',
        'scripts/install_private_hooks.py', '.gitignore']
    for name in paths:
        path = root / name
        # These are intended Git index modes, independent of host stat semantics.
        is_hook = name in {'.githooks/pre-commit', '.githooks/pre-push'}
        mode = '100755' if is_hook else '100644'
        assert check_blob(name, path.read_bytes(), mode) is None, name
        if is_hook:
            assert check_blob(name, path.read_bytes(), '100644') == 'non_executable_hook'


@pytest.mark.parametrize('path,raw', [
    ('copy.py', b'data = {"official_name": name}'),
    ('copy.py', b'data = dict(organizations=rows)'),
    ('copy.py', b'data = (123_456_789,)'),
    ('copy.py', b'data = {0x75bcd15}'),
    ('copy.py', b'data = "organizations: []"'),
    ('copy.toml', b'[organizations]'),
    ('copy.toml', b'[[registry]]'),
    ('copy.toml', b'organizations.x = 1'),
    ('copy.xml', b'<organizations id="synthetic"/>'),
    ('copy.txt', b'official_name,registration_number'),
    ('copy.txt', b'{"organiza\\u0074ions":1}\n{"safe":2}'),
    ('copy.sh', b'cat <<EOF\n{"organizations":{}}\nEOF'),
    ('copy.ps1', b'$x = \'{"registry":{}}\''),
])
def test_reviewed_content_bypasses_fail_closed(path, raw):
    assert check_blob(path, raw)


def test_subdirectory_audit_covers_whole_index(tmp_path):
    git(tmp_path, 'init', '-q')
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/private.txt').write_text('synthetic')
    git(tmp_path, 'add', 'data/private.txt')
    assert audit(tmp_path / 'nested')['status'] == 'blocked'


@pytest.mark.parametrize('line', [
    'refs/heads/a {oid} refs/heads/a garbage\n',
    'refs/heads/a {oid} invalid {zero}\n',
    'refs/heads/a\t{oid} refs/heads/a {zero}\n',
    'refs/heads/a {oid} refs/heads/a {zero}\r\n',
    'refs/heads/a {oid} refs/heads/a {zero}',
])
def test_push_protocol_strict_fields(monkeypatch, line):
    import io
    from src.private_git_guard import main
    monkeypatch.setattr('sys.stdin', io.StringIO(line.format(oid='a'*40, zero='0'*40)))
    assert main(['--pre-push-input']) == 1


@pytest.mark.parametrize('local,oid', [('refs/heads/synthetic', '0' * 40), ('(delete)', 'a' * 40)])
def test_mismatched_deletion_protocol_blocked(monkeypatch, local, oid):
    import io
    import src.private_git_guard as module
    monkeypatch.setattr('sys.stdin', io.StringIO(f'{local} {oid} refs/heads/synthetic {"b" * 40}\n'))
    monkeypatch.setattr(module, 'audit', lambda *a, **kw: pytest.fail('Invalid protocol must not reach audit'))
    assert module.main(['--pre-push-input']) == 1
