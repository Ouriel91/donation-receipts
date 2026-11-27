"""Focused annual closure; synthetic sources and transport, actual persistence."""
import json
from pathlib import Path
from urllib.parse import unquote,urlparse
import openpyxl
import pytest
from openpyxl.comments import Comment
from tests.test_sync import env,document,put,mail,report,rows,total
from tests.test_report2026_core import existing,saved
from src import sync
from src.sync_workbook import headers,ARCHIVE,ARCHIVE_AUDIT,INDEX,LIVE_HEADERS,_row_fingerprint
from src.sync_state import SyncError
from src.report_layout import _apply_primary_layout,_report_field_columns,_has_primary_layout

def archive(env,version=1):
    existing(env,('1001','9999'));saved(env)
    path=report(env[0]);wb=openpyxl.load_workbook(path);ws,_=rows(wb)[0];col=headers(ws)[ARCHIVE]
    alias=next(r for r in range(2,ws.max_row+1) if ws.cell(r,col).value)
    proof=json.loads(ws.cell(alias,col).value)
    if version==1:ws.cell(alias,col,json.dumps({k:proof[k] for k in ('primary','fingerprint')}))
    wb.save(path);wb.close();return ws.title,alias

def test_v1_known_default_mutation_is_proven_backed_up_and_idempotent(env):
    title,alias=archive(env);path=report(env[0]);wb=openpyxl.load_workbook(path);ws=wb[title]
    status=ws.cell(alias,headers(ws)[LIVE_HEADERS[0]]);status.value=None
    col=headers(ws)[ARCHIVE];proof=json.loads(ws.cell(alias,col).value)
    proof['fingerprint']=_row_fingerprint(ws,alias);ws.cell(alias,col,json.dumps(proof))
    status.value='לא ידוע / דורש בירור';wb.save(path);wb.close();before=path.read_bytes()
    saved(env);wb=openpyxl.load_workbook(path);ws=wb[title]
    proof=json.loads(ws.cell(alias,headers(ws)[ARCHIVE]).value)
    assert proof['version']==2 and proof['fingerprint']==_row_fingerprint(ws,alias)
    audit=[json.loads(r[1]) for r in wb[ARCHIVE_AUDIT].iter_rows(min_row=2,values_only=True)]
    assert any(r['reference']=='exact_v1_default_status_reconstruction' for r in audit)
    assert len(rows(wb))==1 and total(wb)==100;wb.close()
    assert any(p.read_bytes()==before for p in (env[1]/'report-backups').rglob('*.xlsx'))
    saved(env);wb=openpyxl.load_workbook(path);assert len(rows(wb))==1;wb.close()

def test_layout_cannot_mutate_archive_without_explicit_skip(env):
    title,alias=archive(env,2);wb=openpyxl.load_workbook(report(env[0]));ws=wb[title]
    ws.cell(alias,headers(ws)[LIVE_HEADERS[0]]).value=None
    before=_row_fingerprint(ws,alias);_apply_primary_layout(ws)
    assert _row_fingerprint(ws,alias)==before;wb.close()

def test_unexplained_v1_mutation_stops_without_before_state(env):
    title,alias=archive(env);path=report(env[0]);wb=openpyxl.load_workbook(path);ws=wb[title]
    ws.cell(alias,headers(ws)['הערות'],'unexplained change');wb.save(path);wb.close();before=path.read_bytes()
    with pytest.raises(SyncError) as exc:saved(env)
    assert exc.value.code=='historical_archive_reference_missing' and path.read_bytes()==before

def test_v2_manual_note_is_preserved_and_audited_without_resurrection(env):
    title,alias=archive(env,2);path=report(env[0]);wb=openpyxl.load_workbook(path);ws=wb[title]
    ws.cell(alias,headers(ws)['הערות'],'Intentional archive note').comment=Comment('Keep','Tester')
    wb.save(path);wb.close();saved(env);saved(env);wb=openpyxl.load_workbook(path);ws=wb[title]
    assert ws.cell(alias,headers(ws)['הערות']).value=='Intentional archive note'
    assert ws.cell(alias,headers(ws)['הערות']).comment.text=='Keep'
    audit=[json.loads(r[1]) for r in wb[ARCHIVE_AUDIT].iter_rows(min_row=2,values_only=True)]
    assert any(r['manual_review'] for r in audit) and len(rows(wb))==1;wb.close()

