from contextlib import closing
import json

import openpyxl
from openpyxl.comments import Comment
import pytest

from src import sync_state
from src.report_layout import PDF_AUDIT
from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, headers
from tests.test_qa_fixes_2026 import document, env, put, report, rows, saved, synthetic_text


def test_reviewed_oc_allows_missing_catalog_id_and_preserves_sources(env, monkeypatch):
    base, root = env
    original = put(base, document(date='21/07/2026'), '21_07_26__generated_batch_828489o.pdf')
    copied = put(base, document(date='21/07/2026', variant='copy'), '21_07_26__generated_batch_828489c.pdf')
    copy_folder = copied.parent.with_name('02_February')
    copy_folder.mkdir()
    copied = copied.rename(copy_folder / copied.name)
    alias = copied.with_name('21_07_26__generated_batch_828489c__2.pdf')
    alias.write_bytes(copied.read_bytes())

    def text(path):
        return synthetic_text(path.read_bytes()) + (
            'אישור דיווח התרומה לרשות המסים: 00238\n' if path.stem.removesuffix('__2').endswith('c') else '')

    for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr(f'src.receipt_summary.{reader}', text)
    extract = sync_state._extract_row

    def cached(path, *args):
        result = extract(path, *args)
        if path == original:
            result.receipt_number = ''
        return result

    monkeypatch.setattr(sync_state, '_extract_row', cached)
    saved(env)
    path = report(base)
    ids = {}
    with closing(openpyxl.load_workbook(path)) as wb:
        active = rows(wb)
        assert len(active) == 2
        for ws, row in active:
            cols = headers(ws)
            name = ws.cell(row, cols['שם קובץ']).value
            ids[name] = ws.cell(row, cols[ID_HEADER]).value
            ws.cell(row, cols['הערות']).comment = Comment('Keep ' + name, 'Tester')
            if name == original.name:
                ws.cell(row, cols['שם קובץ']).hyperlink = original.as_uri()
            key = ids[name]
            snapshot = next(raw for pub, raw in wb[INDEX].iter_rows(min_row=2, max_col=2) if pub.value == key)
            data = json.loads(snapshot.value)
            data['identifier_approvals'] = {'shared-test-key': name}
            snapshot.value = json.dumps(data)
        if PDF_AUDIT in wb:
            del wb[PDF_AUDIT]
        first = active[0][0].cell(active[0][1], headers(active[0][0])[ID_HEADER]).value
        assert first == ids[original.name]
        wb.save(path)
    before = path.read_bytes()
    args = base/'receipts', 'primary', root, 2026
    with pytest.raises(sync_state.SyncError, match='numeric O/C source'):
        sync_state.review_identifiers(*args, oc_pairs=[(ids[copied.name], ids[original.name])], reports_dir=base/'reports')
    sync_state.review_identifiers(*args, oc_pairs=[(ids[original.name], ids[copied.name])], reports_dir=base/'reports')
    assert before == path.read_bytes()
    for _ in range(2):
        saved(env)
        with closing(openpyxl.load_workbook(path)) as wb:
            (ws, row), = rows(wb)
            cols = headers(ws)
            assert ws.cell(row, cols[ID_HEADER]).value == first
            assert ws.cell(row, cols['סכום']).value == 100
            assert ws.cell(row, cols['שם קובץ']).hyperlink.target == str(copied)
            archives = [r for r in range(2, ws.max_row+1) if ws.cell(r,cols[ARCHIVE]).value]
            assert len(archives) == 1 and ws.row_dimensions[archives[0]].hidden
            assert all(ws.cell(r, cols['הערות']).comment for r in [row, *archives])
            entry = next(json.loads(raw) for key,raw in wb[INDEX].iter_rows(min_row=2,max_col=2,values_only=True) if key==first)
            assert len(entry['documents']) == 2 and entry['historical_publication_ids']
            assert entry['identifier_approvals']['shared-test-key'] == original.name
    assert all(source.exists() for source in (original, copied, alias))
    assert sync_state.review_identifiers(*args, revoke_oc=[ids[original.name]])['revoked_oc_pairs'] == 1
    assert json.loads((root/'sync-primary.json').read_bytes())['reviewed_oc_pairs'] == {}
    saved(env)
    with closing(openpyxl.load_workbook(path)) as wb:
        (ws,row), = rows(wb)
        assert ws.cell(row,headers(ws)[ID_HEADER]).value == first
        assert ws.cell(row,headers(ws)['שם קובץ']).hyperlink.target == str(copied)



def test_review_does_not_unify_different_receipt_numbers_from_pdfs(env):
    base, root = env
    original = put(base, document(number='1001', date='21/07/2026'), '21_07_26__tag_687163o.pdf')
    copied = put(base, document(number='1002', date='21/07/2026', variant='copy'), '21_07_26__tag_687163c__2.pdf')
    saved(env)
    path = report(base)
    with closing(openpyxl.load_workbook(path)) as wb:
        ids = {ws.cell(row,headers(ws)['שם קובץ']).value: ws.cell(row,headers(ws)[ID_HEADER]).value
               for ws,row in rows(wb)}
    before = path.read_bytes()
    with pytest.raises(sync_state.SyncError, match='consistent identity'):
        sync_state.review_identifiers(base/'receipts','primary',root,2026,
            oc_pairs=[(ids[original.name],ids[copied.name])], reports_dir=base/'reports')
    assert path.read_bytes() == before



