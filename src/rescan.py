"""Explicit full-window diagnostic. No publication, official API or delta reset."""
import argparse
from collections import Counter
from contextlib import redirect_stdout
import io
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from html import escape
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from src.account_config import load_account_config
from src.delta_sync import (read_state, ingest, all_rows, managed_rows, discovery_counts,
                            discovery_error_reason, parsed_date, candidate_identity_uncertain)
from src.private_runtime import atomic_json, new_bytes, operational_lock, registry_root, safe_path
from src.receipt_deduplication import _identity, _possibly_same, consolidate_receipts, receipt_amount
from src.sync_state import SyncError, digest, effective_metadata, document_paths
from src.sync_workbook import load, metadata


def binding(state_path, workbook, manifest):
    return {name: digest(path.read_bytes()) if path.is_file() else None
            for name, path in [('state', state_path), ('workbook', workbook), ('manifest', manifest)]}


def check_binding(expected, state_path, workbook, manifest):
    if binding(state_path, workbook, manifest) != expected:
        raise SyncError('rescan_source_changed', 'Canonical inputs changed; preserve scan and review before new scan')


def copy_sources(queue, receipts_dir, staging, account):
    """Copy only known queue documents; no global historical PDF reconstruction."""
    origin = safe_path(Path(receipts_dir)/account)
    for key, entry in queue['documents'].items():
        for relative in entry.get('paths', []):
            source = safe_path(origin/relative)
            if not source.is_relative_to(origin):
                raise SyncError('unsafe_rescan_source', 'Source outside account')
            if not source.is_file():
                continue  # Report unavailable source; do not fabricate evidence.
            raw = source.read_bytes()
            if digest(raw) != key:
                raise SyncError('document_changed', 'Existing queued PDF changed')
            target = safe_path(staging/account/relative)
            if target.exists():
                if target.read_bytes() != raw:
                    raise SyncError('rescan_source_collision', 'Staged PDF differs')
            else:
                new_bytes(target, raw)