@pytest.mark.parametrize('mutation',['amount','source','snapshot'])
def test_identity_source_or_proof_corruption_still_blocks_atomically(env,mutation):
    title,alias=archive(env,2);path=report(env[0]);wb=openpyxl.load_workbook(path);ws=wb[title]
    if mutation=='amount':ws.cell(alias,headers(ws)['סכום'],999)
    elif mutation=='source':
        other=put(env[0],document('2002','250'),filename='different.pdf')
        ws.cell(alias,headers(ws)['שם קובץ']).hyperlink=other.as_uri()
    else:
        col=headers(ws)[ARCHIVE];proof=json.loads(ws.cell(alias,col).value);proof['snapshot'][0][1]='tamper';ws.cell(alias,col,json.dumps(proof))
    wb.save(path);wb.close();before=path.read_bytes()
    with pytest.raises(SyncError):saved(env)
    assert path.read_bytes()==before

def annual(env):
    base,root,provider,_=env
    return sync.run_sync(provider=provider,receipts_dir=base/'receipts',reports_dir=base/'reports',accounts_dir=base/'accounts',manifest_path=base/'manifest.json',account='primary',year=2026,root=root,full_report=True)

def test_new_real_pdf_annual_and_local_use_same_core_without_repeat_growth(env,monkeypatch):
    import pymupdf
    from src import pdf_text_extractor as readers
    base,root,provider,_=env
    pdf=pymupdf.open();page=pdf.new_page()
    page.insert_htmlbox(page.rect,'עמותה סינתטית<br>עמותה רשומה: 580000001<br>קבלה מספר: 1001<br>10/05/2026<br>₪100<br>Donation receipt')
    raw=pdf.tobytes();pdf.close()
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf',readers.extract_text_from_pdf)
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf',readers.extract_text_from_pdf_pymupdf)
    monkeypatch.setattr('src.pdf_donation_validator.extract_text_from_pdf_bytes',readers.extract_text_from_pdf_bytes)
    mail(provider,raw);first=annual(env)
    assert first['documents_extracted']==1 and first['catalog_documents']==1
    assert provider.download_attachment.call_count==1
    def inspect():
        wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1
        ws,row=rows(wb)[0];cols=_report_field_columns(ws)
        assert _has_primary_layout(ws) and ws.sheet_view.rightToLeft
        assert ws.cell(row,cols['organization_name']).value=='השם הרשמי הסינתטי'
        assert ws.cell(row,cols['section46_status']).value=='מאושר'
        assert ws.cell(row,cols['severity']).value=='הכי טוב'
        visible=next(c.column for c in ws[1] if c.value=='סטטוס הקבלה' and not ws.column_dimensions[c.column_letter].hidden)
        assert ws.cell(row,visible).value=='הכי טוב' and ws.cell(row,visible).data_type!='f'
        cell=ws.cell(row,cols['file_name']);target=cell.hyperlink.target
        assert '\\' not in target and Path(unquote(urlparse(target).path)).is_file()
        assert cell.font.sz==11 and not cell.alignment.shrinkToFit
        result=(ws.title,total(wb),ws.cell(row,cols['organization_name']).value,ws.cell(row,cols['severity']).value,target)
        wb.close();return result
    original=inspect();second=annual(env)
    assert second['documents_extracted']==0 and second['documents_reused']==1
    assert provider.download_attachment.call_count==1 and inspect()==original
    local=saved(env);assert local['documents_extracted']==0 and inspect()==original

def test_downloaded_uncataloged_source_is_extracted_without_gmail(env):
    base,_,provider,_=env;put(base,document());result=saved(env)
    assert result['documents_extracted']==1 and not provider.fetch_emails.called
    result=saved(env);assert result['documents_extracted']==0 and result['documents_reused']==1

@pytest.mark.parametrize('receipt,status,expected',[('1001','approved','הכי טוב'),('','approved','טוב'),('1001','denied','לא זכאית'),('1001','unknown','דורשת בדיקה')])
def test_classification_uses_dated_evidence_and_preserves_manual(env,receipt,status,expected):
    from src.sync_workbook import classify_receipts
    from unittest.mock import MagicMock
    existing(env);saved(env);wb=openpyxl.load_workbook(report(env[0]));ws,row=rows(wb)[0];cols=_report_field_columns(ws)
    entries={k:json.loads(v) for k,v in wb[INDEX].iter_rows(min_row=2,max_col=2,values_only=True)}
    key=ws.cell(row,headers(ws)['Sync donation ID']).value;ws.cell(row,cols['receipt_number']).value=receipt
    registry=MagicMock();registry.official_identity.return_value={'official_name':'verified'}
    registry.lookup_tax.return_value={'section46_status':status,'stale':False,'conflict':False}
    classify_receipts(wb,entries,{key:(ws,row)},registry)
    assert ws.cell(row,cols['severity']).value==expected
    assert registry.lookup_tax.call_args.args[1].isoformat()=='2026-01-15'
    ws.cell(row,cols['severity'],'Intentional manual classification').comment=Comment('Manual','Tester')
    ws.cell(row,cols['notes'],'Manual notes');classify_receipts(wb,entries,{key:(ws,row)},registry)
    assert ws.cell(row,cols['severity']).value=='Intentional manual classification'
    assert ws.cell(row,cols['notes']).value=='Manual notes';wb.close()

