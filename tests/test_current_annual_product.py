"""One production pipeline, actual PDF readers, controlled Gmail/HTTP boundaries."""
import json
from copy import copy
from pathlib import Path
from urllib.parse import urlparse,unquote
import openpyxl
import pytest
from openpyxl.comments import Comment
from tests.test_sync import env,mail,report,rows,total
from tests.test_report2026_core import saved,existing
from tests.test_annual2026_integrated import annual
from src.sync_workbook import headers,ARCHIVE,INDEX
from src.report_layout import _has_primary_layout,_report_field_columns

def test_annual_all_requirements_with_real_pdf_and_unusable_archive(env,monkeypatch):
    import pymupdf
    from src import pdf_text_extractor as readers
    base,root,provider,_=env
    from src import main as cli, sync as engine
    observed=[];actual=engine.run_sync
    def capture(**kw):
        result=actual(**kw);observed.append(result);return result
    monkeypatch.setattr(engine,'run_sync',capture)
    from types import SimpleNamespace
    scope = SimpleNamespace(
        user='alpha', project=base, receipts=base/'receipts', reports=base/'reports',
        accounts=base/'accounts', runtime=root, registry=root/'organizations.json',
        identity_scope=lambda account: f'alpha/{account}', manifest=base/'manifest.json', require=lambda: None, require_account=lambda _: None,
    )
    monkeypatch.setattr(cli.UserScope, 'open', lambda _: scope)
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT',str(root))
    queries=[]
    def gmail(*args,**kw):
        queries.append(kw['query']);return provider
    monkeypatch.setattr(cli,'GmailProvider',gmail)
    def run_annual():
        monkeypatch.setattr('sys.argv',['product','--user','alpha','--mode','full','--account','primary','--year','2026'])
        cli.main()
        assert 'after:' in queries[-1] and 'before:' in queries[-1]
        return observed[-1]
    for module,attr,fn in [('src.receipt_summary','extract_text_from_pdf',readers.extract_text_from_pdf),
       ('src.receipt_summary','extract_text_from_pdf_pymupdf',readers.extract_text_from_pdf_pymupdf),
       ('src.pdf_donation_validator','extract_text_from_pdf_bytes',readers.extract_text_from_pdf_bytes)]:
        monkeypatch.setattr(module+'.'+attr,fn)
    def pdf(number,date):
        doc=pymupdf.open();page=doc.new_page()
        page.insert_htmlbox(page.rect,f'עמותה סינתטית<br>עמותה רשומה: 580000001<br>קבלה מספר: {number}<br>{date}<br>₪100<br>Donation receipt')
        raw=doc.tobytes();doc.close();return raw
    mail(provider,pdf('1001','10/01/2026'));assert run_annual()['documents_extracted']==1
    wb=openpyxl.load_workbook(report(base));ws,r=rows(wb)[0];cols=headers(ws)
    ws.cell(r,cols['הערות'],'Intentional manual note').comment=Comment('Keep','Tester')
    for cell in list(ws[r]):
        target=ws.cell(r+1,cell.column,cell.value);target._style=copy(cell._style)
        target.hyperlink=copy(cell.hyperlink);target.comment=copy(cell.comment)
    ws.cell(r+1,cols['מספר קבלה']).value=None
    source=next((base/'receipts').rglob('*.pdf'));proper=source.with_name('קבלה.pdf');source.rename(proper)
    broken=proper.with_name(proper.name.encode().decode('latin1'))
    for rr in (r,r+1):
        cell=ws.cell(rr,cols['שם קובץ']);cell.value=broken.name
        cell.hyperlink='file:///'+str(broken).lstrip('/').replace('/','\\')
    wb.save(report(base));wb.close();saved(env)
    wb=openpyxl.load_workbook(report(base));assert len(rows(wb))==1
    ws,r=rows(wb)[0];cols=headers(ws)
    archived=next(rr for rr in range(2,ws.max_row+1) if ws.cell(rr,cols[ARCHIVE]).value)
    # The reported failure: no old before-state can prove this audit marker.
    # Annual generation must not consult it, parse it, or migrate it.
    marker='{"fingerprint":"no-reference-exists","primary":"old-audit-only"}'
    ws.cell(archived,cols[ARCHIVE],marker)
    ws.cell(archived,cols['הערות'],'Retained archive manual amendment')
    wb.save(report(base));wb.close()
    def deny_backup_scan(path,*args,**kw):
        if 'report-backups' in path.parts:pytest.fail('Archive before-state lookup is forbidden')
        return original_glob(path,*args,**kw)
    original_glob=Path.glob;monkeypatch.setattr(Path,'glob',deny_backup_scan)
    provider.fetch_emails.side_effect=None
    mail(provider,pdf('2002','15/02/2026'),msg='second')
    result=run_annual();assert result['documents_extracted']==1 and result['published']==1
    def check():
        wb=openpyxl.load_workbook(report(base));active=rows(wb)
        assert len(active)==2 and total(wb)==200
        for sheet,row in active:
            cols=_report_field_columns(sheet)
            assert _has_primary_layout(sheet) and sheet.sheet_view.rightToLeft
            assert sheet.cell(row,cols['organization_name']).value=='השם הרשמי הסינתטי'
            assert sheet.cell(row,cols['section46_status']).value=='מאושר'
            assert sheet.cell(row,cols['severity']).value=='הכי טוב'
            cell=sheet.cell(row,cols['file_name']);assert cell.font.sz==11 and not cell.alignment.shrinkToFit
            assert '\\' not in cell.hyperlink.target
            assert Path(unquote(urlparse(cell.hyperlink.target).path)).is_file()
        january=wb['ינואר'];c=headers(january)
        assert january.cell(2,c['מספר קבלה']).value=='1001'
        assert january.cell(2,c['שם קובץ']).value=='קבלה.pdf'
        assert january.cell(2,c['הערות']).value=='Intentional manual note'
        assert january.cell(2,c['הערות']).comment.text=='Keep'
        assert january.cell(archived,c[ARCHIVE]).value==marker
        assert january.cell(archived,c['הערות']).value=='Retained archive manual amendment'
        assert january.row_dimensions[archived].hidden
        wb.close()
    check();downloads=provider.download_attachment.call_count
    assert run_annual()['documents_extracted']==0;check()
    assert provider.download_attachment.call_count==downloads
    saved(env);check()
    assert list(report(base).parent.iterdir())==[report(base)]

