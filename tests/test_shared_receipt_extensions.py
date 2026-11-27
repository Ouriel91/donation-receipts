"""Focused offline checks of additive cross-route changes (no Gmail/PII)."""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import hashlib
import importlib
import json
import sys

import openpyxl
import pytest

from src import sync_workbook as sw, sync_state as ss, manual_receipts as manual
from src.receipt_deduplication import _identity
from src.receipt_metadata_extractor import ReceiptMetadata
from src.user_scope import UserScope, annual_report
from src.private_runtime import atomic_json


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def meta(*, filename='', amount='100', tax='', year=2025):
    return ReceiptMetadata(file_name=filename, registration_number='580000001',
                           receipt_number='12345', receipt_date=f'14/04/{year}',
                           organization_name='Synthetic organization', amount=amount,
                           tax_report_number=tax)


def pair(tmp_path, monkeypatch, accounts=('manual', 'manual')):
    sources = []
    for i, (account, name) in enumerate(zip(accounts, ('00012345o.pdf', '00012345c.pdf'))):
        raw = f'%PDF-synthetic-{i}'.encode()
        sha = digest(raw)
        path = tmp_path / 'receipts' / account / (sha+'.pdf')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        item = {'metadata': asdict(meta(filename=name if account == 'manual' else path.name,
                                       tax='' if not i else '23456')),
                'paths': [path.name]}
        if account != 'manual':
            item['attachment_origins'] = [name]
        sources.append((sha, path, account, item))
    proof = {sha: ss.effective_metadata(item) for sha, _, _, item in sources}
    monkeypatch.setattr(sw, '_oc_pdf_evidence', lambda path, account, year, sha:
                        (_identity(proof[sha]), proof[sha]) if digest(path.read_bytes()) == sha else None)
    return sources


@pytest.mark.parametrize('accounts',[('manual','manual'),('primary','primary'),('primary','manual'),('primary','secondary')])
def test_strict_copy_representative_shared(tmp_path, monkeypatch, accounts):
    sources=pair(tmp_path, monkeypatch, accounts)
    assert sw.verified_copy_source(sources, _identity(meta())) == sources[1][1]


@pytest.mark.parametrize('problem',['zero','prefix','distinct','override','changed_pdf','conflicting_tax','lone_copy'])
def test_copy_proof_preserves_old_rules(tmp_path, monkeypatch, problem):
    sources=pair(tmp_path, monkeypatch, ('primary','manual'))
    sha, path, account, item = sources[1]
    if problem=='zero': item['metadata']['file_name']='12345c.pdf'
    if problem=='prefix': item['metadata']['file_name']='prefix00012345c.pdf'
    if problem=='distinct': item['review_decision']={'action':'distinct'}
    if problem=='override': item['donation_amount_override']='100'
    if problem=='changed_pdf': path.write_bytes(b'different')
    if problem=='conflicting_tax': sources[0][3]['metadata']['tax_report_number']='11111'
    if problem=='lone_copy': sources=sources[1:]
    assert sw.verified_copy_source(sources, _identity(meta())) is None


@pytest.mark.parametrize('account',['manual','primary'])
@pytest.mark.parametrize('stage',['extracted','published','review'])
def test_saved_amount_stage_scoped_and_idempotent(tmp_path,monkeypatch,account,stage):
    base=tmp_path/'receipts'/account;base.mkdir(parents=True)
    raw=b'%PDF-synthetic';sha=digest(raw);path=base/(sha+'.pdf');path.write_bytes(raw)
    item={'metadata':asdict(meta(amount='1')),'paths':[path.name],'stage':stage,
          'logical_id':'d' if stage=='published' else None}
    state={'documents':{sha:item}}
    fresh=meta(amount='200');fresh.payment_total_verified=True
    calls=[]
    def extract(*args,**kwargs):calls.append(args);return fresh
    monkeypatch.setattr(ss,'_extract_row',extract)
    ss.stage_saved_table_amounts(state,base.parent,account,None,{2024})
    assert not calls
    ss.stage_saved_table_amounts(state,base.parent,account,None,{2025})
    ss.stage_saved_table_amounts(state,base.parent,account,None,{2025})
    if stage=='review': assert not calls and item['metadata']['amount']=='1'
    elif stage=='published': assert len(calls)==1 and item['table_amount_candidate']=={'before':'1','after':'200'} and item['metadata']['amount']=='1'
    else: assert len(calls)==1 and item['metadata']['amount']=='200'


