"""Explicit private review decisions. No Gmail/API or workbook writes."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from src.private_runtime import atomic_json, operational_lock, new_bytes, safe_path
from src.receipt_deduplication import _identity, _possibly_same, receipt_amount
from src.sync_state import SyncError, effective_metadata, document_paths, identity, digest
from src.user_scope import UserScope, annual_report, identifier
from src.sync_workbook import load, metadata, headers

def row_proof(meta):
    return {k:getattr(meta,k) for k in ('registration_number','receipt_number','receipt_date','amount','donor_id','donor_name','file_name')}


def check_existing(entry, wb):
    proof=entry['review_decision']['proof']
    if proof['sheet'] not in wb.sheetnames:
        raise SyncError('review_binding_changed','Reviewed historical row no longer exists')
    ws=wb[proof['sheet']]
    if row_proof(metadata(ws,proof['row'])) != proof['values']:
        raise SyncError('review_binding_changed','Reviewed historical row changed; review again')


def apply_decisions(state, decisions, report, receipts_dir, account, year):
    """Caller holds the existing operational lock; commit all decisions together."""
    for decision in decisions:
        key=decision['document_sha256']
        if key not in state['documents'] or not document_paths(receipts_dir,account,state,[key]):
            raise SyncError('review_document_missing','Review requires recoverable original PDF')
        entry=state['documents'][key]
        if entry['stage'] not in {'review','review_existing','review_not_donation','review_deferred_year'}:
            raise SyncError('review_stage_not_reviewable','Only explicit review stages may receive decisions')
        action=decision['action']
        record={'action':action,'reviewed_at':datetime.now(timezone.utc).isoformat(),'reason_code':'explicit_human_pdf_review'}
        meta=effective_metadata(entry)
        if action=='existing':
            wb=load(report)
            try:
                sheet,row=decision['sheet'],decision['row']
                if type(row) is not int or row<2 or sheet not in wb.sheetnames or row>wb[sheet].max_row:
                    raise SyncError('review_target_invalid','Select an existing donation row')
                target=metadata(wb[sheet],row)
                if not _possibly_same(_identity(meta),_identity(target)):
                    raise SyncError('review_target_unrelated','Target lacks related donation evidence')
                record['proof']={'tax_year':year,'sheet':sheet,'row':row,'values':row_proof(target)}
                entry['stage']='review_existing'
            finally:wb.close()
        elif action=='defer-year':
            try:donated=datetime.strptime(meta.receipt_date,'%d/%m/%Y').date()
            except ValueError:raise SyncError('review_date_required','A real donation date is required') from None
            if donated.year==year:
                raise SyncError('review_relevant_year','Relevant-year donations cannot be deferred as outside scope')
            entry['stage']='review_deferred_year'
        elif action=='distinct':
            if entry.get('reason') not in {'candidate_duplicate_review','historical_duplicate_candidate'}:
                raise SyncError('review_distinct_not_applicable','Distinct requires an actual duplicate-review candidate')
            entry['stage']='extracted'
            entry['logical_id']=identity(account,[key])
        elif action=='not-donation':
            entry['stage']='review_not_donation'
        elif action=='correct-fields':
            changed=False
            if 'date' in decision:
                donated=datetime.strptime(decision['date'],'%d/%m/%Y').date()
                if donated>datetime.now(timezone.utc).date():
                    raise SyncError('review_future_date','Human correction must be an actual donation date')
                entry['donation_date_override']=decision['date'];changed=True
            if 'amount' in decision:
                if not isinstance(decision['amount'],str) or receipt_amount(decision['amount']) is None:
                    raise SyncError('review_amount_invalid','Human amount must be valid and positive')
                entry['donation_amount_override']=decision['amount'];changed=True
            if not changed:raise SyncError('review_fields_missing','Provide actual receipt date or amount')
            entry['stage']='extracted'
        else:raise SyncError('review_action_invalid','Unknown explicit review action')
        entry.setdefault('review_history',[]).append(record)
        entry['review_decision']=record
        entry.pop('reason',None)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--user', required=True)
    parser.add_argument('--account',required=True);parser.add_argument('--year',type=int,required=True)
    parser.add_argument('--decisions',type=Path);parser.add_argument('--export-review',type=Path)
    args=parser.parse_args()
    from src.delta_sync import read_state
    scope = UserScope.open(args.user)
    scope.require()
    account = identifier(args.account)
    scope.require_account(account)
    root = scope.runtime
    path = safe_path(root / f'delta-{account}.json')
    for option in ('export_review', 'decisions'):
        value = getattr(args, option)
        if value is not None:
            value = safe_path(value)
            if not value.is_relative_to(root):
                parser.error("Use the selected user's private runtime directory for review files")
            setattr(args, option, value)
    with operational_lock(root):
        state=read_state(path,args.account)
        if args.export_review:
            records=[{'document_sha256':key,'reason_code':value.get('reason'),'paths':value['paths'],
                      'metadata':value.get('metadata'),'action':'REVIEW_REQUIRED'}
                     for key,value in state['documents'].items() if value['stage']=='review']
            # Explicit local destination; no overwrite and no private stdout.
            new_bytes(args.export_review,json.dumps(records,ensure_ascii=False,indent=2).encode())
        elif args.decisions:
            decisions=json.loads(args.decisions.read_text(encoding='utf-8-sig'))
            before=path.read_bytes()
            backup=root/'review-backups'/('delta-'+args.account+'-'+digest(before)+'.json')
            if backup.exists():
                if backup.read_bytes()!=before:raise SyncError('review_backup_mismatch','Private review backup mismatch')
            else:new_bytes(backup,before)
            apply_decisions(state,decisions,annual_report(scope.reports, account, args.year),
                            scope.receipts, account, args.year)
            if path.read_bytes()!=before:raise SyncError('review_state_changed','Private state changed during review')
            atomic_json(path,state)
        else:
            print(dict(Counter(d.get('reason','handled_or_deferred') for d in state['documents'].values())))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print('STOP: '+(exc.code if isinstance(exc,SyncError) else type(exc).__name__))
        raise SystemExit(1)
