import json

import openpyxl
import pytest
from openpyxl.comments import Comment

from src import sync_state
from src.attachment_saver import MONTH_NAMES
from src.receipt_metadata_extractor import ReceiptMetadata
from src.receipt_summary import COLUMN_HEADERS_HE
from src.sync_workbook import sync_approved_identifiers


def test_revision_refresh_is_proposed_once_and_explicitly_approved(tmp_path, monkeypatch):
    account, year = 'secondary', 2024
    receipts, root = tmp_path / 'receipts', tmp_path / 'private'
    root.mkdir()
    source = receipts / account / str(year) / MONTH_NAMES[1] / 'sample.pdf'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'synthetic PDF input')
    calls = []

    def extract(path, account, config, year):
        calls.append(path)
        return ReceiptMetadata(receipt_date='01/01/2024', receipt_number='0087',
                               tax_report_number='old', amount='100')

    monkeypatch.setattr(sync_state, '_extract_row', extract)
    catalog, state = sync_state.catalog_documents(receipts, account, root, None)
    sha, entry = next(iter(state['documents'].items()))
    entry.pop('identifier_refresh')
    entry['logical_id'] = 'published'
    sync_state.atomic_json(catalog, state)
    calls.clear()

    def refresh(path, *_):
        calls.append(path)
        return ReceiptMetadata(receipt_date='01/01/2024', receipt_number='',
                               tax_report_number='0061', amount='200')

    monkeypatch.setattr(sync_state, '_extract_row', refresh)
    sync_state.catalog_documents(receipts, account, root, None, refresh_year=year + 1)
    assert calls == []
    _, state = sync_state.catalog_documents(receipts, account, root, None, refresh_year=year)
    sync_state.catalog_documents(receipts, account, root, None, refresh_year=year)
    assert calls == [source]
    prior = catalog.read_bytes()
    assert sync_state.review_identifiers(receipts, account, root, year) == [
        {'document_sha256': sha, 'field': 'tax_report_number', 'before': 'old', 'value': '0061'}
    ]
    assert catalog.read_bytes() == prior
    with pytest.raises(sync_state.SyncError, match='changed'):
        sync_state.review_identifiers(receipts, account, root, year, [(sha, 'tax_report_number', '9', '')])
    assert catalog.read_bytes() == prior
    sync_state.review_identifiers(receipts, account, root, year, [(sha, 'tax_report_number', '0061', '')])
    item = json.loads(catalog.read_bytes())['documents'][sha]
    assert item['metadata']['receipt_number'] == '0087'
    assert item['metadata']['amount'] == '100'
    assert item['metadata']['tax_report_number'] == '0061'
    assert item['logical_id'] == 'published'
    assert next((root / 'review-backups').glob('*.json')).read_bytes() == prior


def test_workbook_approval_uses_document_identity_and_preserves_later_edits(monkeypatch):
    from src import report_layout
    monkeypatch.setattr(report_layout, '_report_field_columns', lambda sheet: {'tax_report_number': 2})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws['A2'], ws['B2'] = 'other publication', '003'
    ws['A7'], ws['B7'], ws['C7'] = 'stable id', 'old', '=1+2'
    ws['C7'].comment = Comment('Manual', 'Reviewer')
    rows = {'stable id': (ws, 7)}
    entries = {'stable id': {'documents': ['sha'], 'managed': {COLUMN_HEADERS_HE['tax_report_number']: 'old'}}}
    state = {'documents': {'sha': {
        'metadata': vars(ReceiptMetadata(receipt_date='01/01/2024', tax_report_number='0061')),
        'verified_identifier_updates': {'tax_report_number': {
            'before': 'old', 'value': '0061', 'year': 2024, 'source': sync_state.IDENTIFIER_REVIEW_SOURCE,
        }},
    }}}
    ws['B7'].comment = Comment('Manual control', 'Reviewer')
    with pytest.raises(sync_state.SyncError, match='baseline'):
        sync_approved_identifiers(entries, rows, state, {}, year=2024)
    assert ws['B7'].value == 'old'
    ws['B7'].comment = None
    assert sync_approved_identifiers(entries, rows, state, {}, year=2024) == 1
    assert ws['B7'].value == '0061' and ws['B2'].value == '003'
    assert ws['C7'].value == '=1+2' and ws['C7'].comment.text == 'Manual'
    assert sync_approved_identifiers(entries, rows, state, {}, year=2024) == 0
    ws['B7'].value = 'later human edit'
    assert sync_approved_identifiers(entries, rows, state, {}, year=2024) == 0
    assert ws['B7'].value == 'later human edit'


from tests.test_sync import env, document, put, report, rows, synthetic_text
from tests.test_annual2026_integrated import annual


def test_normal_annual_run_publishes_reviewed_identifier_once(env, monkeypatch):
    from src.report_layout import _report_field_columns

    base, root, provider, _ = env
    original = put(base, document())
    assert annual(env)['published'] == 1
    path = report(base)
    wb = openpyxl.load_workbook(path)
    ws, row = rows(wb)[0]
    columns = _report_field_columns(ws)
    notes = ws.cell(row, columns['notes'])
    notes.value = 'Manual review'
    notes.comment = Comment('Keep', 'Reviewer')
    wb.save(path)
    wb.close()
    catalog = root / 'sync-primary.json'
    state = json.loads(catalog.read_bytes())
    sha, item = next(iter(state['documents'].items()))
    item.pop('identifier_refresh')
    sync_state.atomic_json(catalog, state)
    changed = synthetic_text(original.read_bytes()) + '\nאישור דיווח התרומה לרשות המסים: 00041'
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf', lambda _: changed)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf', lambda _: changed)

    assert annual(env)['published'] == 0
    proposals = sync_state.review_identifiers(base/'receipts', 'primary', root, 2026)
    assert proposals == [{'document_sha256': sha, 'field': 'tax_report_number', 'before': '', 'value': '00041'}]
    wb = openpyxl.load_workbook(path)
    ws, row = rows(wb)[0]
    prior = ws.cell(row, _report_field_columns(ws)['tax_report_number']).value
    assert prior in (None, '')
    wb.close()

    sync_state.review_identifiers(base/'receipts', 'primary', root, 2026,
                                  [(sha, 'tax_report_number', '00041', '')])
    before_publish = path.read_bytes()
    result = annual(env)
    assert result['reports_updated'] == 1 and result['published'] == 0
    wb = openpyxl.load_workbook(path)
    assert len(rows(wb)) == 1
    ws, row = rows(wb)[0]
    columns = _report_field_columns(ws)
    assert ws.cell(row, columns['tax_report_number']).value == '00041'
    assert ws.cell(row, columns['receipt_number']).value == '1001'
    assert ws.cell(row, columns['notes']).comment.text == 'Keep'
    wb.close()
    assert any(p.read_bytes() == before_publish for p in (root/'report-backups').rglob('*.xlsx'))
    assert annual(env)['reports_updated'] == 0
    provider.download_attachment.assert_not_called()
