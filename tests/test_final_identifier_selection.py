from contextlib import closing
import json

import openpyxl
import pytest
from openpyxl.comments import Comment

from src import sync_state
from src.receipt_metadata_extractor import ReceiptMetadata
from src.organization_enrichment import LIVE_HEADERS
from src.sync_workbook import ID_HEADER, headers
from tests.test_qa_fixes_2026 import document, env, put, report, rows, saved


def test_v2_selected_refresh_compares_published_and_requires_review(env, monkeypatch):
    base, root = env
    path = put(base, document(), 'source.pdf')
    saved(env)
    workbook = report(base)
    catalog = root/'sync-primary.json'
    sha = sync_state.digest(path.read_bytes())
    before = catalog.read_bytes()
    item = json.loads(before)['documents'][sha]
    assert item['identifier_refresh']['version'] == 2
    assert item['metadata']['receipt_number'] == '1001'

    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        columns = headers(ws)
        preserved = tuple(ws.cell(row, columns[label]).value for label in (ID_HEADER, 'סכום', LIVE_HEADERS[0]))
        ws.cell(row, columns['מספר קבלה'], '71234')
        wb.save(workbook)
    canonical = workbook.read_bytes()
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(
        receipt_date='15/01/2026', receipt_number='1001', tax_report_number='71234', amount='100'))
    args = base/'receipts', base/'reports', base/'accounts', 'primary', root, 2026, [sha]
    plan, = sync_state.refresh_selected_identifiers(*args, preview=True)
    assert plan['proposals']['receipt_number'] == {
        'before':'1001', 'value':'1001', 'published_before':'71234'}
    assert catalog.read_bytes() == before and workbook.read_bytes() == canonical
    assert sync_state.refresh_selected_identifiers(*args) == [plan]
    assert workbook.read_bytes() == canonical
    assert list((root/'review-backups').glob(f'*{sync_state.digest(before)}.json'))
    saved(env)
    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        assert ws.cell(row, headers(ws)['מספר קבלה']).value == '71234'
    args_review = base/'receipts','primary',root,2026
    with pytest.raises(sync_state.SyncError, match='Proposal or source changed'):
        sync_state.review_identifiers(*args_review, [(sha,'receipt_number','1001','earlier')])
    sync_state.review_identifiers(*args_review, [
        (sha,'receipt_number','1001','71234'), (sha,'tax_report_number','71234','')])
    assert saved(env)['corrections_published'] == 2
    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws,row), = rows(wb)
        assert ws.cell(row,headers(ws)['מספר קבלה']).value == '1001'
        assert ws.cell(row,headers(ws)['מספר אישור דיווח']).value == '71234'
        assert tuple(ws.cell(row, headers(ws)[label]).value for label in (ID_HEADER, 'סכום', LIVE_HEADERS[0])) == preserved
    assert not saved(env).get('corrections_published')
    historical = json.loads(catalog.read_bytes())['documents'][sha]['verified_identifier_updates']
    assert sync_state.refresh_selected_identifiers(*args)[0]['proposals'] == {}
    assert json.loads(catalog.read_bytes())['documents'][sha]['verified_identifier_updates'] == historical


def test_selected_preview_preserves_commented_identifier_and_rejects_changed_pdf(env, monkeypatch):
    base, root = env
    pdf = put(base, document(), 'source.pdf')
    saved(env)
    sha = sync_state.digest(pdf.read_bytes())
    workbook = report(base)
    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        ws.cell(row,headers(ws)['מספר קבלה']).comment = Comment('Verified', 'Tester')
        wb.save(workbook)
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(
        receipt_date='15/01/2026', receipt_number='9000'))
    args = base/'receipts',base/'reports',base/'accounts','primary',root,2026,[sha]
    assert sync_state.refresh_selected_identifiers(*args, preview=True)[0]['proposals'] == {}
    pdf.write_bytes(b'changed')
    with pytest.raises(sync_state.SyncError, match='verified source'):
        sync_state.refresh_selected_identifiers(*args)


