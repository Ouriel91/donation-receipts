"""Synthetic reproductions of 2026 production QA, no private workbook or Gmail."""
from copy import copy
from pathlib import Path
import json

import openpyxl
import pytest
from openpyxl.comments import Comment

from tests.test_sync import env, initial_workbook, document, report, rows, total
from src import sync
from src.sync_state import catalog_documents
from src.sync_workbook import (bootstrap, write_index, headers, donation_sheets, INDEX,
    ARCHIVE, ID_HEADER, LIVE_HEADERS, OFFICIAL, ORIGINAL_NAME, SOURCES)
from src.report_layout import _has_primary_layout, _DISPLAY_MARKER


def saved(env, **kwargs):
    base, root, provider, _ = env
    provider.fetch_emails.side_effect = AssertionError('Unexpected Gmail scan')
    return sync.run_sync(provider=None, receipts_dir=base/'receipts', reports_dir=base/'reports',
        accounts_dir=base/'accounts', manifest_path=base/'manifest.json', account='primary', year=2026,
        root=root, full_report=True, resume_pending=True, refresh_base=True, **kwargs)


def existing(env, receipt_ids=('1001',), *, indexed=True):
    base, root, _, _ = env
    initial_workbook(env)
    _, state = catalog_documents(base/'receipts', 'primary', root, None)
    wb = openpyxl.load_workbook(report(base)); ws = next(donation_sheets(wb)); cols = headers(ws)
    ws.cell(2, cols['סכום'], 100)
    raw_name = next(iter(state['documents'].values()))['metadata']['organization_name']
    ws.cell(2, cols['שם עמותה'], raw_name)
    ws.cell(2, cols['הערות']).value = None
    for row, receipt in enumerate(receipt_ids, 2):
        if row > 2:
            for cell in list(ws[2]):
                out = ws.cell(row,cell.column,cell.value)
                out._style=copy(cell._style);out.hyperlink=copy(cell.hyperlink);out.comment=copy(cell.comment)
        ws.cell(row,cols['מספר קבלה'],receipt)
    entries, mapping, _, _ = bootstrap(wb, report(base), base/'receipts/primary','primary',state)
    if indexed:
        for key, entry in entries.items():
            entry['managed'] = {'שם עמותה': raw_name}
            entry['original_extracted_name'] = raw_name
        write_index(wb, entries)
        wb[INDEX]['D1'] = 'Full report layout v1'
    else:
        if INDEX in wb: del wb[INDEX]
        # bootstrap IDs without index are ignored by the legacy bootstrap.
    wb.save(report(base)); wb.close()
    return raw_name


@pytest.mark.parametrize('indexed', [False, True])
def test_same_proven_pdf_different_extracted_receipt_ids_merge_visibly(env, indexed):
    base, _, _, _ = env
    existing(env, ('1001','9999','5555'), indexed=indexed)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    ws.cell(3,cols['שם עמותה'],'Manual chosen name').comment=Comment('Intentional edit','Tester')
    ws.cell(1,ws.max_column+1,'Manual formula');ws.cell(3,ws.max_column,'=2+3')
    wb.save(report(base));wb.close()
    saved(env)
    wb=openpyxl.load_workbook(report(base));active=rows(wb)
    assert len(active)==1 and total(wb)==100
    ws,row=active[0];cols=headers(ws)
    assert ws.cell(row,cols['שם עמותה']).value=='Manual chosen name'
    assert ws.cell(row,cols['Manual formula']).value=='=2+3'
    assert sum(bool(ws.row_dimensions[r].hidden) for r in range(2,5))==2
    assert all(ws.cell(r,cols[ARCHIVE]).value for r in range(2,5) if r!=row)
    assert _has_primary_layout(ws)
    wb.close()
    # A second run reconstructs a base locally and must validate retained archives.
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1 and total(wb)==100;wb.close()


