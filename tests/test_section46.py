from datetime import date, datetime, timedelta, timezone
import copy
import json
import pytest
from src.organization_cache import OrganizationCache
from src.section46 import Section46Verifier, normalize_evidence

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
NUMBER = '012345678'
TAX = '987654321'


def evidence(**changes):
    value = {'registration_number': NUMBER, 'tax_file_number': TAX,
        'source_url': 'https://www.gov.il/he/service/confirmation-of-donations',
        'retrieved_at': NOW.isoformat(), 'evidence_sha256': 'a' * 64,
        'approval_periods': [['2020-01-01', '2020-12-31']], 'denial_periods': [],
        'verification': 'operator_attested_official_intervals'}
    return value | changes


def test_cache_first_and_historical_boundaries(tmp_path):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.store_tax_evidence(evidence())
    def no_network(*args):
        pytest.fail('Fresh verified cache must prevent network')
    verifier = Section46Verifier(cache, transport=no_network, clock=lambda: NOW)
    for day in (date(2020, 1, 1), date(2020, 12, 31)):
        result = verifier.verify(NUMBER, day)
        assert result['section46_status'] == 'approved' and result['origin'] == 'cache'
    for day in (date(2019, 12, 31), date(2021, 1, 1)):
        assert verifier.verify(NUMBER, day)['section46_status'] == 'unknown'
    assert OrganizationCache(cache._path, clock=lambda: NOW).lookup_tax(NUMBER, date(2020, 6, 1))['section46_status'] == 'approved'


def test_missing_offline_and_incomplete_never_approve(tmp_path):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.put(NUMBER, exists=True, approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))])
    assert Section46Verifier(cache).verify(NUMBER, date(2020, 2, 1))['section46_status'] == 'unknown'
    for payload in ({'approved': True}, evidence(approval_periods=[], denial_periods=[]),
                    evidence(registration_number='999999999')):
        verifier = Section46Verifier(cache, transport=lambda *args: payload, clock=lambda: NOW)
        assert verifier.verify(NUMBER, date(2020, 2, 1), tax_file_number=TAX)['section46_status'] == 'unknown'


def test_stale_evidence_queries_only_with_tax_binding(tmp_path):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW, tax_ttl=timedelta(days=1))
    cache.store_tax_evidence(evidence(retrieved_at=(NOW-timedelta(days=2)).isoformat()))
    calls = []
    def transport(tax, donated):
        calls.append((tax, donated))
        return evidence(verification='verified_normalized_transport_intervals')
    verifier = Section46Verifier(cache, transport=transport, clock=lambda: NOW)
    assert verifier.verify(NUMBER, date(2020, 2, 1))['section46_status'] == 'unknown'
    assert not calls
    assert verifier.verify(NUMBER, date(2020, 2, 1), tax_file_number=TAX)['section46_status'] == 'approved'
    assert calls == [(TAX, date(2020, 2, 1))]


def test_conflicts_unknown_and_no_network(tmp_path):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.store_tax_evidence(evidence())
    cache.store_tax_evidence(evidence(approval_periods=[], denial_periods=[['2020-01-01', '2020-12-31']], evidence_sha256='b'*64))
    result = Section46Verifier(cache, transport=lambda *args: pytest.fail('conflict')).verify(NUMBER, date(2020, 3, 1), tax_file_number=TAX)
    assert result['conflict'] and result['section46_status'] == 'unknown'


@pytest.mark.parametrize('changes', [dict(verification='current_only'), dict(source_url='https://evil.test/a'),
    dict(approval_periods=[['2020-12-31','2020-01-01']]), dict(evidence_sha256='bad'),
    dict(denial_periods=[['2020-01-01','2020-12-31']]), dict(retrieved_at='2026-11-05')])
def test_bad_evidence_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        normalize_evidence(evidence(**changes), registration_number=NUMBER, tax_file_number=TAX, now=NOW)


def test_registry_and_tax_survive_other_writers(tmp_path):
    path, csv = tmp_path / 'cache.json', tmp_path / 'input.csv'
    csv.write_bytes('מספר עמותה,שם עמותה בעברית\n012345678,עמותה סינתטית\n'.encode())
    stale = OrganizationCache(path, clock=lambda: NOW)
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.store_tax_evidence(evidence())
    before = json.loads(path.read_bytes())['tax_evidence']
    cache.import_registry(csv)
    stale.put(NUMBER, exists=True)
    stale.save()
    document = json.loads(path.read_bytes())
    assert document['tax_evidence'] == before
    assert document['registry']['records'][NUMBER] == 'עמותה סינתטית'


