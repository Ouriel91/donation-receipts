"""Offline regressions for the 2026 Windows source candidate."""
from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path

import openpyxl
import pytest

from src.annual_excel_summary import summary_name, write_summary
from src.organization_cache import OrganizationCache
from src.organization_enrichment import dated_section46
from src.receipt_metadata_extractor import ReceiptMetadata, extract_metadata
from src.sync_state import digest, _refresh_identifiers
from src.section46_live import LiveSection46
from src.sync import run_sync
from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, SOURCES, donation_sheets, headers


def response(now):
    return json.dumps({'Status': 200, 'Message': {
        'start_date': '2024-01-01', 'end_date': '2029-12-31',
        'as_of_date': now.date().isoformat(),
    }}).encode()


def setup(tmp_path):
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    cache = OrganizationCache(tmp_path / 'organizations.json', clock=lambda: now)
    cache.save()
    live = LiveSection46(tmp_path / 'live.json', cache, transport=lambda _: response(now),
                         clock=lambda: now, sleep=lambda _: None)
    return cache, live


def document(number='1001', amount='100', date='15/01/2026', variant='original'):
    return json.dumps({'receipt_number': number, 'amount': amount, 'receipt_date': date,
                       'variant': variant}, ensure_ascii=False).encode()


def synthetic_text(raw):
    item = json.loads(raw)
    return (f'עמותה סינתטית\nעמותה רשומה: 580000001\nקבלה מס {item["receipt_number"]}\n'
            f'תאריך {item["receipt_date"]}\nסה"כ ₪{item["amount"]}\nתרומה\n')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-appdata'))
    root = tmp_path / 'registry'
    root.mkdir()
    registry = tmp_path / 'registry.csv'
    registry.write_text('מספר עמותה,שם עמותה בעברית\n580000001,השם הרשמי הסינתטי\n',
                        encoding='utf-8-sig')
    OrganizationCache(root / 'organizations.json').import_registry(registry)
    for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr(f'src.receipt_summary.{reader}', lambda path: synthetic_text(path.read_bytes()))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr('src.sync.LiveSection46', lambda path, cache: LiveSection46(
        path, cache, transport=lambda _: response(now), clock=lambda: now, sleep=lambda _: None))
    return tmp_path, root


def put(base, raw, filename):
    path = base / 'receipts/primary/2026/01_January' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def report(base):
    return base / 'reports/primary/2026/donation_summary_2026.xlsx'


def rows(wb):
    return [(ws, row) for ws in donation_sheets(wb) for row in range(2, ws.max_row + 1)
            if ws.cell(row, headers(ws)[ID_HEADER]).value]


def saved(env):
    base, root = env
    return run_sync(provider=None, receipts_dir=base/'receipts', reports_dir=base/'reports',
                    accounts_dir=base/'accounts', manifest_path=base/'processed_messages.json',
                    account='primary', year=2026, root=root, full_report=True,
                    resume_pending=True, refresh_base=True)



@pytest.mark.parametrize('text,receipt,tax', [
    ('קבלה על תרומה 14519', '14519', ''),
    ('קבלה עבור תרומה 15523', '15523', ''),
    ('אישור דיווח התרומה לרשות המסים: 18828', '', '18828'),
    ('מספר אישור דיווח התרומה לרשות המסים: 46664', '', '46664'),
    ('קבלה עבור תרומה —\n00224\nאישור דיווח\nתרומה לרשות המיסים：\n00151', '00224', '00151'),
    ('אישור דיווח התרומה לרשות המסים: 45678\n45678 # קבלה', '45678', '45678'),
])
def test_identifier_regressions(text, receipt, tax):
    result = extract_metadata(text, Path('sample.pdf'))
    assert (result.receipt_number, result.tax_report_number) == (receipt, tax)


def test_refresh_clears_only_with_positive_tax_evidence_and_review():
    entry = {'metadata': {'receipt_number': '12345', 'tax_report_number': ''}}
    _refresh_identifiers(entry, ReceiptMetadata(), 2026)
    assert not entry['identifier_refresh']['proposals']
    _refresh_identifiers(entry, ReceiptMetadata(tax_report_number='12345'), 2026)
    assert entry['identifier_refresh']['proposals']['receipt_number'] == {'before': '12345', 'value': ''}
    entry['verified_identifier_updates'] = {'receipt_number': {}}
    _refresh_identifiers(entry, ReceiptMetadata(tax_report_number='12345'), 2026)
    assert 'receipt_number' not in entry['identifier_refresh']['proposals']


@pytest.mark.parametrize('cache_status,live_match,expected', [
    ('unknown', True, 'approved'),
    ('unknown', False, 'unknown'),
    ('denied', False, 'denied'),
    ('denied', True, 'unknown'),
    ('approved', False, 'approved'),
])
def test_dated_authoritative_evidence(cache_status, live_match, expected, tmp_path, monkeypatch):
    cache, live = setup(tmp_path)
    live.prepare('123456789')
    donated = date(2026 if live_match else 2020, 3, 1)
    evidence = {
        'section46_status': cache_status, 'stale': False, 'conflict': False,
        'provenance': [{'retrieved_at': '2026-11-09', 'source_url': 'https://www.gov.il/',
                        'approval_periods': [], 'denial_periods': []}],
    }
    monkeypatch.setattr(cache, 'lookup_tax', lambda number, supplied_date: evidence
                        if number == '123456789' and supplied_date == donated
                        else pytest.fail('Incorrect identity or donation date'))
    assert dated_section46(cache, live, '123456789', donated)[1] == expected


