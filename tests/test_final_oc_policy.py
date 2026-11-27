from contextlib import closing
import json
from pathlib import Path

import openpyxl
import pytest
from openpyxl.comments import Comment

from src.sync_state import SyncError, digest
from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, _oc_role, headers, reviewed_oc_pair
from tests.test_qa_fixes_2026 import document, env, put, report, rows, saved, synthetic_text


@pytest.mark.parametrize('suffixes', [('o','c'), ('O','C'), ('o','C')])
def test_numeric_pair_prefers_copy_and_retains_both_sources(env, suffixes):
    base, _ = env
    original = put(base, document(), '12345' + suffixes[0] + '.pdf')
    saved(env)
    with closing(openpyxl.load_workbook(report(base))) as wb:
        (sheet, row), = rows(wb)
        cols = headers(sheet)
        sheet.cell(row, cols['הערות'], 'Protected correction').comment = Comment('Keep', 'Tester')
        key = sheet.cell(row, cols[ID_HEADER]).value
        wb.save(report(base))
    copied = put(base, document(variant='copy'), '12345' + suffixes[1] + '.pdf')
    for _ in range(2):
        saved(env)
        with closing(openpyxl.load_workbook(report(base))) as wb:
            (sheet, row), = rows(wb)
            cols = headers(sheet)
            assert sheet.cell(row, cols[ID_HEADER]).value == key
            assert sheet.cell(row, cols['שם קובץ']).hyperlink.target == str(copied)
            assert sheet.cell(row, cols['הערות']).value == 'Protected correction'
            assert sheet.cell(row, cols['הערות']).comment.text == 'Keep'
            assert any(sheet.cell(i, cols[ARCHIVE]).value for i in range(2, sheet.max_row + 1))
            entry = next(json.loads(raw) for publication, raw in wb[INDEX].iter_rows(
                min_row=2, max_col=2, values_only=True) if publication == key)
            assert set(entry['documents']) == {digest(original.read_bytes()), digest(copied.read_bytes())}


@pytest.mark.parametrize('names', [('abcC.pdf','abcO.pdf'), ('12345o.pdf','67890c.pdf'),
                                  ('abc.pdf','abo.pdf'), ('tag_12345o.pdf','tag_12345c.pdf'),
                                  ('12345o__2.pdf','12345c__2.pdf')])
def test_ineligible_names_leave_matching_receipts_separate(env, names):
    base, _ = env
    put(base, document(), names[0])
    put(base, document(variant='copy'), names[1])
    saved(env)
    with closing(openpyxl.load_workbook(report(base))) as wb:
        assert len(rows(wb)) == 2


@pytest.mark.parametrize('mismatch', ['number','missing','ambiguous','organization','date','amount'])
def test_invalid_pdf_identity_preserves_both_publications(env, monkeypatch, mismatch):
    base, root = env
    original = put(base, document(), '12345o.pdf')
    copied = put(base, document(number='2002' if mismatch == 'number' else '1001',
                                amount='200' if mismatch == 'amount' else '100',
                                date='16/01/2026' if mismatch == 'date' else '15/01/2026',
                                variant='copy'), '12345c.pdf')
    def read(path):
        text = synthetic_text(path.read_bytes())
        if path == copied:
            if mismatch == 'missing':
                text = text.replace('קבלה מס 1001', '')
            if mismatch == 'ambiguous':
                text += '\nקבלה מספר: 3003\n'
            if mismatch == 'organization':
                text = text.replace('580000001', '580000002')
        return text
    for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr('src.receipt_summary.' + reader, read)
    saved(env)
    with closing(openpyxl.load_workbook(report(base))) as wb:
        active = rows(wb)
        assert len(active) == 2
        by_name = {ws.cell(row, headers(ws)['שם קובץ']).value: (ws,row) for ws,row in active}
        sources = {'O': by_name[original.name], 'C': by_name[copied.name]}
        entries = {'O': {'documents': [digest(original.read_bytes())]},
                   'C': {'documents': [digest(copied.read_bytes())]}}
        state = json.loads((root/'sync-primary.json').read_bytes())
        with pytest.raises(SyncError):
            reviewed_oc_pair(sources, entries, state, base/'receipts/primary', 'O', 'C')


def test_unpaired_copy_does_not_set_preference(env):
    base, _ = env
    put(base, document(), '12345c.pdf')
    saved(env)
    with closing(openpyxl.load_workbook(report(base))) as wb:
        assert len(rows(wb)) == 1
        assert all('representative_document' not in json.loads(entry)
                   for _, entry in wb[INDEX].iter_rows(min_row=2, max_col=2, values_only=True)
                   if entry)
    assert _oc_role(Path('abr0c.pdf')) is None
    assert _oc_role(Path('reference Copy.pdf')) == (('labelled', 'reference'), 'C')


def test_review_uses_fresh_evidence_and_respects_distinct_decision(env, monkeypatch):
    from src import sync_state

    base, root = env
    original = put(base, document(), '12345O.pdf')
    copied = put(base, document(variant='copy'), '12345c.pdf')
    extract = sync_state._extract_row

    def cached(path, *args, **kwargs):
        meta = extract(path, *args, **kwargs)
        if path == original:
            meta.receipt_number = ''
        return meta

    monkeypatch.setattr(sync_state, '_extract_row', cached)
    saved(env)
    state = json.loads((root/'sync-primary.json').read_bytes())
    with closing(openpyxl.load_workbook(report(base))) as wb:
        active = rows(wb)
        assert len(active) == 2
        by_name = {ws.cell(row, headers(ws)['שם קובץ']).value: (ws,row) for ws,row in active}
        published = {'O': by_name[original.name], 'C': by_name[copied.name]}
        original_sha, copy_sha = (digest(p.read_bytes()) for p in (original, copied))
        entries = {'O': {'documents': [original_sha]}, 'C': {'documents': [copy_sha]}}
        proof = reviewed_oc_pair(published, entries, state, base/'receipts/primary', 'O', 'C')
        assert proof['copy_sha'] == copy_sha
        state['documents'][original_sha]['review_decision'] = {'action': 'distinct'}
        with pytest.raises(SyncError, match='reviewed as distinct'):
            reviewed_oc_pair(published, entries, state, base/'receipts/primary', 'O', 'C')