def test_store_failure_atomic(tmp_path, monkeypatch):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.save()
    before = cache._path.read_bytes()
    def fail(*args):
        raise OSError('disk')
    monkeypatch.setattr('src.organization_cache.os.replace', fail)
    with pytest.raises(OSError):
        cache.store_tax_evidence(evidence())
    assert cache._path.read_bytes() == before
    assert cache.lookup_tax(NUMBER, date(2020, 1, 1))['section46_status'] == 'unknown'


def test_explicit_binding_mismatch_and_multiple_bindings(tmp_path):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.store_tax_evidence(evidence())
    assert Section46Verifier(cache).verify(NUMBER, date(2020, 2, 1), tax_file_number='111111111')['section46_status'] == 'unknown'
    cache.store_tax_evidence(evidence(tax_file_number='111111111'))
    result = cache.lookup_tax(NUMBER, date(2020, 2, 1))
    assert result['conflict'] and result['section46_status'] == 'unknown'


def test_pending_edits_rejected_and_stale_writers_merge(tmp_path):
    path = tmp_path / 'cache.json'
    a, b = (OrganizationCache(path, clock=lambda: NOW) for _ in range(2))
    a.put(NUMBER, exists=True)
    with pytest.raises(ValueError):
        a.store_tax_evidence(evidence())
    a.save()
    b.put('999999999', exists=None)
    b.save()
    assert set(json.loads(path.read_bytes())['organizations']) == {NUMBER, '999999999'}


@pytest.mark.parametrize('payload', [evidence(source_url=5), evidence(retrieved_at=7)])
def test_malformed_transport_is_unknown(tmp_path, payload):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    result = Section46Verifier(cache, transport=lambda *args: payload, clock=lambda: NOW).verify(NUMBER, date(2020, 2, 1), tax_file_number=TAX)
    assert result['section46_status'] == 'unknown'


def test_legacy_periods_not_used_by_new_workbook(tmp_path):
    import openpyxl
    from src.organization_enrichment import enrich_workbook
    path = tmp_path / 'cache.json'
    cache = OrganizationCache(path)
    cache.put(NUMBER, exists=True, approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))])
    cache.save()
    source, output = tmp_path / 'input.xlsx', tmp_path / 'new.xlsx'
    wb = openpyxl.Workbook()
    wb.active.append(['מספר עמותה', 'תאריך'])
    wb.active.append([NUMBER, '2020-01-01'])
    wb.save(source)
    wb.close()
    counts = enrich_workbook(source, path, output)
    assert counts['unknown'] == 1 and counts.get('approved', 0) == 0
    wb = openpyxl.load_workbook(output)
    assert 'legacy periods retained but not used' in wb.active.cell(2, 6).value
    wb.close()


def test_identity_and_tax_freshness_independent(tmp_path):
    from src.organization_enrichment import _enrichment
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW, tax_ttl=timedelta(days=1))
    cache.put(NUMBER, exists=True)
    cache.save()
    cache.store_tax_evidence(evidence(retrieved_at=(NOW - timedelta(days=2)).isoformat()))
    result = _enrichment(NUMBER, date(2020, 1, 1), cache)
    assert result[0] == 'exists' and result[1] == 'unknown' and result[2] == 'stale'


def test_cross_process_lock_survives_different_temp_and_abrupt_exit(tmp_path):
    import os
    import subprocess
    import sys
    import time
    path = tmp_path / 'cache.json'
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.put(NUMBER, exists=True)
    cache.save()
    ready = tmp_path / 'ready'
    other_temp = tmp_path / 'other-temp'
    other_temp.mkdir()
    code = ('from pathlib import Path; import os,sys; '
            'from src.private_runtime import file_lock; '
            'root=Path(sys.argv[1]); '
            'lock=file_lock(root/".organization-cache.lock"); lock.__enter__(); '
            '(root/"ready").write_text("ready"); sys.stdin.read(1); os._exit(0)')
    env = dict(os.environ, TMPDIR=str(other_temp), TMP=str(other_temp), TEMP=str(other_temp))
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        cache.put('999999999', exists=None)
        with pytest.raises(OSError):
            cache.save()
        child.communicate(input=b'x', timeout=5)
        cache.save()  # OS released the stable sidecar even without __exit__.
        assert '999999999' in json.loads(path.read_bytes())['organizations']
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_reverse_tax_binding_conflicts_unknown(tmp_path):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.store_tax_evidence(evidence())
    cache.store_tax_evidence(evidence(registration_number='111111111'))
    for number in (NUMBER, '111111111'):
        result = cache.lookup_tax(number, date(2020, 1, 1))
        assert result['conflict'] and result['section46_status'] == 'unknown'