def test_first_occurrence_completes_missing_number_date_and_stays_first(env):
    existing(env,('','1001'));path=report(env[0]);wb=openpyxl.load_workbook(path);ws,row=rows(wb)[0];cols=headers(ws)
    ws.cell(row,cols['תאריך']).value=None;wb.save(path);wb.close();saved(env);saved(env)
    wb=openpyxl.load_workbook(path);active=rows(wb);assert len(active)==1 and active[0][1]==2
    from src.sync_workbook import metadata
    meta=metadata(*active[0]);assert meta.receipt_number=='1001' and meta.receipt_date=='15/01/2026' and total(wb)==100;wb.close()

def test_readable_existing_hebrew_alias_wins_over_identical_gibberish(env):
    existing(env);base=env[0];original=next((base/'receipts').rglob('*.pdf'))
    proper=original.with_name('קבלה.pdf');original.rename(proper)
    broken=proper.with_name(proper.name.encode().decode('latin1'));broken.write_bytes(proper.read_bytes())
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0];cell=ws.cell(row,headers(ws)['שם קובץ'])
    cell.value=broken.name;cell.hyperlink='file:///'+str(broken).lstrip('/').replace('/','\\')
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));ws,row=rows(wb)[0];cell=ws.cell(row,headers(ws)['שם קובץ'])
    assert cell.value==proper.name and cell.hyperlink.target==proper.as_uri();wb.close()

def test_v1_changed_manual_fields_migrate_only_from_verified_backup(env):
    from src.sync_state import digest
    title,alias=archive(env);path=report(env[0]);before=path.read_bytes()
    folder=env[1]/'report-backups/primary/2026';folder.mkdir(parents=True,exist_ok=True)
    (folder/('.backup-'+digest(before)+'.xlsx')).write_bytes(before)
    wb=openpyxl.load_workbook(path);ws=wb[title]
    ws.cell(alias,headers(ws)['הערות'],'Preserved manual amendment');wb.save(path);wb.close()
    saved(env);wb=openpyxl.load_workbook(path)
    audit=[json.loads(r[1]) for r in wb[ARCHIVE_AUDIT].iter_rows(min_row=2,values_only=True)]
    assert any(r['reference'].startswith('verified_backup:') and r['manual_review'] for r in audit)
    assert wb[title].cell(alias,headers(wb[title])['הערות']).value=='Preserved manual amendment'
    assert len(rows(wb))==1;wb.close();saved(env)

def test_refresh_discovers_new_local_source_and_reports_extraction_once(env):
    base,_,provider,_=env;put(base,document());assert saved(env)['documents_extracted']==1
    put(base,document('2002','75','15/02/2026'),filename='new-local.pdf')
    result=saved(env)
    assert result['documents_extracted']==1 and result['documents_reused']==1 and result['source_pdfs_scanned']==2
    assert not provider.fetch_emails.called
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==2 and total(wb)==175;wb.close()
    assert saved(env)['documents_extracted']==0

def test_operational_summary_contains_observed_counts_only(env,capsys):
    from src.logger import RunDiagnostics
    base,_,provider,_=env;mail(provider,document())
    with RunDiagnostics(log_dir=base/'private-logs') as diagnostics:
        result=annual(env);diagnostics.done(result,'donation_summary_2026.xlsx')
    output=capsys.readouterr().out
    for text in ('messages_total=1','messages_scanned=1','new_files=1','source_pdfs_scanned=1',
                 'documents_extracted=1','documents_reused=0','catalog_documents=1','merged_copies=0',
                 'registry_matches=1','approved_rows=1','classified_best=1','links_fixed=0',
                 'links_not_found=0','links_conflicts=0'):
        assert text in output,text
    assert 'עמותה סינתטית' not in output and 'new-message' not in output