@pytest.mark.parametrize('marker',['not-json',None,'{"version":2,"snapshot":"invalid"}'])
def test_current_index_retains_audit_without_proof_validation(env,marker):
    existing(env,('1001','9999'));saved(env)
    path=report(env[0]);wb=openpyxl.load_workbook(path);ws,r=rows(wb)[0];cols=headers(ws)
    alias=next(rr for rr in range(2,ws.max_row+1) if ws.cell(rr,cols[ARCHIVE]).value)
    ws.cell(alias,cols[ARCHIVE]).value=marker
    wb.save(path);wb.close();saved(env);saved(env)
    wb=openpyxl.load_workbook(path);assert len(rows(wb))==1 and total(wb)==100
    assert wb[ws.title].cell(alias,headers(wb[ws.title])[ARCHIVE]).value==marker;wb.close()

def test_unowned_marker_does_not_silently_hide_current_data(env):
    existing(env,('1001','9999'));saved(env)
    path=report(env[0]);wb=openpyxl.load_workbook(path)
    payload=wb[INDEX]['B2'];item=json.loads(payload.value);item['historical_aliases']=[];payload.value=json.dumps(item)
    wb.save(path);wb.close();saved(env)
    wb=openpyxl.load_workbook(path);assert len(rows(wb))==1 and total(wb)==100
    assert any(c.value for ws in wb for c in ws[1] if c.value=='Retained duplicate audit');wb.close()
