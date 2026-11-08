from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import pytest
from src.registry_sync import refresh, official_url
from src.organization_cache import OrganizationCache

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
CSV = 'מספר עמותה,שם עמותה בעברית\n012345678,עמותה סינתטית\n'.encode()


def test_weekly_hash_and_manual(tmp_path):
    calls = []
    def download():
        calls.append(True)
        return CSV
    root = tmp_path / 'runtime'
    assert refresh(root, downloader=download, clock=lambda: NOW)['updated']
    cache = root / 'organizations.json'
    before = cache.read_bytes(), cache.stat().st_mtime_ns
    assert refresh(root, downloader=download, clock=lambda: NOW + timedelta(days=6))['status'] == 'not_due'
    assert len(calls) == 1
    result = refresh(root, downloader=download, clock=lambda: NOW + timedelta(days=7))
    assert result['status'] == 'success' and not result['updated']
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before
    assert len(calls) == 2
    csv = tmp_path / 'manual.csv'
    csv.write_bytes(CSV)
    assert refresh(root, manual_csv=csv, downloader=lambda: pytest.fail('network'), clock=lambda: NOW + timedelta(days=8))['mode'] == 'manual_csv'


@pytest.mark.parametrize('response', [b'<html>blocked</html>', b'', b'garbage'])
def test_bad_download_preserves_last_good(tmp_path, response):
    assert refresh(tmp_path, downloader=lambda: CSV, clock=lambda: NOW)['status'] == 'success'
    before = {p.name: p.read_bytes() for p in tmp_path.glob('*.json') if p.name != 'last-attempt.json'}
    result = refresh(tmp_path, downloader=lambda: response, force=True, clock=lambda: NOW)
    assert result['status'] == 'unavailable'
    assert {p.name: p.read_bytes() for p in tmp_path.glob('*.json') if p.name != 'last-attempt.json'} == before


def test_timeout_preserves_tax_and_cache(tmp_path):
    refresh(tmp_path, downloader=lambda: CSV, clock=lambda: NOW)
    cache = OrganizationCache(tmp_path / 'organizations.json', clock=lambda: NOW)
    cache.put('012345678', exists=None, approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))])
    cache.save()
    before = cache._path.read_bytes()
    def blocked():
        raise TimeoutError('secret response must not be logged')
    result = refresh(tmp_path, force=True, downloader=blocked, clock=lambda: NOW)
    assert result['status'] == 'unavailable' and 'secret' not in str(result)
    assert cache._path.read_bytes() == before
    updated = CSV + '999999999,עמותה אחרת\n'.encode()
    assert refresh(tmp_path, force=True, downloader=lambda: updated, clock=lambda: NOW)['updated']
    assert json.loads(cache._path.read_bytes())['organizations'] == json.loads(before)['organizations']


def test_lock_fail_closed(tmp_path):
    (tmp_path / '.operation.lock').mkdir()
    assert refresh(tmp_path, downloader=lambda: pytest.fail('network'))['status'] == 'unavailable'
    assert (tmp_path / '.operation.lock').is_dir()


@pytest.mark.parametrize('url', ['http://data.gov.il/a', 'https://evil.test/a',
    'https://data.gov.il.evil.test/a', 'https://user@data.gov.il/a',
    'https://data.gov.il:444/a', 'https://127.0.0.1/a'])
def test_download_url_restrictions(url):
    with pytest.raises(ValueError):
        official_url(url)


def test_scheduler_definition_is_persistent_and_independent():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'scheduling/task-scheduler.ps1').read_text()
    for fragment in ('DaysInterval>1', 'StartWhenAvailable>true', 'IgnoreNew', 'LeastPrivilege', 'Register-ScheduledTask', 'CreateNew'):
        assert fragment in text
    script = (root / 'scheduling/weekly-registry.ps1').read_text()
    assert '-m src.registry_sync' in script
    assert 'codex' not in script.lower().split('\n', 1)[1]


def test_shrink_and_partial_snapshot_recovery(tmp_path):
    full = CSV + '999999999,עמותה שנייה\n'.encode()
    first = refresh(tmp_path, downloader=lambda: full, clock=lambda: NOW)
    before = (tmp_path / 'organizations.json').read_bytes()
    assert refresh(tmp_path, force=True, downloader=lambda: CSV, clock=lambda: NOW)['status'] == 'unavailable'
    assert (tmp_path / 'organizations.json').read_bytes() == before
    snapshot = tmp_path / 'snapshots' / (first['file_sha256'] + '.csv')
    snapshot.write_bytes(b'partial')
    assert refresh(tmp_path, force=True, downloader=lambda: full, clock=lambda: NOW)['status'] == 'success'
    assert snapshot.read_bytes() == full


