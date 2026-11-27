from contextlib import closing
import hashlib
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
import pytest

from src.migrate_users import LinkMappingError, main, migrate


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / 'snapshot'
    (source / 'accounts/primary').mkdir(parents=True)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    new = tmp_path / 'qa3'
    receipt = source / 'receipts/primary/2026/01_January/קבלה # literal%20.pdf'
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b'%PDF-1.4 synthetic')
    workbook = source / 'reports/primary/2026/donation_summary_2026.xlsx'
    workbook.parent.mkdir(parents=True)
    return source, runtime, new, receipt, workbook


def book(path, link, formula=None):
    with closing(Workbook()) as wb:
        wb.active['A1'].hyperlink = link
        wb.active['A2'] = formula
        wb.save(path)


def migration(snapshot, **options):
    source, runtime, new, *_ = snapshot
    return migrate(source, runtime, new, 'alpha', 'primary', 'alpha@example.test', **options)


def test_plan_reports_first_unmapped_link_without_creating_target(snapshot, tmp_path, monkeypatch, capsys):
    source, runtime, new, receipt, workbook = snapshot
    target = str(tmp_path / 'historical' / receipt.relative_to(source))
    book(workbook, target)
    monkeypatch.setattr('sys.argv', ['migrate', '--source', str(source), '--legacy-runtime', str(runtime),
        '--destination', str(new), '--user', 'alpha', '--account', 'primary', '--mailbox', 'alpha@example.test'])
    assert main() == 1
    err = capsys.readouterr().err
    assert 'pdf_outside_mapped_receipt_roots' in err and '|rId1;' in err
    assert target not in err
    monkeypatch.setattr('sys.argv', [*__import__('sys').argv, '--show-link'])
    assert main() == 1
    assert 'Local link target:' in capsys.readouterr().err
    assert not new.exists()


def test_multiple_roots_uri_native_relative_and_manifest_share_verified_plan(snapshot, tmp_path):
    source, runtime, new, receipt, workbook = snapshot
    first, second = tmp_path / 'production', tmp_path / 'historical'
    relative = receipt.relative_to(source)
    uri = (first / relative).as_uri()
    native = str(second / relative)
    book(workbook, uri, f'=HYPERLINK("{native}","source")')
    manifest = source / 'data/processed_messages.json'
    manifest.parent.mkdir()
    manifest.write_text(json.dumps([{'saved_files': [native]}]))
    before = workbook.read_bytes()
    assert migration(snapshot, legacy_link_root=[first, second]) == 3
    assert not new.exists()
    assert migration(snapshot, apply=True, legacy_link_root=[first, second]) == 3
    output = new / 'reports/alpha/primary/2026/donation_summary_2026.xlsx'
    copied = new / 'receipts/alpha' / relative.relative_to('receipts')
    with closing(load_workbook(output)) as wb:
        assert wb.active['A1'].hyperlink.target == str(copied)
        assert wb.active['A2'].value == f'=HYPERLINK("{copied}","source")'
    assert receipt.read_bytes() == copied.read_bytes()
    assert workbook.read_bytes() == before


def test_verified_override_requires_context_target_and_copied_bytes(snapshot, tmp_path):
    source, _, new, receipt, workbook = snapshot
    missing = str(receipt.with_name('old-name.pdf'))
    book(workbook, missing)
    location = 'reports/primary/2026/donation_summary_2026.xlsx|xl/worksheets/_rels/sheet1.xml.rels|rId1'
    data = [{'location': location, 'target_sha256': hashlib.sha256(missing.encode()).hexdigest(),
             'snapshot_receipt': receipt.relative_to(source).as_posix(), 'receipt_sha256': '0'*64}]
    path = tmp_path / 'map.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='does not match copied receipt bytes'):
        migration(snapshot, link_map=path)
    data[0]['receipt_sha256'] = hashlib.sha256(receipt.read_bytes()).hexdigest()
    path.write_text(json.dumps(data))
    assert migration(snapshot, link_map=path) == 2
    assert migration(snapshot, link_map=path, apply=True) == 2
    assert new.is_dir()


def test_encoded_and_literal_distinct_targets_fail_closed(snapshot):
    _, _, new, receipt, workbook = snapshot
    receipt.with_name('literal%20.pdf').write_bytes(b'one')
    link = receipt.with_name('literal .pdf')
    link.write_bytes(b'two')
    book(workbook, str(receipt.with_name('literal%20.pdf')))
    with pytest.raises(LinkMappingError, match='ambiguous_local_pdf_address'):
        migration(snapshot)
    assert not new.exists()


def test_manifest_path_is_checked_during_plan(snapshot):
    source, _, new, _, _ = snapshot
    data = source / 'data'
    data.mkdir()
    (data / 'processed_messages.json').write_text(json.dumps([{'saved_files': ['missing.pdf']}]))
    with pytest.raises(LinkMappingError, match='message_manifest_source_missing'):
        migration(snapshot)
    assert not new.exists()