def test_distinct_receipts_in_one_pdf_are_retained_for_review(env, monkeypatch):
    base, _, _, _=env
    existing(env,('1001','2002'))
    from tests.test_sync import synthetic_text
    def multi(path):
        return synthetic_text(path.read_bytes())+'\nקבלה מס 2002\nתאריך 15/01/2026\nסה"כ ₪100\n'
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf',multi)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf',multi)
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==2 and total(wb)==200
    assert all(not ws.row_dimensions[row].hidden for ws,row in rows(wb))
    assert all('בדיקה' in str(ws.cell(row,headers(ws)['הערות']).value) for ws,row in rows(wb));wb.close()


def test_same_filename_different_source_bytes_do_not_merge(env):
    from tests.test_sync import put
    base, _, _, _=env
    existing(env,('1001','2002'))
    second=put(base,document('2002'),arrival='02_February',filename='old.pdf')
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    # Source IDs are index authority. Give second row its separately proven PDF.
    doc=__import__('hashlib').sha256(second.read_bytes()).hexdigest()
    key=ws.cell(3,cols[ID_HEADER]).value
    payload=next(r for r in wb[INDEX].iter_rows(min_row=2) if r[0].value==key)
    entry=json.loads(payload[1].value);entry['documents']=[doc];payload[1].value=json.dumps(entry)
    ws.cell(3,cols['שם קובץ']).hyperlink=second.as_uri()
    wb.save(report(base));wb.close()
    base_root=env[1];catalog_documents(base/'receipts','primary',base_root,None)
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==2;wb.close()


@pytest.mark.parametrize('ownership', ['managed','raw','manual','comment'])
def test_official_name_promoted_only_for_proven_automatic_text(env, ownership):
    base, _, _, _=env
    raw=existing(env)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    ws.cell(1,ws.max_column+1,OFFICIAL);ws.cell(2,ws.max_column,'השם הרשמי הסינתטי')
    if ownership=='raw':
        entrycell=wb[INDEX]['B2'];entry=json.loads(entrycell.value);entry['managed']={};entrycell.value=json.dumps(entry)
    elif ownership=='manual': ws.cell(2,cols['שם עמותה'],'Manual different name')
    elif ownership=='comment': ws.cell(2,cols['שם עמותה']).comment=Comment('Preserve intentional correction','Tester')
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0];cols=headers(ws)
    expected='Manual different name' if ownership=='manual' else raw if ownership=='comment' else 'השם הרשמי הסינתטי'
    assert ws.cell(row,cols['שם עמותה']).value==expected
    assert ws.cell(row,cols[ORIGINAL_NAME]).value==raw
    assert ws.cell(row,cols[OFFICIAL]).value=='השם הרשמי הסינתטי'
    wb.close()


@pytest.mark.parametrize('prior', [None,'מאושר','Manual verified status'])
def test_section46_blank_fills_existing_verified_and_manual_survive(env, prior):
    base, _, _, _=env
    existing(env)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb))
    col=ws.max_column+1;ws.cell(1,col,LIVE_HEADERS[0]);ws.cell(2,col,prior)
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    assert ws.cell(row,headers(ws)[LIVE_HEADERS[0]]).value==(prior or 'מאושר')
    wb.close()


def test_section46_insufficient_evidence_is_unknown_not_blank(env):
    base, root, _, network=env
    existing(env)
    # Existing transport returns insufficient service evidence. Keep the real API
    # adapter and validation; never turn a missing period into approval.
    network.return_value=json.dumps({'Status':200,'Message':{}}).encode()
    saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    label=ws.cell(row,headers(ws)[LIVE_HEADERS[0]]).value
    assert label and label!='מאושר';wb.close()


def test_file_uri_backslashes_and_mojibake_are_repaired_in_active_project(env):
    base, _, _, _=env
    existing(env)
    original=next((base/'receipts').rglob('*.pdf'));proper=original.with_name('קבלה.pdf');original.rename(proper)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    broken=proper.with_name(proper.name.encode().decode('latin1'))
    target='file:///'+str(broken).lstrip('/').replace('/','\\')
    cell=ws.cell(2,cols['שם קובץ']);cell.value=broken.name;cell.hyperlink=target
    for row in wb['קובצי מקור'].iter_rows(min_row=2): row[2].value=broken.name;row[2].hyperlink=target
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0];cell=ws.cell(row,headers(ws)['שם קובץ'])
    assert cell.value==proper.name and cell.hyperlink.target==proper.as_uri()
    assert cell.font.sz==11 and not cell.alignment.shrinkToFit
    assert all(r[2].hyperlink.target==proper.as_uri() for r in wb['קובצי מקור'].iter_rows(min_row=2))
    wb.close()