@pytest.mark.parametrize('mode',['copy_pair','ordinary_duplicate','conflicting_optional'])
def test_manual_intake_keeps_original_policy_except_verified_pair(tmp_path,monkeypatch,mode):
    monkeypatch.setenv('DONATION_RECEIPTS_PRIVATE_ROOT',str(tmp_path/'private'))
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT',str(tmp_path/'registry'))
    scope=UserScope.open('synthetic',tmp_path/'project');scope.create()
    inbox=scope.receipts/'manual'/'inbox';inbox.mkdir(parents=True)
    names=('00012345o.pdf','00012345c.pdf') if mode!='ordinary_duplicate' else ('other1.pdf','other2.pdf')
    proofs={}
    for i,n in enumerate(names):
        raw=f'%PDF-fake-{i}'.encode();(inbox/n).write_bytes(raw)
        proofs[digest(raw)]=meta(filename=n,tax=('99999' if not i else '23456') if mode=='conflicting_optional' else ('' if not i else '23456'))
    monkeypatch.setattr(manual,'_inspect',lambda path,raw,year,*a,**k:(proofs[digest(raw)],None))
    monkeypatch.setattr(sw,'_oc_pdf_evidence',lambda path,account,year,sha:(_identity(proofs[sha]),proofs[sha]))
    # The publication boundary is stubbed; the real intake, storage and grouping run.
    fake=ModuleType('src.sync');seen=[]
    def publish(*a,prepared_catalog,**k):
        seen.append(prepared_catalog[1]);return {}
    fake.publish=publish;monkeypatch.setitem(sys.modules,'src.sync',fake)
    out=manual.run_manual(scope,2025)
    state=seen[-1];intake=json.loads((scope.runtime/'manual-intake.json').read_bytes())
    assert len(intake['documents'])==2
    assert all((scope.receipts/'manual'/item['paths'][0]).is_file() for item in intake['documents'].values())
    if mode=='copy_pair':
        assert len(state['documents'])==2 and out['manual_review']==0
        groups=ss.groups(state,{})
        assert len(groups)==1 and len(groups[0].sources)==2
        paths=ss.document_paths(scope.receipts,'manual',state,list(state['documents']))
        preferred=sw.preferred_group_paths(paths,'manual',state,groups[0].metadata)
        assert digest(preferred[0].read_bytes()) in proofs and proofs[digest(preferred[0].read_bytes())].file_name.endswith('c.pdf')
    elif mode=='ordinary_duplicate':
        assert len(state['documents'])==1 and out['manual_review']==1
    else:
        # Existing ambiguous-component behavior is preserved; no forced O/C pair.
        assert len(ss.groups(state,{}))==2
    second=manual.run_manual(scope,2025)
    assert second['manual_ingested']==0


def test_preference_protects_workbook_user_value(tmp_path,monkeypatch):
    sources=pair(tmp_path,monkeypatch)
    state={'documents':{sha:item for sha,_,_,item in sources}}
    wb=openpyxl.Workbook();ws=wb.active;ws.title='אפריל'
    fields=['registration_number','receipt_number','receipt_date','amount','tax_report_number','file_name']
    ws.append([sw.COLUMN_HEADERS_HE[f] for f in fields]);m=meta(filename='old.pdf')
    ws.append([getattr(m,f) or None for f in fields]);rows={'d':(ws,2)}
    entry={'documents':list(state['documents']),'owned_source_fields':True,'managed':{sw.COLUMN_HEADERS_HE['tax_report_number']:'manual'}}
    entries={'d':entry}
    assert not sw.apply_group_copy_preference(entries,rows,state,tmp_path/'receipts'/'manual')
    assert ws.cell(2,5).value is None
    entry['managed'].clear()
    assert sw.apply_group_copy_preference(entries,rows,state,tmp_path/'receipts'/'manual')=={'d'}
    assert ws.cell(2,5).value=='23456' and entry['representative_document']==sources[1][0]
    assert not sw.apply_group_copy_preference(entries,rows,state,tmp_path/'receipts'/'manual')


@pytest.fixture
def offline_modules(monkeypatch):
    # Optional Google dependencies are not installed in this local environment.
    # Stub the provider import, NOT any publication or workbook operations.
    fake=ModuleType('src.providers.gmail_provider');fake.GmailProvider=object
    monkeypatch.setitem(sys.modules,'src.providers.gmail_provider',fake)
    return importlib.import_module('src.delta_sync'),importlib.import_module('src.combined_report')


def make_publication(path, meta, sources, key, entries, *, formula=False):
    from src.receipt_summary import _write_month_sheet, SOURCE_SHEET
    from src.receipt_deduplication import Donation
    wb=openpyxl.Workbook();wb.remove(wb.active)
    source=wb.create_sheet(SOURCE_SHEET);source.append(['id','month','pdf'])
    _write_month_sheet(wb,'04_April',[Donation(meta,tuple(sources))],source)
    ws=wb['אפריל'];ws.cell(1,ws.max_column+1,sw.ID_HEADER);ws.cell(2,ws.max_column,key)
    rows={key:(ws,2)}
    if formula:
        amountcol=sw.headers(ws)[sw.COLUMN_HEADERS_HE['amount']]
        ws.cell(2,amountcol,'=1+0')
    sw.write_totals(wb,rows,False,preserve_formulas=formula)
    sw.write_index(wb,entries)
    path.parent.mkdir(parents=True,exist_ok=True);wb.save(path);wb.close()


