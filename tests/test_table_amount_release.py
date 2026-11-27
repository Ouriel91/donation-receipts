from dataclasses import asdict
from pathlib import Path
import hashlib
import openpyxl
import pytest
from src import sync_workbook as sw
from src.receipt_metadata_extractor import (ReceiptMetadata, _find_amount, table_payment_totals,
    receipt_amounts_in_text, resolve_identifier_evidence, TABLE_AMOUNT_CONFLICT)
from src.receipt_deduplication import receipt_amount

# Synthetic reconstruction of observed reader layouts, no real receipt numbers or PII.
COMPACT = '#קוד מוצרתיאור מוצרסה"כ כמותמחירסה"כ\n1תרומה1200200\nסה"כ ש"ח200'
SPLIT = '#קוד מוצר\nתיאור מוצר\nסה"כ כמות\nמחיר\nסה"כ\n1תרומה1\n200\n200\nסה"כ\n ש"ח200'
@pytest.mark.parametrize('text',[COMPACT,SPLIT])
def test_quantity_is_not_total(text):
    assert _find_amount(text)=='200'
    assert receipt_amounts_in_text(text)=={'200'}
    assert table_payment_totals(text)=={receipt_amount('200')}
    meta=ReceiptMetadata(amount='1')
    resolve_identifier_evidence(meta,text)
    assert meta.amount=='200' and meta.payment_total_verified

@pytest.mark.parametrize('amount',['1','0.50','1,250.75'])
def test_real_small_amount_or_thousands(amount):
    text=COMPACT.replace('ש"ח200','ש"ח'+amount)
    assert receipt_amount(_find_amount(text))==receipt_amount(amount)

def test_no_unrelated_line_or_header_quantity():
    assert not table_payment_totals('סה"כ ש"ח200')
    assert not table_payment_totals(COMPACT.split('\nסה"כ ש"ח')[0])
    assert not table_payment_totals(COMPACT.replace('ש"ח200','ש"ח200 #123'))
    assert not table_payment_totals(COMPACT.replace('ש"ח200','ש"ח0'))

def test_conflicting_readers_do_not_publish_amount():
    meta=ReceiptMetadata(amount='1')
    resolve_identifier_evidence(meta,COMPACT,SPLIT.replace('ש"ח200','ש"ח300'))
    assert meta.amount=='' and not meta.payment_total_verified and TABLE_AMOUNT_CONFLICT in meta.notes

@pytest.mark.parametrize('mode',['normal','comment','manual','formula','override','wrong_hash','retry','unowned'])
def test_cached_amount_repair(tmp_path,monkeypatch,mode):
    from src import receipt_summary
    base=tmp_path/'receipts'/'primary';base.mkdir(parents=True)
    raw=b'fake pdf';sha=hashlib.sha256(raw).hexdigest();p=base/(sha+'.pdf');p.write_bytes(raw)
    meta=ReceiptMetadata(registration_number='580000001',receipt_number='12345',receipt_date='14/04/2025',amount='1')
    fresh=ReceiptMetadata(**{**asdict(meta),'amount':'200','payment_total_verified':True})
    monkeypatch.setattr(receipt_summary,'_extract_row',lambda *a,**k:fresh)
    wb=openpyxl.Workbook();ws=wb.active;ws.title='אפריל'
    fields=['registration_number','receipt_number','receipt_date','amount']
    ws.append([sw.COLUMN_HEADERS_HE[f] for f in fields]);ws.append([meta.registration_number,meta.receipt_number,meta.receipt_date,1])
    entries={'d':{'documents':[sha],'owned_source_fields':mode!='unowned'}}
    item={'metadata':asdict(meta),'paths':[p.name],'table_amount_candidate':{'before':'1','after':'200'}}
    state={'documents':{sha:item}}; cell=ws.cell(2,4)
    if mode=='comment':cell.comment=openpyxl.comments.Comment('manual','test')
    if mode=='manual':cell.value=3
    if mode=='formula':cell.value='=1+2'
    if mode=='override':item['donation_amount_override']='1'
    if mode=='wrong_hash':p.write_bytes(b'wrong')
    if mode=='retry':cell.value=200
    original=cell.value
    n=sw.apply_verified_table_amounts(entries,{'d':(ws,2)},state,base,2025)
    if mode in ('normal','retry'):
        assert cell.value==200 and item['metadata']['amount']=='200'
        assert n==(1 if mode=='normal' else 0)
        assert sw.apply_verified_table_amounts(entries,{'d':(ws,2)},state,base,2025)==0
    else:
        assert n==0 and cell.value==original and item['metadata']['amount']=='1'

@pytest.mark.parametrize('published',[True,False])
def test_standard_catalog_revalidation_once(tmp_path,monkeypatch,published):
    from src import sync_state
    from src.private_runtime import atomic_json
    root=tmp_path/'runtime';root.mkdir()
    base=tmp_path/'receipts'/'primary';folder=base/'2025'/'04_April';folder.mkdir(parents=True)
    raw=b'synthetic source';sha=hashlib.sha256(raw).hexdigest();p=folder/(sha+'.pdf');p.write_bytes(raw)
    old=ReceiptMetadata(registration_number='580000001',receipt_number='12345',receipt_date='14/04/2025',amount='1')
    fresh=ReceiptMetadata(**{**asdict(old),'amount':'200','payment_total_verified':True})
    calls=[]
    def extract(*args,**kwargs):
        calls.append(args[0]);return fresh
    monkeypatch.setattr(sync_state,'_extract_row',extract)
    item={'metadata':asdict(old),'paths':[p.relative_to(base).as_posix()], 'logical_id':'d' if published else None,
          'identifier_refresh':{'version':sync_state.IDENTIFIER_REFRESH_VERSION,'year':2025,'proposals':{}}}
    atomic_json(root/'sync-primary.json',{'version':1,'documents':{sha:item}})
    _, state=sync_state.catalog_documents(base.parent,'primary',root,None,refresh_year=2025)
    item=state['documents'][sha]
    if published:
        assert item['table_amount_candidate']=={'before':'1','after':'200'} and item['metadata']['amount']=='1'
    else:
        assert item['metadata']['amount']=='200'
    assert len(calls)==1
    sync_state.catalog_documents(base.parent,'primary',root,None,refresh_year=2025)
    assert len(calls)==1