def test_generated_visible_section46_is_a_value_and_manual_projection_survives(env):
    base, _, _, _ = env
    existing(env)
    saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    header=next(c for c in ws[1] if c.comment and c.comment.text.startswith(_DISPLAY_MARKER+'section46_status'))
    cell=ws.cell(row,header.column)
    assert cell.value=='מאושר' and cell.data_type!='f'
    cell.value='Manual display decision'
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    assert ws.cell(row,header.column).value=='Manual display decision';wb.close()


def test_one_pdf_with_distinct_currency_values_does_not_prove_a_single_donation(env,monkeypatch):
    base, _, _, _=env
    existing(env,('1001','9999'))
    from tests.test_sync import synthetic_text
    def multi(path): return synthetic_text(path.read_bytes())+'\nתרומה נוספת ₪200\n'
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf',multi)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf',multi)
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==2;wb.close()


def test_exact_copies_within_a_multi_receipt_pdf_merge_without_merging_other_receipt(env,monkeypatch):
    base, _, _, _=env
    existing(env,('1001','9999','2002'))
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb))
    ws.cell(3,headers(ws)['מספר קבלה'],'1001')
    wb.save(report(base));wb.close()
    from tests.test_sync import synthetic_text
    def multi(path): return synthetic_text(path.read_bytes())+'\nקבלה מס 2002\n'
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf',multi)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf',multi)
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==2 and total(wb)==200;wb.close()


def test_same_pdf_without_readable_receipt_evidence_stays_separate(env,monkeypatch):
    base, _, _, _=env
    existing(env,('1001','9999'))
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf',lambda p:'')
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf',lambda p:'')
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==2 and total(wb)==200;wb.close()


def test_native_visible_thirteen_columns_fill_blank_section46(env):
    from src.receipt_summary import COLUMN_HEADERS_HE
    base, _, _, _=env
    existing(env)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    key=ws.cell(2,cols[ID_HEADER]).value
    ws.delete_cols(10,ws.max_column-9)
    for col,label in enumerate((LIVE_HEADERS[0],'מצב דיווח לרשות המסים',COLUMN_HEADERS_HE['severity'],'הערות',ID_HEADER),10):
        ws.cell(1,col,label)
    ws.cell(2,14,key)
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    assert ws['J2'].value=='מאושר' and ws['J2'].data_type!='f' and _has_primary_layout(ws)
    wb.close()


def test_unverified_technical_official_text_is_not_promoted(env):
    base, _, _, _=env
    raw=existing(env)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    ws.cell(2,cols['מספר עמותה'],'580000002')
    col=ws.max_column+1;ws.cell(1,col,OFFICIAL);ws.cell(2,col,'Unverified technical name')
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    assert ws.cell(row,headers(ws)['שם עמותה']).value==raw
    assert ws.cell(row,headers(ws)[OFFICIAL]).value=='Unverified technical name';wb.close()


def test_cli_refreshes_completed_report_from_saved_sources_without_gmail(env,monkeypatch):
    from src import main as cli
    base,root,_,_=env
    existing(env,('1001','9999'))
    from types import SimpleNamespace
    scope = SimpleNamespace(
        user='alpha', project=base, receipts=base/'receipts', reports=base/'reports',
        accounts=base/'accounts', runtime=root, registry=root/'organizations.json',
        identity_scope=lambda account: f'alpha/{account}', manifest=base/'manifest.json', require=lambda: None, require_account=lambda _: None,
    )
    monkeypatch.setattr(cli.UserScope, 'open', lambda _: scope)
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT',str(root))
    monkeypatch.setattr(cli,'GmailProvider',lambda *a,**k:pytest.fail('Gmail provider constructed'))
    monkeypatch.setattr('sys.argv',['receipts','--user','alpha','--mode','full','--account','primary','--year','2026',
                                   '--resume-pending','--refresh-base'])
    cli.main()
    cli.main()  # Prior successful publication must permit an explicit fresh base.
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1 and total(wb)==100;wb.close()
    assert list(report(base).parent.iterdir())==[report(base)]