@pytest.mark.parametrize('protected',[False,True])
def test_delta_existing_publication_amount_repair(tmp_path,monkeypatch,offline_modules,protected):
    from src import receipt_summary
    delta,_=offline_modules
    base=tmp_path/'receipts'/'primary';base.mkdir(parents=True)
    raw=b'%PDF-fake';sha=digest(raw);p=base/(sha+'.pdf');p.write_bytes(raw)
    key='d-'+('a'*64)
    m=meta(filename=p.name,amount='1')
    item={'metadata':asdict(m),'paths':[p.name],'stage':'published','logical_id':key}
    state={'account':'primary','documents':{sha:item}}
    entries={key:{'documents':[sha],'owned_source_fields':True,'managed':{}}}
    report=tmp_path/'reports'/'primary'/'2025'/'donation_summary_2025.xlsx'
    make_publication(report,m,[p],key,entries,formula=protected)
    before=report.read_bytes()
    fresh=meta(filename=p.name,amount='200');fresh.payment_total_verified=True
    monkeypatch.setattr(ss,'_extract_row',lambda *a,**k:fresh)
    monkeypatch.setattr(receipt_summary,'_extract_row',lambda *a,**k:fresh)
    monkeypatch.setattr(delta,'OrganizationCache',lambda *_:None)
    monkeypatch.setattr(delta,'LiveSection46',lambda *a,**k:SimpleNamespace(counts={}))
    root=tmp_path/'runtime';root.mkdir();state_path=root/'delta-primary.json'
    now=datetime(2026,11,26,tzinfo=timezone.utc)
    out=delta.publish_pending(state,state_path,base.parent,tmp_path/'reports',root,None,{2025},now)
    if protected:
        assert report.read_bytes()==before and out.get('amount_repairs',0)==0
    else:
        assert out['amount_repairs']==1 and out['reports_updated']==1
        with __import__('contextlib').closing(sw.load(report)) as wb:
            after,rows=delta.managed_rows(wb)
            assert sw.metadata(*rows[key]).amount=='200'
            assert after[key]['documents']==[sha]
        assert any((root/'report-backups').rglob('*.xlsx'))
        second=delta.publish_pending(state,state_path,base.parent,tmp_path/'reports',root,None,{2025},now)
        assert second.get('amount_repairs',0)==0 and second['reports_updated']==0


def test_combined_read_preserves_verified_copy_path(tmp_path,monkeypatch,offline_modules):
    _,combined=offline_modules
    sources=pair(tmp_path,monkeypatch)
    state={'documents':{sha:item for sha,_,_,item in sources},'version':1}
    scope=SimpleNamespace(receipts=tmp_path/'receipts',reports=tmp_path/'reports',runtime=tmp_path/'runtime')
    scope.runtime.mkdir();atomic_json(scope.runtime/'sync-manual.json',state)
    key='d-'+('b'*64)
    entries={key:{'documents':list(state['documents']),'owned_source_fields':True,'managed':{},'representative_document':sources[1][0]}}
    report=annual_report(scope.reports,'manual',2025)
    make_publication(report,meta(filename=sources[1][1].name,tax='23456'),[source[1] for source in sources],key,entries)
    rows,review=combined._read(scope,'manual',2025)
    assert not review and len(rows)==1 and len(rows[0].hashes)==2
    assert rows[0].paths[0]==sources[1][1]


def test_delta_tax_layout_repair(tmp_path,monkeypatch,offline_modules):
    from src import receipt_summary
    delta,_=offline_modules
    base=tmp_path/'receipts'/'primary';base.mkdir(parents=True)
    raw=b'%PDF-test-tax';sha=digest(raw);p=base/(sha+'.pdf');p.write_bytes(raw)
    key='d-'+('c'*64);m=meta(filename=p.name)
    item={'metadata':asdict(m),'paths':[p.name],'stage':'published','logical_id':key,'table_amount_revision':1}
    state={'account':'primary','documents':{sha:item}}
    entries={key:{'documents':[sha],'owned_source_fields':True,'managed':{}}}
    report=tmp_path/'reports'/'primary'/'2025'/'donation_summary_2025.xlsx'
    make_publication(report,m,[p],key,entries)
    fresh=meta(filename=p.name,tax='12345');fresh.tax_layout_verified=True
    monkeypatch.setattr(ss,'_extract_row',lambda *a,**k:fresh)
    monkeypatch.setattr(receipt_summary,'_extract_row',lambda *a,**k:fresh)
    monkeypatch.setattr(delta,'OrganizationCache',lambda *_:None)
    monkeypatch.setattr(delta,'LiveSection46',lambda *a,**k:SimpleNamespace(counts={}))
    root=tmp_path/'runtime';root.mkdir();state_path=root/'delta-primary.json';now=datetime(2026,11,26,tzinfo=timezone.utc)
    out=delta.publish_pending(state,state_path,base.parent,tmp_path/'reports',root,None,{2025},now)
    assert out['tax_layout_repairs']==1 and out['reports_updated']==1
    with __import__('contextlib').closing(sw.load(report)) as wb:
        after,rows=delta.managed_rows(wb);assert sw.metadata(*rows[key]).tax_report_number=='12345'
        assert after[key]['documents']==[sha]
    second=delta.publish_pending(state,state_path,base.parent,tmp_path/'reports',root,None,{2025},now)
    assert second.get('tax_layout_repairs',0)==0 and second['reports_updated']==0
