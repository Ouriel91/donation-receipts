from contextlib import closing
import json
from io import StringIO

import openpyxl
from openpyxl.comments import Comment
import pytest

from src.logger import RunDiagnostics
from src.organization_enrichment import LIVE_HEADERS
from src.sync_state import SyncError, digest, review_identifiers
from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, headers
from tests.test_qa_fixes_2026 import env, document, put, report, rows, saved, synthetic_text

UNKNOWN = 'לא ידוע / דורש בירור'


def test_reviewed_historical_status_and_amount_then_manual_edit(env):
    base, root = env
    first = put(base, document('17001', date='08/04/2026'), 'first.pdf')
    second = put(base, document('17002', date='08/04/2026'), 'second.pdf')
    saved(env)
    path = report(base)
    ids = []
    with closing(openpyxl.load_workbook(path)) as wb:
        for i, (ws, row) in enumerate(rows(wb)):
            cols = headers(ws)
            key = ws.cell(row, cols[ID_HEADER]).value
            ids.append(key)
            item = next(record[1] for record in wb[INDEX].iter_rows(min_row=2, max_col=2)
                        if record[0].value == key)
            data = json.loads(item.value)
            data['managed'].pop(LIVE_HEADERS[0])
            item.value = json.dumps(data)
            status = ws.cell(row, cols[LIVE_HEADERS[0]])
            status.value = UNKNOWN
            if i:
                status.comment = Comment('Manual review', 'Tester')
                ws.cell(row, cols['סכום']).value = 51
            else:
                ws.cell(row, cols['סכום']).value = None
                ws.cell(row, cols[LIVE_HEADERS[3]]).value = 'Manual evidence note'
                ws.cell(row, cols[LIVE_HEADERS[3]]).comment = Comment('Keep', 'Tester')
                assert ws.cell(row, cols['מספר קבלה']).value == '17001'
        wb.save(path)
    before = saved(env)
    assert not before.get('corrections_published')
    sha = digest(first.read_bytes())
    with pytest.raises(SyncError, match='approved amount'):
        review_identifiers(base/'receipts','primary',root,2026, amounts=[(sha,ids[0],'99','')])
    with pytest.raises(SyncError, match='canonical organization'):
        review_identifiers(base/'receipts','primary',root,2026,
                           eligibility=[(ids[0], UNKNOWN, '580000002', '2026-04-08')], reports_dir=base/'reports')
    with pytest.raises(SyncError, match='uniquely own'):
        review_identifiers(base/'receipts','primary',root,2026,
                           amounts=[(sha,ids[1],'100','')], reports_dir=base/'reports')
    with pytest.raises(SyncError, match='differs'):
        review_identifiers(base/'receipts','primary',root,2026,
                           amounts=[(sha,ids[0],'100','99')], reports_dir=base/'reports')
    review_identifiers(base/'receipts','primary',root,2026,
                       amounts=[(sha,ids[0],'100','')], reports_dir=base/'reports')
    assert review_identifiers(base/'receipts','primary',root,2026, revoke_amounts=[sha])['revoked_amounts'] == 1
    assert not saved(env).get('corrections_published')
    review_identifiers(base/'receipts','primary',root,2026,
                       amounts=[(sha,ids[0],'100',''),
                                (digest(second.read_bytes()),ids[1],'100','51')],
                       eligibility=[(key,UNKNOWN,'580000001','2026-04-08') for key in ids], reports_dir=base/'reports')
    output = StringIO()
    with RunDiagnostics(log_dir=base/'diag', stream=output) as diagnostics:
        counts = saved(env)
        diagnostics.done(counts, 'synthetic')
    assert counts['corrections_published'] == 2
    assert 'corrections_published=2' in output.getvalue()
    assert 'section46_write_blocked=2' in output.getvalue()
    assert 'section46_companions_preserved=1' in output.getvalue()
    with closing(openpyxl.load_workbook(path)) as wb:
        ws,row = rows(wb)[0]
        cols = headers(ws)
        assert ws.cell(row, cols[LIVE_HEADERS[0]]).value == UNKNOWN
        details = ws.cell(row, cols[LIVE_HEADERS[3]])
        assert details.comment.text == 'Keep'
        # The operator explicitly resolves the conflicting protected evidence text.
        details.comment = None
        details.value = None
        wb.save(path)
    assert saved(env)['corrections_published'] == 1
    assert review_identifiers(base/'receipts','primary',root,2026,
                              revoke_eligibility=[ids[1]])['revoked_section46'] == 1
    with closing(openpyxl.load_workbook(path)) as wb:
        for i, (ws,row) in enumerate(rows(wb)):
            cols=headers(ws)
            assert ws.cell(row,cols[ID_HEADER]).value == ids[i]
            status=ws.cell(row,cols[LIVE_HEADERS[0]])
            assert status.value == (UNKNOWN if i else 'מאושר')
            if i:
                assert status.comment.text == 'Manual review'
                assert ws.cell(row,cols['סכום']).value == 100
            else:
                entry = next(json.loads(record[1]) for record in wb[INDEX].iter_rows(
                    min_row=2,max_col=2,values_only=True) if record[0] == ids[0])
                assert entry['historical_section46_applied']['before'] == UNKNOWN
                assert 'Manual evidence note' not in ws.cell(row, cols[LIVE_HEADERS[3]]).value
                amount = ws.cell(row,cols['סכום'])
                assert amount.value == 100 and amount.data_type == 'n'
                amount.value = 85
                status.value = 'הכרעה ידנית'
        wb.save(path)
    assert not saved(env).get('corrections_published')
    with closing(openpyxl.load_workbook(path)) as wb:
        ws,row=rows(wb)[0]
        cols=headers(ws)
        assert ws.cell(row,cols['סכום']).value == 85
        assert ws.cell(row,cols[LIVE_HEADERS[0]]).value == 'הכרעה ידנית'
    assert saved(env)['reports_updated'] == 0


@pytest.mark.parametrize('numeric,active_count', [(True,1), (False,2)])
def test_lowercase_pair_requires_numeric_stem_and_receipt_evidence(env, monkeypatch, numeric, active_count):
    base,_ = env
    stem = '204017' if numeric else 'receipt'
    original = put(base,document(date='19/07/2026'),stem+'o.pdf')
    copied = put(base,document(date='19/07/2026',variant='copy'),stem+'c.pdf')
    def text(path):
        return synthetic_text(path.read_bytes())+(
            'אישור דיווח התרומה לרשות המסים: 00701\n' if path == copied else '')
    for reader in ('extract_text_from_pdf','extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr(f'src.receipt_summary.{reader}', text)
    saved(env)
    path = report(base)
    with closing(openpyxl.load_workbook(path)) as wb:
        active = rows(wb)
        assert len(active) == active_count
        if numeric:
            (ws,row), = active
            assert ws.cell(row,headers(ws)['שם קובץ']).hyperlink.target == str(copied)
            (archived,) = (r for r in range(2,ws.max_row+1) if ws.cell(r,headers(ws)[ARCHIVE]).value)
            assert ws.row_dimensions[archived].hidden
    assert original.is_file() and copied.is_file()
    first_ids = [ws.cell(r, headers(ws)[ID_HEADER]).value for ws, r in active]
    saved(env)
    with closing(openpyxl.load_workbook(path)) as wb:
        repeated = rows(wb)
        assert [ws.cell(r,headers(ws)[ID_HEADER]).value for ws,r in repeated] == first_ids
        if numeric:
            (ws,row), = repeated
            assert ws.cell(row,headers(ws)['שם קובץ']).hyperlink.target == str(copied)
            assert ws.row_dimensions[archived].hidden