def test_shared_primary_pdf_merges_rows_while_retaining_other_source_copies(env):
    from tests.test_sync import put
    base,root,_,_=env
    existing(env,('1001','9999'))
    extra=put(base,document(variant='copy'),filename='second-copy.pdf')
    doc=__import__('hashlib').sha256(extra.read_bytes()).hexdigest()
    wb=openpyxl.load_workbook(report(base));payload=wb[INDEX]['B2']
    entry=json.loads(payload.value);entry['documents'].append(doc);payload.value=json.dumps(entry)
    wb.save(report(base));wb.close()
    catalog_documents(base/'receipts','primary',root,None)
    saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1 and total(wb)==100
    entry=json.loads(wb[INDEX]['B2'].value);assert len(entry['documents'])==2
    assert wb[SOURCES].max_row==3;wb.close()


def test_real_pdf_readers_prove_one_receipt_despite_historical_number_noise(env,monkeypatch):
    import pymupdf
    from src import pdf_text_extractor as real
    from src.receipt_summary import generate_summary_workbook
    from tests.test_sync import put
    base,root,_,_=env
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf',real.extract_text_from_pdf)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf',real.extract_text_from_pdf_pymupdf)
    pdf=pymupdf.open();page=pdf.new_page()
    page.insert_htmlbox(page.rect, 'עמותה סינתטית<br>עמותה רשומה: 580000001<br>קבלה מספר: 1001<br>15/01/2026<br>₪100<br>Donation receipt')
    raw=pdf.tobytes();pdf.close();put(base,raw,filename='synthetic.pdf')
    generate_summary_workbook(base/'receipts',base/'reports','primary',2026)
    catalog_documents(base/'receipts','primary',root,None)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));cols=headers(ws)
    for cell in list(ws[2]):
        out=ws.cell(3,cell.column,cell.value);out._style=copy(cell._style);out.hyperlink=copy(cell.hyperlink)
    ws.cell(3,cols['מספר קבלה'],'9999')
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1 and total(wb)==100;wb.close()


@pytest.mark.parametrize('old_hidden_name',[False,True])
def test_official_name_updates_actual_column_c_with_supported_header_alias(env,old_hidden_name):
    base,_,_,_=env
    raw=existing(env)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb))
    ws['C1']='שם העמותה'
    if old_hidden_name:
        col=ws.max_column+1;ws.cell(1,col,'שם עמותה');ws.cell(2,col,'השם הרשמי הסינתטי')
        payload=wb[INDEX]['B2'];entry=json.loads(payload.value);entry['managed']['שם עמותה']='השם הרשמי הסינתטי';payload.value=json.dumps(entry)
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0]
    assert ws.cell(row,3).value=='השם הרשמי הסינתטי' and _has_primary_layout(ws)
    assert ws.cell(row,headers(ws)[ORIGINAL_NAME]).value==raw
    wb.close()


def test_active_pdf_link_is_repaired_and_retained_audit_is_not_rewritten(env):
    base,_,_,_=env
    existing(env,('1001','9999'))
    original=next((base/'receipts').rglob('*.pdf'));proper=original.with_name('קבלה.pdf');original.rename(proper)
    wb=openpyxl.load_workbook(report(base));ws=next(donation_sheets(wb));col=headers(ws)['שם קובץ']
    bad=proper.with_name(proper.name.encode().decode('latin1'))
    target='file:///'+str(bad).lstrip('/').replace('/','\\')
    for row in (2,3): ws.cell(row,col,bad.name).hyperlink=target
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,_=rows(wb)[0]
    assert ws.cell(2,col).hyperlink.target==proper.as_uri() and ws.cell(2,col).value==proper.name
    assert ws.cell(3,col).hyperlink.target==target and ws.cell(3,col).value==bad.name
    wb.close();saved(env)  # Audit history is preserved without gating generation.
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1 and total(wb)==100;wb.close()