def test_manual_recovers_state_without_delaying_official(tmp_path):
    (tmp_path / 'sync-state.json').write_text('broken')
    csv = tmp_path / 'manual.csv'
    csv.write_bytes(CSV)
    assert refresh(tmp_path, manual_csv=csv, clock=lambda: NOW)['status'] == 'success'
    calls = []
    def download():
        calls.append(1)
        return CSV
    assert refresh(tmp_path, downloader=download, clock=lambda: NOW)['status'] == 'success'
    assert calls == [1]


def test_truncated_http_length_rejected(monkeypatch):
    from src.registry_sync import _get
    import io
    class Response(io.BytesIO):
        headers = {'Content-Length': '200'}
        def geturl(self):
            return 'https://data.gov.il/file.csv'
    class Opener:
        def open(self, *args, **kwargs):
            return Response(CSV)
    monkeypatch.setattr('src.registry_sync.build_opener', lambda *args: Opener())
    with pytest.raises(ValueError, match='Truncated'):
        _get('https://data.gov.il/file.csv', 1000)


def test_unchanged_accepted_quarantine_is_not_rejected_again(tmp_path):
    raw = CSV + (('bad,עמותה\n' * 12).encode())
    assert refresh(tmp_path, downloader=lambda: raw, clock=lambda: NOW, allow_large_change=True)['status'] == 'success'
    result = refresh(tmp_path, downloader=lambda: raw, clock=lambda: NOW + timedelta(days=7))
    assert result['status'] == 'success' and not result['updated']


def test_corrupt_cache_gets_persistent_diagnostic(tmp_path):
    (tmp_path / 'organizations.json').write_text('corrupt')
    result = refresh(tmp_path, downloader=lambda: pytest.fail('network'), clock=lambda: NOW)
    assert result['reason'] == 'cache_invalid'
    assert json.loads((tmp_path / 'last-attempt.json').read_bytes())['reason'] == 'cache_invalid'


def test_missing_registry_despite_recent_state_is_due(tmp_path):
    refresh(tmp_path, downloader=lambda: CSV, clock=lambda: NOW)
    (tmp_path / 'organizations.json').write_text('{"version":1,"organizations":{}}')
    assert refresh(tmp_path, downloader=lambda: CSV, clock=lambda: NOW)['status'] == 'success'


