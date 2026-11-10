"""Explicit recovery uses synthetic bytes only; no government CSV fixture."""
import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone

import pytest

from src import registry_csv
from src.organization_cache import OrganizationCache
from src.registry_csv import parse_csv

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def row(number, name='עמותה סינתטית', text='', newline='\n'):
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator=newline, quoting=csv.QUOTE_ALL).writerow(
        [number, name] + [''] * 20 + [text])
    return stream.getvalue()


def header(newline='\n'):
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator=newline).writerow(
        [registry_csv.ID, registry_csv.NAME] + [f'field{i}' for i in range(21)])
    return stream.getvalue()


def sample(*, newline='\n', codec='utf-8-sig', suffix=None, prefix=None, bad=None):
    headings = header(newline)
    prefix = row('012345678', text='valid\nmultiline "quoted", text', newline=newline) if prefix is None else prefix
    bad = ','.join(['333333333', 'עמותה פגומה'] + [''] * 20 + ['"broken']) + newline + 'still open' + newline if bad is None else bad
    suffix = row('987654321', text='next\nvalid multiline', newline=newline) if suffix is None else suffix
    first = len(list(io.StringIO(headings + prefix, newline=''))) + 1
    last = first + len(list(io.StringIO(bad, newline=''))) - 1
    raw = (headings + prefix + bad + suffix).encode(codec)
    return raw, first, last, bad.encode('utf-8' if codec == 'utf-8-sig' else codec)


@pytest.fixture
def authorize(monkeypatch):
    def apply(raw, first, last=None, columns=23):
        monkeypatch.setattr(registry_csv, 'APPROVED_RECOVERY',
                            (hashlib.sha256(raw).hexdigest(),
                             ((first, last),) if last is not None else tuple(first), columns))
    return apply


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('codec', ['utf-8-sig', 'cp1255'])
def test_recovery_exact_bytes_and_multiline(authorize, tmp_path, newline, codec):
    raw, first, last, payload = sample(newline=newline, codec=codec)
    authorize(raw, first, last)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    cache = OrganizationCache(path, clock=lambda: NOW)
    counts = cache.import_registry(source, recover_malformed=True)
    assert counts == {'updated': True, 'verified': 2, 'quarantined': 0, 'rows_read': 2,
                      'duplicate_rows': 0, 'duplicate_ids': 0, 'malformed_segments': 1,
                      'quarantined_physical_lines': 2, 'directory_durable': os.name != 'nt'}
    snapshot = json.loads(path.read_text())['registry']
    audit = snapshot['recovery'][0]
    assert (audit['start_line'], audit['end_line'], audit['resume_line']) == (first, last, last + 1)
    assert raw[audit['byte_start']:audit['byte_end']] == payload
    assert raw[:audit['byte_start']] + payload + raw[audit['byte_end']:] == raw
    assert (tmp_path / audit['artifact']).read_bytes() == payload
    assert audit['sha256'] == hashlib.sha256(payload).hexdigest()
    assert 'broken' not in path.read_text()
    assert source.read_bytes() == raw
    reopened = OrganizationCache(path, clock=lambda: NOW)
    assert reopened.official_identity('012345678')
    assert reopened.official_identity('987654321')
    assert reopened.official_identity('333333333') is None
    assert reopened.lookup_tax('987654321', NOW.date())['section46_status'] == 'unknown'
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert not reopened.import_registry(source, recover_malformed=True)['updated']
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_strict_default_and_sha_binding(authorize):
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    with pytest.raises(ValueError, match='Malformed registry CSV'):
        parse_csv(raw, imported_at=NOW)
    with pytest.raises(ValueError, match='SHA256'):
        parse_csv(raw + b'\n', imported_at=NOW, recover_malformed=True)