def test_windows_lock_contention_is_portably_busy():
    import errno
    from src.private_runtime import windows_lock
    class Backend:
        LK_NBLCK = 1
        def locking(self, *args):
            raise PermissionError(errno.EACCES, 'Synthetic busy')
    class Stream:
        def fileno(self):
            return 1
    with pytest.raises(BlockingIOError):
        windows_lock(Stream(), Backend())


def test_document_hash_mismatch_does_not_publish_evidence(tmp_path, capsys):
    from src.section46_cli import main
    manifest = tmp_path / 'evidence.json'
    manifest.write_text(json.dumps(evidence()))
    document = tmp_path / 'synthetic-document.txt'
    document.write_text('Synthetic document only')
    code = main(['--root', str(tmp_path / 'runtime'), '--registration-number', NUMBER,
                 '--donation-date', '2020-01-01', '--evidence', str(manifest),
                 '--document', str(document)])
    assert code == 1 and not (tmp_path / 'runtime/organizations.json').exists()


def test_provider_origin_survives_cache_reopen_and_workbook(tmp_path):
    import openpyxl
    from src.organization_enrichment import enrich_workbook
    path = tmp_path / 'cache.json'
    cache = OrganizationCache(path, clock=lambda: NOW)
    payload = evidence(verification='verified_normalized_transport_intervals')
    result = Section46Verifier(cache, transport=lambda *a: payload, clock=lambda: NOW).verify(NUMBER, date(2020, 2, 1), tax_file_number=TAX)
    assert result['section46_status'] == 'approved'
    reopened = OrganizationCache(path, clock=lambda: NOW)
    assert reopened.lookup_tax(NUMBER, date(2020, 2, 1))['provenance'][0]['verification'] == 'verified_normalized_transport_intervals'
    source, output = tmp_path / 'input.xlsx', tmp_path / 'output.xlsx'
    wb = openpyxl.Workbook()
    wb.active.append(['מספר עמותה', 'תאריך'])
    wb.active.append([NUMBER, '2020-02-01'])
    wb.save(source)
    wb.close()
    # Use real retrieval time for the workbook's real clock.
    current = dict(payload, retrieved_at=datetime.now(timezone.utc).isoformat())
    OrganizationCache(path).store_tax_evidence(current)
    assert enrich_workbook(source, path, output)['approved'] == 1
    wb = openpyxl.load_workbook(output)
    note = wb.active.cell(2, 6).value
    assert 'verified_normalized_transport_intervals' in note
    assert 'operator-attested' not in note
    wb.close()


def test_directory_fsync_failure_does_not_rollback_published_evidence(tmp_path, monkeypatch):
    cache = OrganizationCache(tmp_path / 'cache.json', clock=lambda: NOW)
    cache.save()
    def fail(*args):
        raise OSError('Synthetic directory fsync failure')
    monkeypatch.setattr('src.private_runtime.fsync_directory', fail)
    assert cache.store_tax_evidence(evidence())
    assert not cache.last_save_durable
    assert cache.lookup_tax(NUMBER, date(2020, 1, 1))['section46_status'] == 'approved'
    assert json.loads(cache._path.read_bytes())['tax_evidence'][NUMBER]


def test_unrepresentable_utc_timestamp_never_corrupts_cache(tmp_path):
    path = tmp_path / 'cache.json'
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.save()
    before = path.read_bytes()
    payload = evidence(retrieved_at='0001-01-01T00:00:00+05:00')
    with pytest.raises(ValueError):
        cache.store_tax_evidence(payload)
    assert path.read_bytes() == before
    payload['verification'] = 'verified_normalized_transport_intervals'
    result = Section46Verifier(cache, transport=lambda *a: payload, clock=lambda: NOW).verify(NUMBER, date(2020,1,1), tax_file_number=TAX)
    assert result['section46_status'] == 'unknown' and path.read_bytes() == before
    assert OrganizationCache(path, clock=lambda: NOW).lookup_tax(NUMBER, date(2020,1,1))['section46_status'] == 'unknown'


@pytest.mark.parametrize('url', [
    'https://evil.test\\.gov.il/x', 'https://evil.test%2f.gov.il/',
    'https://@www.gov.il/', 'https://:@www.gov.il/',
    'https://www.gov.il/?', 'https://www.gov.il/#',
    'https://www.\ngov.il/', 'https://www.gov.il/\tpath',
    'https://bad_label.gov.il/', 'https://-bad.gov.il/',
])
def test_noncanonical_official_evidence_urls_rejected(url):
    with pytest.raises(ValueError):
        normalize_evidence(evidence(source_url=url), registration_number=NUMBER,
                           tax_file_number=TAX, now=NOW)