@pytest.mark.parametrize('name, expected', [
    ('21_07_26__generated_batch_828489o.pdf', ('828489', 'O', False)),
    ('21_07_26__generated_batch_828489c.pdf', ('828489', 'C', False)),
    ('21_07_26__generated_batch_828489c__2.pdf', ('828489', 'C', True)),
    ('21_07_26__generated_batch_828489o__12.PDF', ('828489', 'O', True)),
    ('828489O.pdf', ('828489', 'O', False)),
    ('828489c__1.pdf', None),
    ('828489c__2_preview.pdf', None),
    ('tok828489o.pdf', None),
    ('828489c__2.jpg', None),
    ('2026-07-05_O.pdf', None),
    ('21_07_26c.pdf', None),
])
def test_numeric_oc_filename(name, expected):
    from pathlib import Path
    from src.sync_workbook import _numeric_oc_filename
    parsed = _numeric_oc_filename(Path(name))
    assert (parsed[:3] if parsed else None) == expected


def test_review_requires_same_numeric_base_even_for_matching_pdf_receipt(env):
    base, root = env
    original = put(base, document(date='21/07/2026'), '21_07_26__tag_828489o__2.pdf')
    copied = put(base, document(date='21/07/2026', variant='copy'), '21_07_26__tag_377130c__2.pdf')
    saved(env)
    path = report(base)
    with closing(openpyxl.load_workbook(path)) as wb:
        ids = {ws.cell(row,headers(ws)['שם קובץ']).value: ws.cell(row,headers(ws)[ID_HEADER]).value
               for ws,row in rows(wb)}
    with pytest.raises(sync_state.SyncError, match='consistent identity'):
        sync_state.review_identifiers(base/'receipts','primary',root,2026,
            oc_pairs=[(ids[original.name],ids[copied.name])], reports_dir=base/'reports')


def test_auto_pairs_long_names_with_only_collision_suffixes(env):
    base, _ = env
    original = put(base, document(date='21/07/2026'), '21_07_26__tag_828489o__2.pdf')
    copied = put(base, document(date='21/07/2026', variant='copy'), '21_07_26__tag_828489c__2.pdf')
    for _ in range(2):
        saved(env)
        with closing(openpyxl.load_workbook(report(base))) as wb:
            assert len(rows(wb)) == 1
    assert original.is_file() and copied.is_file()


@pytest.mark.parametrize('mismatch', ['date', 'amount', 'organization'])
def test_review_rejects_same_numeric_stem_with_conflicting_identity(env, monkeypatch, mismatch):
    base, root = env
    original = put(base, document(date='21/07/2026'), '21_07_26__tag_828489o.pdf')
    copied = put(base, document(variant='copy', date='22/07/2026' if mismatch == 'date' else '21/07/2026',
                               amount='200' if mismatch == 'amount' else '100'), '21_07_26__tag_828489c.pdf')
    if mismatch == 'organization':
        def text(path):
            raw = synthetic_text(path.read_bytes())
            return raw.replace('580000001', '580000002') if path == copied else raw
        for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
            monkeypatch.setattr(f'src.receipt_summary.{reader}', text)
    saved(env)
    path = report(base)
    before = path.read_bytes()
    with closing(openpyxl.load_workbook(path)) as wb:
        ids = {ws.cell(row, headers(ws)['שם קובץ']).value: ws.cell(row, headers(ws)[ID_HEADER]).value
               for ws, row in rows(wb)}
        assert len(ids) == 2
    with pytest.raises(sync_state.SyncError, match='consistent identity') as failure:
        sync_state.review_identifiers(base/'receipts', 'primary', root, 2026,
            oc_pairs=[(ids[original.name], ids[copied.name])], reports_dir=base/'reports')
    assert failure.value.code == f'oc_pair_{mismatch}_conflict'
    assert path.read_bytes() == before



def test_different_sha_collision_alias_blocks_review(env):
    base, root = env
    original = put(base, document(date='21/07/2026'), '21_07_26__tag_828489o.pdf')
    copied = put(base, document(date='21/07/2026', variant='copy'), '21_07_26__tag_828489c.pdf')
    alias = put(base, document(number='1002', date='21/07/2026', variant='alias'), '21_07_26__tag_828489c__2.pdf')
    saved(env)
    path = report(base)
    # Prevent the synthetic auto-pair from consuming the intended publications
    # by using the explicit ownership verifier with an ambiguous C entry.
    from src.sync_workbook import reviewed_oc_pair
    state = json.loads((root/'sync-primary.json').read_bytes())
    with closing(openpyxl.load_workbook(path)) as wb:
        ws, row = rows(wb)[0]
        row_map = {'original': (ws, row), 'copy': (ws, row)}
        entries = {
            'original': {'documents': [sync_state.digest(original.read_bytes())]},
            'copy': {'documents': [sync_state.digest(copied.read_bytes()), sync_state.digest(alias.read_bytes())]},
        }
        with pytest.raises(sync_state.SyncError, match='one verified numeric O/C source'):
            reviewed_oc_pair(row_map, entries, state, base/'receipts/primary', 'original', 'copy')
    assert original.is_file() and copied.is_file() and alias.is_file()