@pytest.mark.parametrize('bounds', [(1, 2), (5, 4), (999, 1000), (4, 999)])
def test_invalid_ranges_fail(authorize, bounds):
    raw, _, _, _ = sample()
    authorize(raw, *bounds)
    with pytest.raises(ValueError):
        parse_csv(raw, imported_at=NOW, recover_malformed=True)


def test_cannot_cut_valid_multiline_prefix(authorize):
    raw, first, last, _ = sample()
    authorize(raw, first - 1, last)
    with pytest.raises(ValueError, match='Malformed registry CSV'):
        parse_csv(raw, imported_at=NOW, recover_malformed=True)


def test_cannot_quarantine_valid_records(authorize):
    raw, first, last, _ = sample(bad=row('333333333'))
    authorize(raw, first, last)
    with pytest.raises(ValueError, match='not malformed'):
        parse_csv(raw, imported_at=NOW, recover_malformed=True)


@pytest.mark.parametrize('suffix', [
    row('987654321') + '"unclosed\n',
    row('987654321') + row('555555555') + 'bad,"unclosed',
    '',
    '987654321,עמותה\n',
    row('bad'),
    row('987654321', name='English'),
    row('987654321', name=''),
    '\n' + row('987654321'),
])
def test_additional_corruption_or_invalid_resume_fails(authorize, suffix):
    raw, first, last, _ = sample(suffix=suffix)
    authorize(raw, first, last)
    with pytest.raises(ValueError):
        parse_csv(raw, imported_at=NOW, recover_malformed=True)


def test_header_count_must_match_policy(authorize):
    raw, first, last, _ = sample()
    authorize(raw, first, last, columns=22)
    with pytest.raises(ValueError, match='header column count'):
        parse_csv(raw, imported_at=NOW, recover_malformed=True)


def test_duplicates_across_segments_and_reason_counts(authorize, tmp_path):
    raw, first, last, _ = sample(suffix=row('012345678') + row('987654321') + row('bad'))
    authorize(raw, first, last)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    result = OrganizationCache(path).import_registry(source, recover_malformed=True)
    assert result['rows_read'] == 4
    assert result['verified'] == 1
    assert result['quarantined'] == 3
    assert result['duplicate_rows'] == 2 and result['duplicate_ids'] == 1
    snapshot = json.loads(path.read_text())['registry']
    assert set(snapshot['records']) == {'987654321'}
    assert [x['reason'] for x in snapshot['quarantine']] == ['duplicate_id', 'duplicate_id', 'invalid_id']
    assert snapshot['quarantine'][1]['line'] == last + 1


@pytest.mark.parametrize('failure', ['parse', 'artifact', 'publish'])
def test_failure_preserves_cache_and_tax(authorize, tmp_path, monkeypatch, failure):
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_text(header() + row('111111111'))
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.import_registry(source)
    cache.put('111111111', exists=True)
    cache.save()
    before = path.read_bytes()
    raw, first, last, _ = sample(suffix=row('987654321') + '"unclosed' if failure == 'parse' else None)
    authorize(raw, first, last)
    source.write_bytes(raw)
    def fail(*args, **kwargs):
        raise OSError('simulated write failure')
    if failure == 'artifact':
        monkeypatch.setattr('src.organization_cache.os.link', fail)
    if failure == 'publish':
        monkeypatch.setattr('src.organization_cache.os.replace', fail)
    with pytest.raises((OSError, ValueError)):
        cache.import_registry(source, recover_malformed=True)
    assert path.read_bytes() == before
    assert cache.official_identity('111111111')
    assert cache.official_identity('987654321') is None
    assert not list(tmp_path.glob('.registry-quarantine-*'))


def test_success_preserves_tax_evidence(authorize, tmp_path):
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.put('111111111', exists=True)
    cache.save()
    tax = json.loads(path.read_text())['organizations']
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    source.write_bytes(raw)
    cache.import_registry(source, recover_malformed=True)
    assert json.loads(path.read_text())['organizations'] == tax