def test_summary_all_months_require_published_identity_and_exclude_archives():
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    write_summary(wb, 2026, {})
    summary = wb[summary_name(2026)]
    publication = wb.create_sheet(INDEX)
    publication.append(['Donation ID'])
    active = {}
    for r in range(8, 20):
        ws = wb[summary.cell(r, 1).value]
        cols = headers(ws)
        key = 'd-' + digest(str(r).encode())
        publication.append([key])
        ws.cell(2, cols[ID_HEADER], key)
        ws.cell(3, cols[ID_HEADER], 'technical')
        ws.cell(4, cols[ID_HEADER], key)
        ws.cell(1, ws.max_column + 1, ARCHIVE)
        ws.cell(4, headers(ws)[ARCHIVE], 'archived')
        ws.row_dimensions[2].hidden = True
        active[key] = (ws, 2)

    publication.append(['separate-index-entry'])
    write_summary(wb, 2026, active)
    for r in range(8, 20):
        for col in (2, 3, 4, 5):
            cell = summary.cell(r, col)
            assert cell.value.ref == cell.coordinate
            formula = cell.value.text
            assert '!$A$2:$A$14' in formula
            assert '$2:' in formula and '$4' in formula
            assert 'SUBTOTAL' not in formula
    assert wb.calculation.calcMode == 'auto'


def test_distinct_original_copy_link_and_repeat(env, monkeypatch):
    base, _ = env
    # Start with each source published separately, as in the 2026 workbook.
    original = put(base, document(date='19/07/2026'), filename='תרומה # + % O.pdf')
    copied = put(base, document(number='1002', date='19/07/2026', variant='copy'),
                 filename='תרומה # + % C.pdf')

    corrected = False

    def text(path):
        raw = synthetic_text(path.read_bytes())
        if corrected and path.name == copied.name:
            raw = raw.replace('1002', '1001')
        return raw + (
            'אישור דיווח התרומה לרשות המסים: 00701\n' if path.name == copied.name else ''
        )

    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf', text)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf', text)
    saved(env)
    path = report(base)
    with closing(openpyxl.load_workbook(path)) as wb:
        assert len(rows(wb)) == 2
        active = rows(wb)
        stable_id = active[0][0].cell(active[0][1], headers(active[0][0])[ID_HEADER]).value
        for ws, row in active:
            ws.cell(row, headers(ws)['מספר קבלה'], '1001')
        wb.save(path)
    # Simulate reviewed source identifiers with the corrected source metadata.
    catalog = env[1] / 'sync-primary.json'
    state = json.loads(catalog.read_bytes())
    state['documents'][digest(copied.read_bytes())]['metadata']['receipt_number'] = '1001'
    catalog.write_text(json.dumps(state), encoding='utf-8')
    corrected = True

    saved(env)
    with closing(openpyxl.load_workbook(path)) as wb:
        (ws, row), = rows(wb)
        assert ws.cell(row, headers(ws)[ID_HEADER]).value == stable_id
        assert ws.cell(row, headers(ws)['שם קובץ']).hyperlink.target == str(copied)
        assert sum(bool(ws.row_dimensions[r].hidden) for r in range(2, ws.max_row + 1)) == 1
        assert {cell[2].hyperlink.target for cell in wb[SOURCES].iter_rows(min_row=2)} >= {str(original), str(copied)}
    assert saved(env)['reports_updated'] == 0
    assert original.is_file() and copied.is_file()
    with closing(openpyxl.load_workbook(path)) as wb:
        (ws, row), = rows(wb)
        # Only change the address. A caption refresh must not claim this edit.
        ws.cell(row, headers(ws)['שם קובץ']).hyperlink = str(original)
        wb.save(path)
    saved(env)
    saved(env)
    with closing(openpyxl.load_workbook(path)) as wb:
        (ws, row), = rows(wb)
        assert ws.cell(row, headers(ws)['שם קובץ']).hyperlink.target == str(original)


def test_reviewed_blank_receipt_publishes_and_repeat_preserves_manual_edit(env, monkeypatch):

    from src import sync_state

    base, root = env
    path = put(base, document(), filename='fixture.pdf')
    saved(env)
    workbook = report(base)
    catalog = root / 'sync-primary.json'
    state = json.loads(catalog.read_bytes())
    sha = digest(path.read_bytes())
    state['documents'][sha]['identifier_refresh']['version'] = 1
    sync_state.atomic_json(catalog, state)
    observed = ReceiptMetadata(receipt_date='15/01/2026', tax_report_number='1001')
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: observed)
    saved(env)
    proposals = sync_state.review_identifiers(base / 'receipts', 'primary', root, 2026)
    assert {'document_sha256': sha, 'field': 'receipt_number',
            'before': '1001', 'value': ''} in proposals
    sync_state.review_identifiers(base / 'receipts', 'primary', root, 2026,
                                  [(sha, 'receipt_number', '', '1001')])
    saved(env)
    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        receipt = ws.cell(row, headers(ws)['מספר קבלה'])
        assert receipt.value is None
        receipt.value = 'manual-correction'
        wb.save(workbook)
    saved(env)
    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        assert ws.cell(row, headers(ws)['מספר קבלה']).value == 'manual-correction'