def test_unreadable_refresh_invalidates_old_pending_proposal_but_backs_it_up(env, monkeypatch):
    base, root = env
    pdf = put(base, document(), 'source.pdf')
    saved(env)
    catalog = root/'sync-primary.json'
    sha = sync_state.digest(pdf.read_bytes())
    args = base/'receipts', base/'reports', base/'accounts', 'primary', root, 2026, [sha]
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(receipt_number='91005'))
    assert sync_state.refresh_selected_identifiers(*args)[0]['proposals']
    before = catalog.read_bytes()
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(notes='לא ניתן לקרוא את הקובץ'))
    assert sync_state.refresh_selected_identifiers(*args, preview=True)[0]['review_required']
    assert catalog.read_bytes() == before
    assert not sync_state.refresh_selected_identifiers(*args)[0]['proposals']
    assert not sync_state.review_identifiers(base/'receipts', 'primary', root, 2026)
    assert list((root/'review-backups').glob(f'*{sync_state.digest(before)}.json'))


def test_multiple_sources_are_previewed_without_staging_a_replacement(env, monkeypatch):
    from src.sync_workbook import ID_HEADER, INDEX

    base, root = env
    original = put(base, document(), 'sampleO.pdf')
    copied = put(base, document(variant='copy'), 'sampleC.pdf')
    saved(env)
    sha = sync_state.digest(original.read_bytes())
    workbook = report(base)
    with closing(openpyxl.load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        publication = ws.cell(row, headers(ws)[ID_HEADER]).value
        entry = next(json.loads(raw) for key, raw in wb[INDEX].iter_rows(min_row=2, max_col=2, values_only=True)
                     if key == publication)
        assert set(entry['documents']) == {sha, sync_state.digest(copied.read_bytes())}
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(receipt_number='91005'))
    args = base/'receipts', base/'reports', base/'accounts', 'primary', root, 2026, [sha]
    result, = sync_state.refresh_selected_identifiers(*args)
    assert result['multiple_sources'] and result['review_required'] and not result['proposals']


@pytest.mark.parametrize('protect', [False, True])
def test_selected_refresh_supersedes_applied_misclassified_receipt(env, monkeypatch, protect):
    base, root = env
    pdf = put(base, document(), 'source.pdf')
    saved(env)
    sha = sync_state.digest(pdf.read_bytes())
    catalog = root/'sync-primary.json'
    args = base/'receipts', base/'reports', base/'accounts', 'primary', root, 2026, [sha]
    review_args = base/'receipts', 'primary', root, 2026
    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(
        receipt_date='15/01/2026', receipt_number='71358'))
    sync_state.refresh_selected_identifiers(*args)
    sync_state.review_identifiers(*review_args, [(sha,'receipt_number','71358','1001')])
    assert saved(env)['corrections_published'] == 1
    old = json.loads(catalog.read_bytes())['documents'][sha]['verified_identifier_updates']['receipt_number']

    monkeypatch.setattr(sync_state, '_extract_row', lambda *_: ReceiptMetadata(
        receipt_date='15/01/2026', receipt_number='1001', tax_report_number='71358'))
    plan, = sync_state.refresh_selected_identifiers(*args)
    assert 'supersedes' in plan['proposals']['receipt_number']
    assert saved(env).get('corrections_published', 0) == 0
    if protect:
        with closing(openpyxl.load_workbook(report(base))) as wb:
            (ws, row), = rows(wb)
            ws.cell(row, headers(ws)['מספר קבלה']).comment = Comment('Keep', 'Tester')
            wb.save(report(base))
        catalog_before = catalog.read_bytes()
        with pytest.raises(sync_state.SyncError, match='Protected cell'):
            sync_state.review_identifiers(*review_args, [(sha,'receipt_number','1001','71358')],
                                          reports_dir=base/'reports')
        assert catalog.read_bytes() == catalog_before
        return
    sync_state.review_identifiers(*review_args, [
        (sha,'receipt_number','1001','71358'), (sha,'tax_report_number','71358','')],
        reports_dir=base/'reports')
    assert saved(env)['corrections_published'] == 2
    assert saved(env).get('corrections_published', 0) == 0
    assert json.loads(catalog.read_bytes())['documents'][sha]['identifier_approval_history'] == {
        'receipt_number': [old]}
    with closing(openpyxl.load_workbook(report(base))) as wb:
        (ws, row), = rows(wb)
        assert ws.cell(row, headers(ws)['מספר קבלה']).value == '1001'
        assert ws.cell(row, headers(ws)['מספר אישור דיווח']).value == '71358'