@pytest.mark.parametrize('kind', ['conflict', 'symlink'])
def test_artifact_no_clobber(authorize, tmp_path, kind):
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    name = parse_csv(raw, imported_at=NOW, recover_malformed=True)['recovery'][0]['artifact']
    artifact = tmp_path / name
    target = tmp_path / 'other.bin'
    target.write_bytes(b'preserve')
    if kind == 'symlink':
        artifact.symlink_to(target)
    else:
        artifact.write_bytes(b'preserve')
    with pytest.raises(ValueError):
        OrganizationCache(path).import_registry(source, recover_malformed=True)
    assert artifact.read_bytes() == b'preserve'
    assert not path.exists()


@pytest.mark.parametrize('field,value', [('byte_end', 0), ('artifact', '../escape'),
                                        ('start_line', True), ('sha256', 'z' * 64)])
def test_invalid_recovery_audit_rejected(authorize, field, value):
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    snapshot = parse_csv(raw, imported_at=NOW, recover_malformed=True)
    snapshot['recovery'][0][field] = value
    with pytest.raises(ValueError, match='recovery audit'):
        OrganizationCache._validate_registry(snapshot)


def test_automatic_refresh_accepts_only_approved_recovery(authorize, tmp_path):
    from src.registry_sync import refresh
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    result = refresh(tmp_path, downloader=lambda: raw, clock=lambda: NOW)
    assert result['status'] == 'success'
    before = (tmp_path / 'organizations.json').read_bytes()
    rejected = refresh(tmp_path, downloader=lambda: raw + b'\n', force=True, clock=lambda: NOW)
    assert rejected['status'] == 'unavailable'
    assert rejected['reason'] == 'csv_invalid_or_abnormal_change'
    assert (tmp_path / 'organizations.json').read_bytes() == before


def test_cli_requires_opt_in(authorize, tmp_path, capsys):
    from src.registry_import import main
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    arguments = ['--csv', str(source), '--cache', str(path)]
    with pytest.raises(ValueError):
        main(arguments)
    main(arguments + ['--recover-known-malformed-record'])
    assert json.loads(capsys.readouterr().out)['malformed_segments'] == 1


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_reopen_requires_preserved_artifact(authorize, tmp_path, damage):
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    OrganizationCache(path).import_registry(source, recover_malformed=True)
    artifact = tmp_path / json.loads(path.read_text())['registry']['recovery'][0]['artifact']
    if damage == 'missing':
        artifact.unlink()
    else:
        artifact.write_bytes(b'altered')
    with pytest.raises((ValueError, OSError)):
        OrganizationCache(path)


def test_valid_multiline_that_looks_like_record_is_not_split():
    fake_record = row('987654321').strip()
    raw = (header() + row('012345678', text='text\n' + fake_record + '\nmore')).encode()
    snapshot = parse_csv(raw, imported_at=NOW)
    assert set(snapshot['records']) == {'012345678'}
    assert snapshot['quarantine'] == []


def test_artifact_cannot_be_cache(authorize, tmp_path):
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    source = tmp_path / 'input.csv'
    source.write_bytes(raw)
    name = parse_csv(raw, imported_at=NOW, recover_malformed=True)['recovery'][0]['artifact']
    path = tmp_path / name
    with pytest.raises(ValueError, match='differ from cache'):
        OrganizationCache(path).import_registry(source, recover_malformed=True)
    assert not path.exists()


