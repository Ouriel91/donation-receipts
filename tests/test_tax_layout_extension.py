from dataclasses import asdict
from pathlib import Path
import hashlib
from types import SimpleNamespace
import openpyxl
import pytest
from src import receipt_metadata_extractor as me, receipt_summary as rs, sync_state as ss, sync_workbook as sw
from src.pdf_text_extractor import extract_tax_layout
from src.hebrew_text_normalizer import normalize_hebrew_text

LABEL='המיסים לרשות התרומה דיווח אישור מספר'
NOTE=me.IDENTIFIER_REVIEW_NOTES['tax_report_number']

def fields(joined,bold):
    raw=f'{joined} : {LABEL}'
    other=f'{bold}\n: {LABEL}'
    return (raw,normalize_hebrew_text(raw),other,normalize_hebrew_text(other))

@pytest.mark.parametrize('joined,bold',[('2025','2025'),('112345','12345'),('234567','34567'),('105678','05678')])
def test_label_font_and_reader_evidence(joined,bold):
    m=me.ReceiptMetadata(notes=NOTE)
    me.resolve_identifier_evidence(m,*fields(joined,bold),tax_layout=[(joined,bold)])
    assert m.tax_report_number==bold and m.tax_layout_verified and NOTE not in m.notes

@pytest.mark.parametrize('label',[f'2025 : {LABEL}','מספר אישור דיווח התרומה לרשות המיסים: 2025'])
def test_explicit_year(label):
    assert me._find_full_tax_number(label)=='2025'
    assert me._find_full_tax_number('תאריך: 01/01/2025') is None
    assert me._find_full_tax_number('2025') is None

@pytest.mark.parametrize('extra,layout',[
    (f'99999 : {LABEL}', [('112345','12345')]),
    ('', [('112345','12345'),('234567','34567')]),
    ('', [('112345','')]),
    ('', []),
])
def test_true_conflicts_not_suppressed(extra,layout):
    m=me.ReceiptMetadata(notes=NOTE)
    me.resolve_identifier_evidence(m,*fields('112345','12345'),extra,tax_layout=layout)
    assert not m.tax_report_number and not m.tax_layout_verified and NOTE in m.notes

def test_bold_alone_is_not_enough():
    m=me.ReceiptMetadata()
    me.resolve_identifier_evidence(m,f'112345 : {LABEL}',tax_layout=[('112345','12345')])
    assert m.tax_report_number=='112345' and not m.tax_layout_verified

def test_all_bold_ordinary_unchanged():
    m=me.ReceiptMetadata()
    me.resolve_identifier_evidence(m,f'12345 : {LABEL}',tax_layout=[('12345','12345')])
    assert m.tax_report_number=='12345' and not m.tax_layout_verified

@pytest.mark.parametrize('spans,expected',[
    ([('1',0),('12345',16),(' : '+LABEL,0)],(('112345','12345'),)),
    ([('2025',16),(' : '+LABEL,0)],(('2025','2025'),)),
    ([('12',16),('3',0),('45',16),(' : '+LABEL,0)],(('12345',''),)),
    ([('12345',16),(' : תאריך',0)],()),
    ([('מספר אישור דיווח התרומה לרשות המיסים: ',0),('1',0),('12345',16)],(('112345','12345'),)),
])
def test_physical_spans(monkeypatch,spans,expected):
    import pymupdf
    class Doc(list):
        def __enter__(self):return self
        def __exit__(self,*a):pass
    page=SimpleNamespace(get_text=lambda *a:{'blocks':[{'lines':[{'spans':[{'text':t,'flags':f} for t,f in spans]}]}]})
    monkeypatch.setattr(pymupdf,'open',lambda *a:Doc([page]))
    assert extract_tax_layout('synthetic.pdf')==expected

@pytest.mark.parametrize('mode',['normal','comment','manual','formula','approved','hash','unowned','multi','wrong_amount','wrong_year','managed'])
def test_repair_publication(tmp_path,monkeypatch,mode):
    base=tmp_path/'receipts'/'primary';base.mkdir(parents=True)
    raw=b'%PDF-synthetic';sha=hashlib.sha256(raw).hexdigest();p=base/(sha+'.pdf');p.write_bytes(raw)
    old=me.ReceiptMetadata(registration_number='580000001',receipt_number='123456',receipt_date='14/04/2025',amount='100',notes=NOTE)
    fresh=me.ReceiptMetadata(**{**asdict(old),'tax_report_number':'12345','tax_layout_verified':True,'notes':''})
    if mode=='wrong_amount':fresh.amount='101'
    monkeypatch.setattr(ss,'_extract_row',lambda *a,**k:fresh)
    monkeypatch.setattr(rs,'_extract_row',lambda *a,**k:fresh)
    item={'metadata':asdict(old),'paths':[p.name],'logical_id':'d'}
    state={'documents':{sha:item}}
    if mode=='approved':item['verified_identifier_updates']={'tax_report_number':{'value':''}}
    ss.stage_saved_tax_layout(state,base.parent,'primary',None,{2024 if mode=='wrong_year' else 2025})
    assert not item['metadata']['tax_report_number']
    wb=openpyxl.Workbook();ws=wb.active;ws.title='אפריל'
    fs=['registration_number','receipt_number','receipt_date','amount','tax_report_number']
    ws.append([sw.COLUMN_HEADERS_HE[f] for f in fs]);ws.append([getattr(old,f) or None for f in fs]);cell=ws.cell(2,5)
    entry={'documents':[sha],'owned_source_fields':mode!='unowned','managed':{}}
    if mode=='comment':cell.comment=openpyxl.comments.Comment('keep','test')
    if mode=='manual':cell.value='67890'
    if mode=='formula':cell.value='=1+2'
    if mode=='hash':p.write_bytes(b'changed')
    if mode=='multi':entry['documents'].append('other')
    if mode=='managed':entry['managed'][sw.COLUMN_HEADERS_HE['tax_report_number']]='old'
    before=cell.value
    changed=sw.apply_verified_tax_layout({'d':entry},{'d':(ws,2)},state,base,2025)
    if mode=='normal':
        assert changed==1 and cell.value=='12345' and item['metadata']['tax_report_number']=='12345'
        assert NOTE not in item['metadata']['notes']
        assert sw.apply_verified_tax_layout({'d':entry},{'d':(ws,2)},state,base,2025)==0
    else:assert changed==0 and cell.value==before and item['metadata']['tax_report_number']==''