@pytest.mark.parametrize('target,expected', [
    ('file:///C:/Legacy/receipts/name%2520%23.pdf', r'C:\Legacy\receipts\name%20#.pdf'),
    ('file://localhost/C:/Legacy/receipts/name.pdf', r'C:\Legacy\receipts\name.pdf'),
    ('file://C:/Legacy/receipts/name.pdf', r'C:\Legacy\receipts\name.pdf'),
    ('file://server/share/receipts/name.pdf', r'\\server\share\receipts\name.pdf'),
    (r'..\..\..\receipts\primary\name.pdf', r'C:\Legacy\receipts\primary\name.pdf'),
    (r'C:\Legacy\receipts\literal%80.pdf', r'C:\Legacy\receipts\literal%80.pdf'),
])
def test_windows_path_forms_without_reading_windows_files(target, expected, monkeypatch):
    import ntpath
    from pathlib import PureWindowsPath
    from types import SimpleNamespace
    from src import migrate_users as module
    monkeypatch.setattr(module, 'Path', PureWindowsPath)
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt', path=ntpath))
    assert module._local_candidates(target, PureWindowsPath(r'C:\Legacy\reports\primary\2026')) == (
        PureWindowsPath(expected),)


@pytest.mark.parametrize(('encoding', 'uri'), [
    (None, False), (None, True), ('latin1', False), ('latin1', True), ('cp1252', True), ('cp1255', True), ('cp1255', False),
])
def test_hebrew_link_migration_preserves_exact_decoded_name(snapshot, encoding, uri):
    source, _, new, old, workbook = snapshot
    name = {'cp1252': 'שקל.pdf', 'cp1255': 'פת.pdf'}.get(encoding, 'קבלה_על_תרומה.pdf')
    receipt = old.with_name(name)
    old.rename(receipt)
    receipt.with_name(receipt.stem + '__2.pdf').write_bytes(receipt.read_bytes())
    link_name = name.encode('utf-8').decode(encoding) if encoding else name
    target = receipt.with_name(link_name)
    book(workbook, target.as_uri() if uri else str(target))
    assert migration(snapshot) == 3
    assert migration(snapshot, apply=True) == 3
    output = new / 'reports/alpha/primary/2026/donation_summary_2026.xlsx'
    copied = new / 'receipts/alpha' / receipt.relative_to(source / 'receipts')
    with closing(load_workbook(output)) as wb:
        assert wb.active['A1'].hyperlink.target == str(copied)


def test_exact_path_wins_over_encoding_fallback(snapshot):
    _, _, new, receipt, workbook = snapshot
    literal = receipt.with_name(receipt.name.encode('utf-8').decode('latin1'))
    literal.write_bytes(b'exact filename source')
    book(workbook, str(literal))
    migration(snapshot, apply=True)
    output = new / 'reports/alpha/primary/2026/donation_summary_2026.xlsx'
    with closing(load_workbook(output)) as wb:
        assert Path(wb.active['A1'].hyperlink.target).read_bytes() == b'exact filename source'


def test_ambiguous_encoding_fallback_is_rejected(snapshot):
    _, _, new, receipt, workbook = snapshot
    receipt.with_name('קבלה%20.pdf').write_bytes(b'one')
    receipt.with_name('קבלה .pdf').write_bytes(b'two')
    book(workbook, str(receipt.with_name('קבלה%20.pdf'.encode('utf-8').decode('latin1'))))
    with pytest.raises(LinkMappingError, match='ambiguous_legacy_filename_encoding'):
        migration(snapshot)
    assert not new.exists()


def test_verified_override_precedes_reversible_fallback(snapshot, tmp_path):
    source, _, new, receipt, workbook = snapshot
    alternate = receipt.with_name('verified.pdf')
    alternate.write_bytes(b'verified contents')
    target = str(receipt.with_name(receipt.name.encode('utf-8').decode('latin1')))
    book(workbook, target)
    link_map = tmp_path / 'link-map.json'
    link_map.write_text(json.dumps([{
        'location': 'reports/primary/2026/donation_summary_2026.xlsx|xl/worksheets/_rels/sheet1.xml.rels|rId1',
        'target_sha256': hashlib.sha256(target.encode()).hexdigest(),
        'snapshot_receipt': alternate.relative_to(source).as_posix(),
        'receipt_sha256': hashlib.sha256(alternate.read_bytes()).hexdigest(),
    }]))
    assert migration(snapshot, link_map=link_map) == 3
    assert migration(snapshot, link_map=link_map, apply=True) == 3
    output = new / 'reports/alpha/primary/2026/donation_summary_2026.xlsx'
    with closing(load_workbook(output)) as wb:
        assert Path(wb.active['A1'].hyperlink.target).read_bytes() == alternate.read_bytes()
