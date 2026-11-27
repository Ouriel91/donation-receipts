from pathlib import Path
from dataclasses import asdict
import hashlib
import openpyxl
import pytest
from src import sync_workbook as sw
from src.receipt_metadata_extractor import ReceiptMetadata
from src.receipt_deduplication import _identity
from src.manifest import mark_processed, remember_attachment_origin, load_attachment_origins, recover_attachment_origins


def fixtures(tmp_path, monkeypatch, *, conflict=False, distinct=False, missing=False, protected=False, oc=False):
    base=tmp_path/'receipts'/'primary';base.mkdir(parents=True)
    metas=[ReceiptMetadata(registration_number='580000001', receipt_number='12345', receipt_date='14/04/2025', amount='100', organization_name='organization', tax_report_number=tax) for tax in ('','23456')]
    if conflict: metas[0].tax_report_number='99999'
    wb=openpyxl.Workbook();ws=wb.active;ws.title='אפריל'
    fields=['registration_number','receipt_number','receipt_date','amount','tax_report_number','donor_id','organization_name']
    ws.append([sw.COLUMN_HEADERS_HE[f] for f in fields]);entries={};rows={};state={'documents':{}};paths={};proofs={}
    for i,meta in enumerate(metas,2):
        key=f'p{i}';raw=f'synthetic-{i}'.encode();sha=hashlib.sha256(raw).hexdigest();path=base/(sha+'.pdf');path.write_bytes(raw)
        ws.append([getattr(meta,f) or None for f in fields]);rows[key]=(ws,i)
        entries[key]={'documents':[sha],'owned_source_fields':True}
        state['documents'][sha]={'metadata':asdict(meta),'paths':[path.name]}
        if oc: state['documents'][sha]['attachment_origins']=['00012345'+('o' if i==2 else 'c')+'.pdf']
        paths[sha]=path;proofs[sha]=(_identity(meta),meta)
        if i==3:
            if distinct: state['documents'][sha]['review_decision']={'action':'distinct'}
            if missing: proofs[sha]=None
            if protected: ws.cell(i,5).comment=openpyxl.comments.Comment('manual','tester')
    monkeypatch.setattr(sw,'_oc_pdf_evidence',lambda path,account,year,sha: proofs[sha])
    return rows,entries,state,base


def test_distinct_bytes_same_receipt_prefers_verified_reporting(tmp_path,monkeypatch):
    rows,entries,state,base=fixtures(tmp_path,monkeypatch)
    pairs=sw._verified_identity_pairs(rows,entries,state,base)
    assert pairs=={'p2':('p3',entries['p3']['documents'][0]),'p3':('p2',entries['p3']['documents'][0])}

@pytest.mark.parametrize('kw',['conflict','distinct','missing','protected'])
def test_no_merge_when_evidence_or_protection_conflicts(tmp_path,monkeypatch,kw):
    rows,entries,state,base=fixtures(tmp_path,monkeypatch,**{kw:True})
    assert sw._verified_identity_pairs(rows,entries,state,base)=={}


def test_oc_uses_verified_attachment_names_not_hashes(tmp_path,monkeypatch):
    rows,entries,state,base=fixtures(tmp_path,monkeypatch,oc=True)
    assert sw._original_copy_pairs(rows,entries,state,base)['p2']==('p3',entries['p3']['documents'][0])


def test_oc_leading_zero_mismatch_not_paired(tmp_path,monkeypatch):
    rows,entries,state,base=fixtures(tmp_path,monkeypatch,oc=True)
    state['documents'][entries['p3']['documents'][0]]['attachment_origins']=['12345c.pdf']
    assert sw._original_copy_pairs(rows,entries,state,base)=={}


def test_original_name_hash_and_account_isolation(tmp_path):
    raw=b'example';source=tmp_path/'x.pdf';source.write_bytes(raw);manifest=tmp_path/'processed_messages.json'
    remember_attachment_origin(manifest,'primary',source,raw,'12345c.pdf')
    key=hashlib.sha256(raw).hexdigest()
    assert load_attachment_origins(manifest,'primary')=={key:['12345c.pdf']}
    assert load_attachment_origins(manifest,'second')=={}
    with pytest.raises(ValueError): remember_attachment_origin(manifest,'primary',source,b'other','12345o.pdf')


def test_recover_existing_source_only_after_hash_match(tmp_path):
    base=tmp_path/'receipts'/'primary';base.mkdir(parents=True);raw=b'matching source';source=base/'hash.pdf';source.write_bytes(raw)
    manifest=tmp_path/'runtime'/'processed_messages.json';mark_processed('primary','message',[str(source)],manifest)
    class Provider:
        def __init__(self):self.calls=[]
        def download_attachment(self,m,a):self.calls.append(a);return raw if a=='right' else b'other'
    provider=Provider();email={'message_id':'message','attachments':[{'filename':'12345c.pdf','attachment_id':'right'},{'filename':'22222o.pdf','attachment_id':'wrong'},{'filename':'other.pdf','attachment_id':'skip'}]}
    recover_attachment_origins(provider,manifest,'primary',tmp_path/'receipts',email)
    assert provider.calls==['right','wrong']
    assert load_attachment_origins(manifest,'primary')=={hashlib.sha256(raw).hexdigest():['12345c.pdf']}
    provider.calls=[];recover_attachment_origins(provider,manifest,'primary',tmp_path/'receipts',email)
    assert provider.calls==['wrong']

@pytest.mark.parametrize('oc',[False,True])
def test_filter_archives_duplicate_keeps_sources_and_reporting(tmp_path,monkeypatch,oc):
    rows,entries,state,base=fixtures(tmp_path,monkeypatch,oc=oc)
    ws=rows['p2'][0];wb=ws.parent
    idcol=ws.max_column+1;ws.cell(1,idcol,sw.ID_HEADER)
    for key,(_,row) in rows.items():ws.cell(row,idcol,key)
    expected=entries['p3']['documents'][0]
    all_docs={d for e in entries.values() for d in e['documents']}
    assert sw.filter_verified_pdf_rows(wb,entries,rows,state,base)==1
    assert len(rows)==1 and set(entries['p2']['documents'])==all_docs
    assert entries['p2']['representative_document']==expected
    assert sw.metadata(ws,2).tax_report_number=='23456'
    assert ws.row_dimensions[3].hidden
    assert sw.filter_verified_pdf_rows(wb,entries,rows,state,base)==0