def multiple_sample(*, newline='\n', codec='utf-8-sig', numbers=None, extra=''):
    numbers = numbers or ['012345678', '222222222', '444444444', '987654321']
    parts = [header(newline), row(numbers[0], text='valid\nmultiline "quoted", field', newline=newline)]
    spans, payloads = [], []
    for index, length in enumerate([2, 1, 3]):
        first = len(list(io.StringIO(''.join(parts), newline=''))) + 1
        bad = ','.join(['333333333', 'עמותה פגומה'] + [''] * 20 + ['"broken']) + newline
        bad += ('still open' + newline) * (length - 1)
        spans.append((first, first + length - 1))
        payloads.append(bad.encode('utf-8' if codec == 'utf-8-sig' else codec))
        parts.extend([bad, row(numbers[index + 1], text='valid\nmultiline', newline=newline)])
    return (''.join(parts) + extra).encode(codec), spans, payloads


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('codec', ['utf-8-sig', 'cp1255'])
def test_multiple_spans_exact_coverage_and_reopen(authorize, tmp_path, newline, codec):
    raw, spans, payloads = multiple_sample(newline=newline, codec=codec)
    authorize(raw, spans)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    result = OrganizationCache(path, clock=lambda: NOW).import_registry(source, recover_malformed=True)
    assert result['verified'] == result['rows_read'] == 4
    assert result['quarantined'] == result['duplicate_rows'] == result['duplicate_ids'] == 0
    assert result['malformed_segments'] == 3
    assert result['quarantined_physical_lines'] == 6
    snapshot = json.loads(path.read_text())['registry']
    position, coverage = 0, []
    for audit, (first, last), payload in zip(snapshot['recovery'], spans, payloads):
        assert (audit['start_line'], audit['end_line'], audit['resume_line']) == (first, last, last + 1)
        assert raw[audit['byte_start']:audit['byte_end']] == payload
        assert (tmp_path / audit['artifact']).read_bytes() == payload
        assert hashlib.sha256(payload).hexdigest() == audit['sha256']
        coverage.extend([raw[position:audit['byte_start']], payload])
        position = audit['byte_end']
    coverage.append(raw[position:])
    assert b''.join(coverage) == raw
    cache = OrganizationCache(path, clock=lambda: NOW)
    assert cache.official_identity('987654321')
    assert cache.official_identity('333333333') is None
    assert source.read_bytes() == raw
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert not cache.import_registry(source, recover_malformed=True)['updated']
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize('mode', ['overlap', 'unordered', 'repeated', 'empty', 'bad_middle_start',
                                  'bad_middle_end', 'bad_last_resume'])
def test_multiple_bad_boundaries_fail(authorize, mode):
    raw, spans, _ = multiple_sample()
    if mode == 'overlap':
        spans[1] = (spans[0][1], spans[1][1])
    elif mode == 'unordered':
        spans.reverse()
    elif mode == 'repeated':
        spans.insert(1, spans[0])
    elif mode == 'empty':
        spans = []
    elif mode == 'bad_middle_start':
        spans[1] = (spans[1][0] - 1, spans[1][1])
    elif mode == 'bad_middle_end':
        spans[1] = (spans[1][0], spans[1][1] + 1)
    else:
        spans[2] = (spans[2][0], spans[2][1] + 1)
    authorize(raw, spans)
    with pytest.raises(ValueError):
        parse_csv(raw, imported_at=NOW, recover_malformed=True)


def test_multiple_spans_require_sha_and_explicit_opt_in(authorize):
    raw, spans, _ = multiple_sample()
    authorize(raw, spans)
    with pytest.raises(ValueError, match='Malformed registry CSV'):
        parse_csv(raw, imported_at=NOW)
    with pytest.raises(ValueError, match='SHA256'):
        parse_csv(raw + b'\n', imported_at=NOW, recover_malformed=True)


def test_duplicates_across_all_segments(authorize, tmp_path):
    raw, spans, _ = multiple_sample(numbers=['012345678', '222222222', '012345678', '222222222'],
                                     extra=row('987654321'))
    authorize(raw, spans)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    result = OrganizationCache(path).import_registry(source, recover_malformed=True)
    assert result['rows_read'] == 5
    assert result['verified'] == 1
    assert result['quarantined'] == result['duplicate_rows'] == 4
    assert result['duplicate_ids'] == 2
    assert set(json.loads(path.read_text())['registry']['records']) == {'987654321'}