def reconcile(queue, workbook, receipts_dir, year, now):
    wb = load(workbook)
    try:
        entries, managed = managed_rows(wb)
        historical = [(ws.title, row, metadata(ws, row)) for ws, row in all_rows(wb).values()]
        published_docs = {doc: key for key, entry in entries.items() for doc in entry['documents']}
        records = [(Path(key), effective_metadata(entry)) for key, entry in sorted(queue['documents'].items())
                   if 'metadata' in entry]
        groups = consolidate_receipts(records)
        result = []
        for group in groups:
            keys = [str(p) for p in group.sources]
            meta = group.metadata
            matches = [{'sheet': sheet, 'row': row} for sheet, row, old in historical
                       if _possibly_same(_identity(meta), _identity(old))]
            published = {published_docs[k] for k in keys if k in published_docs}
            paths = document_paths(receipts_dir, queue['account'], queue, keys)
            date = parsed_date(meta, now)
            reason = None
            if published:
                category = 'already_published'
                if len(published) > 1:
                    category, reason = 'manual_review', 'conflicting_publications'
            elif any(queue['documents'][k]['stage']=='review_existing' for k in keys):
                try:
                    from src.sync_review import check_existing
                    for k in keys:
                        if queue['documents'][k]['stage']=='review_existing':check_existing(queue['documents'][k],wb)
                    category, reason = 'already_published', 'prior_verified_existing_decision'
                except (SyncError, KeyError, TypeError, ValueError):
                    category, reason = 'manual_review', 'prior_existing_decision_changed'
            elif any(queue['documents'][k]['stage']=='review_deferred_year' for k in keys):
                category, reason = 'manual_review', 'prior_deferred_year_decision'
            elif any(queue['documents'][k]['stage']=='legacy_published' or queue['documents'][k].get('prior_publication_stage')=='legacy_published' for k in keys):
                category, reason = 'manual_review', 'legacy_publication_unverified'
            elif not paths:
                category, reason = 'manual_review', 'document_source_unavailable'
            elif any(queue['documents'][k]['stage'] == 'review_not_donation' for k in keys):
                category, reason = 'manual_review', 'prior_not_donation_decision'
            elif date is None:
                category, reason = 'manual_review', 'invalid_donation_date'
            elif date.year != year:
                category, reason = 'manual_review', 'outside_requested_year'
            elif receipt_amount(meta.amount) is None:
                category, reason = 'manual_review', 'invalid_donation_amount'
            elif matches or group.suspected_group:
                category, reason = 'possible_duplicate', ('historical_duplicate_candidate' if matches else 'candidate_duplicate_review')
            elif any(queue['documents'][k]['stage'].startswith('published') for k in keys):
                category, reason = 'manual_review', 'publication_not_in_canonical_ledger'
            elif candidate_identity_uncertain(wb, managed, meta):
                category, reason = 'manual_review', 'insufficient_identity_exclusion'
            else:
                category, reason = 'proven_missing', 'complete_identity_no_related_row'
            # Never turn similarity or prior ingestion into proof of publication.
            result.append({'documents': keys, 'category': category, 'reason': reason,
                'metadata': asdict(meta), 'matching_rows': matches,
                'sources': [str(p) for p in paths],
                'previous_stages': [queue['documents'][k]['stage'] for k in keys],
                'previous_reasons': [queue['documents'][k].get('reason') for k in keys]})
        for key, entry in queue['documents'].items():
            if 'metadata' not in entry:
                result.append({'documents': [key], 'category': 'manual_review',
                    'reason': ('legacy_publication_unverified' if entry['stage']=='legacy_published' or entry.get('prior_publication_stage')=='legacy_published' else entry.get('reason', 'extraction_incomplete')), 'metadata': {},
                    'matching_rows': [], 'sources': [], 'previous_stages': [entry['stage']],
                    'previous_reasons': [entry.get('reason')]})
        counts = Counter(row['category'] for row in result)
        fields = Counter()
        for _, meta in records:
            for field in ('receipt_date', 'amount', 'registration_number', 'receipt_number'):
                if not getattr(meta, field).strip():
                    fields['missing_'+field] += 1
            if meta.receipt_date and parsed_date(meta, now) is None:
                fields['invalid_donation_date'] += 1
            if meta.amount and receipt_amount(meta.amount) is None:
                fields['invalid_donation_amount'] += 1
            if meta.registration_number and _identity(meta)[0] is None:
                fields['invalid_registration_number'] += 1
        return {'schema': 1, 'year': year, 'existing_workbook_donations': len(historical),
                'classifications': dict(counts), 'recurring_extraction_issues': dict(fields),
                'reason_counts': dict(Counter(row['reason'] for row in result if row['reason'])),
                'target_2026_11_06': [row for row in result if row['metadata'].get('receipt_date') == '06/11/2026'],
                'donations': result,
                'historical_link_exception': '35 unverified historical link differences; not resolved or waived by rescan'}
    finally:
        wb.close()