def test_http_metadata_size_redirect_and_deadline(monkeypatch):
    import io
    from src.registry_sync import _get, OfficialRedirects
    class Response(io.BytesIO):
        headers = {}
        def geturl(self):
            return 'https://data.gov.il/file.csv'
        def read(self, *args):
            pytest.fail('Buffered read is not deadline-safe')
        def read1(self, amount):
            return super().read(amount)
    class Opener:
        def open(self, *args, **kwargs):
            return Response(CSV)
    monkeypatch.setattr('src.registry_sync.build_opener', lambda *args: Opener())
    with pytest.raises(ValueError, match='metadata size'):
        _get('https://data.gov.il/file.csv', 1000, expected_size=999)
    with pytest.raises(ValueError):
        OfficialRedirects().redirect_request(None, None, 302, '', {}, 'https://evil.test/a')
    ticks = iter([0, 0, 61])
    monkeypatch.setattr('src.registry_sync.time.monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError):
        _get('https://data.gov.il/file.csv', 1000)


def test_junction_detection_for_cache_and_workbook_paths(tmp_path, monkeypatch):
    from src.organization_enrichment import _safe_path
    import os
    linked = tmp_path / 'pretend-junction'
    linked.mkdir()
    monkeypatch.setattr(os.path, 'isjunction', lambda value: Path(value) == linked, raising=False)
    with pytest.raises(ValueError):
        OrganizationCache(linked / 'cache.json')
    with pytest.raises(ValueError):
        _safe_path(linked / 'output.xlsx')


def test_published_state_fsync_failure_is_success_with_durability_limit(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError('Synthetic directory fsync failure')
    monkeypatch.setattr('src.private_runtime.fsync_directory', fail)
    result = refresh(tmp_path, downloader=lambda: CSV, clock=lambda: NOW)
    assert result['status'] == 'success' and not result['directory_durable']
    assert json.loads((tmp_path / 'sync-state.json').read_bytes())['last_official_success']
    assert refresh(tmp_path, downloader=lambda: pytest.fail('network'), clock=lambda: NOW)['status'] == 'not_due'


def test_diagnostic_failure_does_not_relabel_published_refresh(tmp_path, monkeypatch):
    import src.registry_sync as module
    original = module.atomic_json
    def write(path, data):
        if path.name == 'last-attempt.json':
            raise OSError('Synthetic diagnostic write failure')
        return original(path, data)
    monkeypatch.setattr(module, 'atomic_json', write)
    result = refresh(tmp_path, downloader=lambda: CSV, clock=lambda: NOW)
    assert result['status'] == 'success' and not result['diagnostic_saved']


def test_scheduler_xml_structure_and_escaped_paths():
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import escape
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scheduling/task-scheduler.ps1').read_text()
    template = script.split('$xml = @"\n', 1)[1].split('\n"@', 1)[0]
    values = {'$boundary': '2026-11-06T09:00:00',
              '$commandXml': escape(r'C:\Synthetic & Test\.venv\Scripts\python.exe'),
              '$argumentsXml': '-m src.registry_sync',
              '$workingXml': escape(r'C:\Synthetic & Test'), '$userIdXml': r'SYNTHETIC\User'}
    for variable, value in values.items():
        template = template.replace(variable, value)
    task = ET.fromstring(b'\xff\xfe' + template.encode('utf-16-le'))
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert task.findtext('t:Actions/t:Exec/t:Command', namespaces=ns) == r'C:\Synthetic & Test\.venv\Scripts\python.exe'
    assert task.findtext('t:Actions/t:Exec/t:Arguments', namespaces=ns) == '-m src.registry_sync'
    assert task.findtext('t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval', namespaces=ns) == '1'
    assert task.findtext('t:Settings/t:StartWhenAvailable', namespaces=ns) == 'true'
    assert task.findtext('t:Settings/t:MultipleInstancesPolicy', namespaces=ns) == 'IgnoreNew'
    assert task.findtext('t:Principals/t:Principal/t:RunLevel', namespaces=ns) == 'LeastPrivilege'
    assert 'GetPreamble()' in script


def test_weekly_calendar_gate_tolerates_scheduler_jitter(tmp_path):
    initial = NOW.replace(hour=9, second=30)
    assert refresh(tmp_path, downloader=lambda: CSV, clock=lambda: initial)['status'] == 'success'
    earlier_by_seconds = initial + timedelta(days=7, seconds=-30)
    assert refresh(tmp_path, downloader=lambda: CSV, clock=lambda: earlier_by_seconds)['status'] == 'success'


@pytest.mark.parametrize('size', [None, 0, -1, True, '12', 10**12])
def test_official_metadata_requires_positive_bounded_integer(monkeypatch, size):
    import src.registry_sync as sync
    calls = []
    def get(url, limit, **kwargs):
        calls.append(url)
        return json.dumps({'success': True, 'result': {'id': sync.RESOURCE,
            'format': 'csv', 'url': 'https://data.gov.il/test.csv', 'size': size}}).encode()
    monkeypatch.setattr(sync, '_get', get)
    with pytest.raises(ValueError, match='metadata size'):
        sync.download_registry()
    assert calls == [sync.METADATA_URL]


def test_failure_diagnostic_is_written_while_lock_held(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import src.registry_sync as sync
    held = []
    @contextmanager
    def lock(root):
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    original = sync.atomic_json
    def write(path, value):
        assert held, 'failure diagnostic escaped the operation lock'
        return original(path, value)
    monkeypatch.setattr(sync, 'operational_lock', lock)
    monkeypatch.setattr(sync, 'atomic_json', write)
    assert sync.refresh(tmp_path, downloader=lambda: b'bad', clock=lambda: NOW)['status'] == 'unavailable'
    assert (tmp_path / 'last-attempt.json').exists()


def test_download_enforces_metadata_size_with_fake_transport(monkeypatch):
    import src.registry_sync as sync
    from io import BytesIO
    class Response(BytesIO):
        headers = {}
        def geturl(self):
            return 'https://data.gov.il/test.csv'
    class Opener:
        def open(self, *args, **kwargs):
            return Response(b'truncated')
    monkeypatch.setattr(sync, 'build_opener', lambda *args: Opener())
    with pytest.raises(ValueError, match='metadata size mismatch'):
        sync._get('https://data.gov.il/test.csv', 1024, expected_size=100)