@pytest.mark.parametrize('failure', ['late_parse', 'second_artifact', 'cache_publish'])
def test_multiple_span_failure_preserves_old_cache(authorize, tmp_path, monkeypatch, failure):
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_text(header() + row('111111111'))
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.import_registry(source)
    cache.put('111111111', exists=True)
    cache.save()
    before = path.read_bytes()
    raw, spans, _ = multiple_sample(extra='"unclosed' if failure == 'late_parse' else '')
    authorize(raw, spans)
    source.write_bytes(raw)
    if failure == 'second_artifact':
        real_link = os.link
        calls = []
        def link(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('second artifact failure')
            return real_link(*args)
        monkeypatch.setattr('src.organization_cache.os.link', link)
    elif failure == 'cache_publish':
        def fail(*args):
            raise OSError('cache publication failure')
        monkeypatch.setattr('src.organization_cache.os.replace', fail)
    with pytest.raises((ValueError, OSError)):
        cache.import_registry(source, recover_malformed=True)
    assert path.read_bytes() == before
    assert OrganizationCache(path).official_identity('111111111')
    assert cache.official_identity('111111111')
    assert cache.official_identity('987654321') is None
    assert not list(tmp_path.glob('.registry-quarantine-*'))


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_each_multispan_artifact_required(authorize, tmp_path, damage):
    raw, spans, _ = multiple_sample()
    authorize(raw, spans)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    OrganizationCache(path).import_registry(source, recover_malformed=True)
    audit = json.loads(path.read_text())['registry']['recovery'][1]
    artifact = tmp_path / audit['artifact']
    if damage == 'missing':
        artifact.unlink()
    else:
        artifact.write_bytes(b'changed')
    with pytest.raises((ValueError, OSError)):
        OrganizationCache(path)


@pytest.mark.parametrize('damage', ['omit', 'reorder', 'overlap'])
def test_multispan_audit_requires_ordered_complete_collection(authorize, damage):
    raw, spans, _ = multiple_sample()
    authorize(raw, spans)
    snapshot = parse_csv(raw, imported_at=NOW, recover_malformed=True)
    if damage == 'omit':
        snapshot['recovery'].pop()
    elif damage == 'reorder':
        snapshot['recovery'].reverse()
    else:
        snapshot['recovery'][1]['byte_start'] = snapshot['recovery'][0]['byte_end'] - 1
    with pytest.raises(ValueError, match='recovery audit'):
        OrganizationCache._validate_registry(snapshot)


def test_legacy_single_span_cache_still_opens(authorize, tmp_path):
    raw, first, last, _ = sample()
    authorize(raw, first, last)
    source, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    source.write_bytes(raw)
    OrganizationCache(path).import_registry(source, recover_malformed=True)
    document = json.loads(path.read_text())
    document['registry']['recovery'] = document['registry']['recovery'][0]
    path.write_text(json.dumps(document))
    # A policy extended by later approvals still accepts the previous audit format.
    authorize(raw, [(first, last), (last + 10, last + 12)])
    before = path.read_bytes()
    assert OrganizationCache(path).official_identity('987654321')
    assert path.read_bytes() == before


def test_multiple_spans_weekly_refresh_recovers_exact_approved_file(authorize, tmp_path):
    from src.registry_sync import refresh
    raw, spans, _ = multiple_sample()
    authorize(raw, spans)
    result = refresh(tmp_path, downloader=lambda: raw, clock=lambda: NOW)
    assert result['status'] == 'success'
    assert result['malformed_segments'] == 3
    assert OrganizationCache(tmp_path / 'organizations.json').official_identity('987654321')
    before = (tmp_path / 'organizations.json').read_bytes()
    assert refresh(tmp_path, downloader=lambda: raw + b'\n', force=True, clock=lambda: NOW)['status'] == 'unavailable'
    assert (tmp_path / 'organizations.json').read_bytes() == before
