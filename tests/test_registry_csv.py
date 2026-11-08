from datetime import datetime, timezone, date
import csv
import hashlib
import io
import json
import openpyxl
import pytest
from src.registry_csv import parse_csv
from src.organization_cache import OrganizationCache
from src.organization_enrichment import enrich_workbook

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
TEXT = 'מספר עמותה,שם עמותה בעברית\n012345678,עמותת בדיקה\n'

@pytest.mark.parametrize('codec', ['utf-8-sig', 'cp1255'])
def test_encoding(codec):
    raw = TEXT.encode(codec)
    result = parse_csv(raw, imported_at=NOW)
    assert result['records'] == {'012345678': 'עמותת בדיקה'}
    assert result['file_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['encoding'] == codec


def test_quarantine():
    raw = (TEXT + '123456789,עמותה אחרת\n123456789,עמותה אחרת\n'
           'bad,עמותה\n222222222,\n333333333,Name\n'
           '444444444,עמותה,extra\n').encode()
    result = parse_csv(raw, imported_at=NOW)
    assert result['records'] == {'012345678': 'עמותת בדיקה'}
    assert len(result['quarantine']) == 6
    assert sum(q['reason'] == 'duplicate_id' for q in result['quarantine']) == 2


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_multiline_quarantine_end_lines_and_auditable_start_lines(newline):
    # Empty physical rows are records; blank lines inside quoted fields are not.
    text = ('מספר עמותה,שם עמותה בעברית,הערה\n'
            '012345678,עמותה סינתטית,"ראשונה\n\nסוף"\n'
            '\n'
            '012345678,עמותה סינתטית,"שנייה\nסוף"\n'
            '\n'
            '222222222,English,"שלישית\n\nסוף"\n'
            '999999999,עמותה תקינה,אחרונה\n'
            '   \n').replace('\n', newline)
    raw = text.encode('utf-8-sig')
    result = parse_csv(raw, imported_at=NOW)
    assert result['records'] == {'999999999': 'עמותה תקינה'}
    assert [(q['line'], q['reason']) for q in result['quarantine']] == [
        (5, 'column_count'), (4, 'duplicate_id'), (7, 'duplicate_id'),
        (8, 'column_count'), (11, 'invalid_name'), (13, 'column_count')]

    # Replay the exact hashed source with the same standard-library reader.
    # No schema field, quote repair or record-boundary guessing is needed.
    assert result['file_sha256'] == hashlib.sha256(raw).hexdigest()
    physical = list(io.StringIO(raw.decode(result['encoding']), newline=''))
    reader = csv.reader(iter(physical), strict=True)
    next(reader)  # Consume the complete header, even if it is multiline.
    previous_end = reader.line_num
    spans = {}
    for row in reader:
        end = reader.line_num
        start = previous_end + 1
        spans[end] = (start, end, row)
        previous_end = end
    assert {end: (start, stop) for end, (start, stop, _) in spans.items()} == {
        4: (2, 4), 5: (5, 5), 7: (6, 7), 8: (8, 8),
        11: (9, 11), 12: (12, 12), 13: (13, 13)}
    for rejected in result['quarantine']:
        start, end, row = spans[rejected['line']]
        identity = (row[0].strip() if row else '', row[1].strip() if len(row) > 1 else '')
        assert identity == (rejected['registration_number'], rejected['official_name'])
        exact_record = ''.join(physical[start - 1:end])
        assert list(csv.reader(io.StringIO(exact_record, newline=''), strict=True)) == [row]

@pytest.mark.parametrize('raw', [b'<html>blocked</html>', b'a,b\n1,2', b'',
    'מספר עמותה,שם עמותה בעברית,מספר עמותה\n'.encode()])
def test_bad_files(raw):
    with pytest.raises(ValueError):
        parse_csv(raw, imported_at=NOW)


def test_cache_repeat_tax_and_reopen(tmp_path):
    path = tmp_path / 'cache.json'
    csv = tmp_path / 'input.csv'
    csv.write_bytes(TEXT.encode())
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.put('012345678', exists=True,
              approval_periods=[(date(2026, 1, 1), date(2026, 12, 31))])
    cache.save()
    tax = json.loads(path.read_text())['organizations']
    assert cache.import_registry(csv)['updated']
    first = path.read_bytes(), path.stat().st_mtime_ns
    assert not cache.import_registry(csv)['updated']
    assert (path.read_bytes(), path.stat().st_mtime_ns) == first
    cache = OrganizationCache(path, clock=lambda: NOW)
    assert cache.official_identity('012345678')['official_name'] == 'עמותת בדיקה'
    assert json.loads(path.read_text())['organizations'] == tax
    assert cache.lookup('012345678', date(2026, 6, 1))['section46_status'] == 'approved'
    csv.write_bytes('מספר עמותה,שם עמותה בעברית\n999999999,שם חדש\n'.encode())
    cache.import_registry(csv)
    assert cache.official_identity('012345678') is None
    assert json.loads(path.read_text())['organizations'] == tax


def test_atomic_failure(tmp_path, monkeypatch):
    path, csv = tmp_path / 'cache.json', tmp_path / 'input.csv'
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.save()
    before = path.read_bytes()
    csv.write_bytes(TEXT.encode())
    def fail(*args):
        raise OSError('simulated disk failure')
    monkeypatch.setattr('src.organization_cache.os.replace', fail)
    with pytest.raises(OSError):
        cache.import_registry(csv)
    assert path.read_bytes() == before
    assert cache.official_identity('012345678') is None
    assert not list(tmp_path.glob('.organization-cache-*'))


def test_excel_exact_id_audit_no_clobber(tmp_path):
    path, csv = tmp_path / 'cache.json', tmp_path / 'input.csv'
    csv.write_bytes(TEXT.encode())
    cache = OrganizationCache(path)
    cache.import_registry(csv)
    source, output = tmp_path / 'original.xlsx', tmp_path / 'new.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['מספר עמותה', 'תאריך', 'שם עמותה'])
    ws.append(['012345678', '2026-01-01', '=original'])
    ws.append(['999999999', '2026-01-01', 'ללא התאמה'])
    wb.save(source)
    before = source.read_bytes()
    result = enrich_workbook(source, path, output)
    assert result['official_names'] == 1
    assert source.read_bytes() == before
    wb = openpyxl.load_workbook(output)
    assert wb.active.cell(2, 3).value == 'עמותת בדיקה'
    assert wb.active.cell(3, 3).value == 'ללא התאמה'
    audit = wb['Registry identity audit']
    assert audit.cell(2, 4).value == '=original'
    assert audit.cell(2, 4).data_type == 's'
    assert audit.cell(2, 9).value == hashlib.sha256(csv.read_bytes()).hexdigest()
    assert wb.active.cell(2, 5).value == 'unknown'
    wb.close()
    with pytest.raises(ValueError):
        enrich_workbook(source, path, output)




def test_tax_writer_retains_registry_imported_after_construction(tmp_path):
    path, csv = tmp_path / 'cache.json', tmp_path / 'input.csv'
    old_writer = OrganizationCache(path, clock=lambda: NOW)
    csv.write_bytes(TEXT.encode())
    OrganizationCache(path, clock=lambda: NOW).import_registry(csv)
    old_writer.put('012345678', exists=None,
                   denial_periods=[(date(2026, 1, 1), date(2026, 12, 31))])
    old_writer.save()
    reopened = OrganizationCache(path, clock=lambda: NOW)
    assert reopened.official_identity('012345678')
    assert reopened.lookup('012345678', date(2026, 2, 1))['section46_status'] == 'denied'


def test_reimport_after_cache_deleted(tmp_path):
    csv, path = tmp_path / 'input.csv', tmp_path / 'cache.json'
    csv.write_bytes(TEXT.encode())
    cache = OrganizationCache(path)
    cache.import_registry(csv)
    path.unlink()
    assert cache.import_registry(csv)['updated']
    assert OrganizationCache(path).official_identity('012345678')