def write_report(folder, report):
    atomic_json(folder/'reconciliation.json', report)
    lines = ['<!doctype html><meta charset="utf-8"><html lang="he" dir="rtl"><title>בדיקת פערים 2026</title>',
             '<style>body{font-family:Arial}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px;vertical-align:top}th{position:sticky;top:0;background:#eee}</style>',
             '<h1>סריקת אבחון בלבד — ללא פרסום תרומות</h1>',
             '<p>35 הבדלי הקישורים ההיסטוריים אינם מאומתים ואינם נפתרים או מאושרים בדוח זה.</p>',
             '<pre>'+escape(json.dumps(report['summary'], ensure_ascii=False, indent=2))+'</pre>',
             '<p>already_published = פרסום מוכח באינדקס או בהחלטה ידנית מאומתת; proven_missing = זהות מלאה ללא שורה קשורה; possible_duplicate = חשד לכפילות; manual_review = בדיקה ידנית. אין אישור אוטומטי לפרסום.</p>',
             '<table><tr><th>מצב וסיבה</th><th>תאריך</th><th>עמותה</th><th>מספר רישום</th><th>קבלה</th><th>סכום</th><th>מצב קודם</th><th>שורות דומות בדוח</th><th>הערות חילוץ</th><th>מזהי מסמכים</th><th>PDF</th></tr>']
    for row in report['donations']:
        meta = row['metadata']
        values = [row['category']+' / '+str(row['reason']), meta.get('receipt_date',''), meta.get('organization_name',''),
                  meta.get('registration_number',''), meta.get('receipt_number',''), meta.get('amount',''),
                  json.dumps([row['previous_stages'],row['previous_reasons']], ensure_ascii=False),
                  json.dumps(row['matching_rows'], ensure_ascii=False), meta.get('notes',''), ' '.join(row['documents'])]
        pdfs = ' '.join('<a href="'+escape(Path(p).as_uri(), quote=True)+'">PDF '+str(i+1)+'</a>' for i,p in enumerate(row['sources']))
        lines.append('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in values)+'<td>'+pdfs+'</td></tr>')
    lines.append('</table><h2>הודעות שסוננו או לא הושלמו — לבדיקה מקומית</h2><table><tr><th>נושא</th><th>תאריך הגעה (אינו תאריך התרומה)</th><th>סיבה</th><th>קבצים</th><th>מזהה הודעה</th></tr>')
    for message in report.get('messages_requiring_review',[]):
        lines.append('<tr>'+''.join('<td>'+escape(str(message.get(k,'')))+'</td>' for k in ('subject','arrival_date','reason_code','attachments','message_id'))+'</tr>')
    lines.append('</table></html>')
    from src.private_runtime import atomic_bytes
    atomic_bytes(folder/'reconciliation.html', '\n'.join(lines).encode('utf-8'))


def run_rescan(*, provider, receipts_dir, reports_dir, accounts_dir, manifest_path,
               account, year, root=None, now=None, plan=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', account) or year != 2026:
        raise SyncError('rescan_scope', 'This supervised diagnostic supports explicit 2026 only')
    root = registry_root(root)
    config = load_account_config(accounts_dir, account)
    zone = getattr(config,'operating_timezone','Asia/Jerusalem')
    now = now or datetime.now(ZoneInfo(zone))
    if now.utcoffset() is None or now.year < year:
        raise SyncError('invalid_rescan_clock', 'Timezone-aware current clock required')
    state_path = root/f'delta-{account}.json'
    workbook = safe_path(Path(reports_dir)/account/str(year)/f'donation_summary_{year}.xlsx')
    folder = root/f'reconciliation-{account}-{year}'
    progress_path = folder/'state.json'
    canonical = read_state(state_path, account)
    if not state_path.is_file() or not workbook.is_file():
        raise SyncError('rescan_baseline_missing', 'Existing state and canonical workbook required')
    if any(workbook.with_name(n).exists() for n in ('~$'+workbook.name, '~$'+workbook.name[2:])):
        raise SyncError('workbook_open', 'Close Excel before diagnostic')
    before = binding(state_path, workbook, manifest_path)
    previous = json.loads(progress_path.read_text()) if progress_path.exists() else None
    if previous:
        check_binding(previous['canonical_binding'], state_path, workbook, manifest_path)
    start = datetime(year,1,1,tzinfo=ZoneInfo(zone))
    discovery = (previous or {}).get('discovery') or {'query': f'has:attachment after:{int(start.timestamp())-1} before:{int(now.timestamp())+1}',
                'start':start.isoformat(), 'boundary':now.isoformat(), 'ids':{}, 'listed':False, 'pages':0}
    info = {'plan_only': plan, 'workflow':'diagnostic_rescan', 'year':year,
            'search_start':discovery['start'], 'search_boundary':discovery['boundary'],
            'checkpoint_unchanged':canonical['checkpoint'], 'discovery_progress':discovery_counts({'discovery':discovery}),
            'pending_review_preserved':sum(d['stage']=='review' for d in canonical['documents'].values()),
            'publication_enabled':False, 'official_api_requests':0, 'phase':(previous or {}).get('phase','not_started')}
    # Validate Excel compatibility without rewriting it or resolving old links.
    wb = load(workbook)
    try: managed_rows(wb)
    finally: wb.close()
    if plan:
        return info
    with operational_lock(root):
        check_binding(before, state_path, workbook, manifest_path)
        folder.mkdir(parents=True, exist_ok=True)
        scan = previous or {'schema':1, 'canonical_binding':before, 'discovery':discovery,
                            'queue':deepcopy(canonical), 'phase':'copy_sources'}
        queue_path = folder/'queue.json'
        if queue_path.exists():
            scan['queue'] = read_state(queue_path, account)
        persist = lambda: atomic_json(progress_path, scan)
        staging = folder/'receipts'
        staged_manifest = folder/'manifest.json'
        persist()
        if scan['phase']=='complete':
            return json.loads((folder/'reconciliation.json').read_text())['summary']
        copy_sources(scan['queue'], receipts_dir, staging, account)
        if not staged_manifest.exists():
            new_bytes(staged_manifest, manifest_path.read_bytes() if manifest_path.exists() else b'[]')
        try:
            scan['phase']='discovery';persist()
            cached = scan['discovery']
            if cached.get('listed') and all(v is not None for v in cached.get('ids',{}).values()):
                messages = list(cached.get('ids',{}).values())
            else:
                messages = provider.fetch_emails_resumable(cached, persist)
            for message in messages:
                key=message['message_id']
                item=scan['queue']['messages'].setdefault(key, {'stage':'discovered','documents':[]})
                if item.get('rescan_boundary') != scan['discovery']['boundary']:
                    # Explicit full rescan must inspect every attachment, even in
                    # an ingested message whose old processing missed a receipt.
                    # Retain document IDs/metadata/cache; no historical re-extraction.
                    item.setdefault('prior_ingestion_result',item.get('ingestion_result'))
                    incoming = ({**item['message'],'discovery_unavailable':'message_not_found'}
                                if message.get('discovery_unavailable') and item.get('message') else message)
                    item.update(stage='discovered',message=incoming,rescan_boundary=scan['discovery']['boundary'])
                elif item['stage']=='discovered' and not message.get('discovery_unavailable'):
                    item['message']=message
            for entry in scan['queue']['documents'].values():
                if entry['stage']=='legacy_published' and 'metadata' not in entry:
                    # Explicit diagnostic extraction only, original state unchanged.
                    entry.update(stage='downloaded',prior_publication_stage='legacy_published')
            scan['phase']='processing';persist()
            atomic_json(queue_path, scan['queue'])
            with redirect_stdout(io.StringIO()):
                ingest(scan['queue'], queue_path, provider, staging, staged_manifest, config, now)
            # ingest atomically persists its own compatible queue file; also bind
            # its most recent state back into the scan progress before reporting.
            scan['phase']='reconciliation';persist()
            report = reconcile(scan['queue'], workbook, staging, year, now)
            check_binding(before, state_path, workbook, manifest_path)
            report['messages_requiring_review'] = []
            for key in scan['discovery']['ids']:
                item = scan['queue']['messages'][key]
                if item['stage']=='discovered' or item.get('ingestion_result') in {'skipped_low_confidence','skipped_no_supported_attachments'}:
                    msg = item.get('message',{})
                    report['messages_requiring_review'].append({'message_id':key,
                        'subject':msg.get('subject',''), 'arrival_date':msg.get('arrival_date',msg.get('date','')),
                        'reason_code':item.get('reason',item.get('ingestion_result','unfinished')),
                        'attachments':[a.get('filename','') for a in msg.get('attachments',[])]})
            summary = {**info, 'plan_only':False, 'phase':'complete',
                'messages_scanned':len(scan['discovery']['ids']),
                'messages_requiring_review':len(report['messages_requiring_review']),
                'message_status_counts':dict(Counter(m.get('reason','unfinished') if m['stage']=='discovered' else m.get('ingestion_result',m['stage']) for m in scan['queue']['messages'].values())),
                'documents_known':len(scan['queue']['documents']),
                'pdf_attachments_seen':sum(str(a.get('filename','')).lower().endswith('.pdf') for m in messages for a in m.get('attachments',[])),
                'new_document_records':len(set(scan['queue']['documents'])-set(canonical['documents'])),
                'extracted_metadata_records':sum('metadata' in d for d in scan['queue']['documents'].values()),
                'pending_messages':sum(m['stage']=='discovered' for m in scan['queue']['messages'].values()),
                'retryable_pending_messages':sum(m['stage']=='discovered' and m.get('reason')!='message_not_found' for m in scan['queue']['messages'].values()),
                'unavailable_message_sources':sum(m.get('reason')=='message_not_found' for m in scan['queue']['messages'].values()),
                'manual_review_required':bool(report['messages_requiring_review'] or report['classifications'].get('manual_review') or report['classifications'].get('possible_duplicate') or report['classifications'].get('proven_missing')),
                'existing_workbook_donations':report['existing_workbook_donations'],
                'classifications':report['classifications'], 'reason_counts':report['reason_counts'],
                'recurring_extraction_issues':report['recurring_extraction_issues'],
                'target_2026_11_06_count':len(report['target_2026_11_06'])}
            if summary['retryable_pending_messages']:
                summary['phase']='processing'
            report.update(canonical_binding=before, summary=summary,
                          discovery_boundary=scan['discovery']['boundary'])
            write_report(folder,report)
            check_binding(before, state_path, workbook, manifest_path)
            if summary['retryable_pending_messages']:
                scan['phase']='processing';persist()
                raise SyncError('rescan_pending_messages',
                    'Diagnostic incomplete; pending_messages='+str(summary['retryable_pending_messages'])+'; report retained; resume same command')
            scan.pop('stopped_reason',None)
            scan['phase']='complete';persist()
            return summary
        except Exception as exc:
            if isinstance(exc,SyncError) and exc.code in {'rescan_source_changed','rescan_pending_messages'}:
                scan['stopped_reason']=exc.code
                persist()
                raise
            scan['stopped_reason']=discovery_error_reason(exc)
            persist()
            raise SyncError('rescan_interrupted', 'Diagnostic stopped; phase='+scan['phase']+'; reason='+scan['stopped_reason']+'; '+json.dumps(discovery_counts({'discovery':scan['discovery']}),sort_keys=True)+'; resume same command') from None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--user', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args()
    from src.providers.gmail_provider import GmailProvider
    from src.user_scope import UserScope, identifier
    try:
        scope = UserScope.open(args.user)
        scope.require()
        account = identifier(args.account)
        path = scope.require_account(account)
        result = run_rescan(provider=GmailProvider(path), receipts_dir=scope.receipts,
                            reports_dir=scope.reports, accounts_dir=scope.accounts,
                            manifest_path=scope.manifest, root=scope.runtime,
                            account=account, year=args.year, plan=args.plan)
        print(json.dumps(result,ensure_ascii=True,sort_keys=True))
    except Exception as exc:
        print('STOP: '+(exc.code if isinstance(exc,SyncError) else 'diagnostic_validation_failed'))
        if isinstance(exc,SyncError) and exc.code in {'rescan_interrupted','rescan_pending_messages'}:print(str(exc))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
