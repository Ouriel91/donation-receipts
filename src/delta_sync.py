"""Forward-only Gmail discovery. Private queue != atomic Excel publication ledger."""
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import openpyxl

from src.annual_excel_summary import summary_name, write_summary
from src.account_config import load_account_config
from src.gmail_runner import run_gmail
from src.manifest import load_manifest
from src.organization_cache import OrganizationCache
from src.private_runtime import atomic_json, operational_lock, registry_root, safe_path
from src.receipt_deduplication import (Donation, _identity, _possibly_same,
                                       consolidate_receipts, receipt_amount)
from src.receipt_summary import _extract_row, _finalize_row
from src.section46_live import LiveSection46
from src.sync_state import SyncError, digest, document_paths, effective_metadata, identity
from src.sync_workbook import (ID_HEADER, INDEX, LIVE_HEADERS, donation_sheets, enrich_candidates,
                               headers, load, metadata, rebind_published_locations, replace_verified,
                               signature, stamp_published_locations,
                               write_index, write_sources, write_totals, archived_duplicate, same_historical_review, validate_historical_archives, refresh_published_rows)

OVERLAP = timedelta(hours=48)


def discovery_error_reason(exc):
    """Allowlisted reason only: never provider response, IDs or exception text."""
    from googleapiclient.errors import HttpError
    from google.auth.exceptions import RefreshError
    import socket
    import ssl
    if isinstance(exc, HttpError):
        return 'http_' + str(exc.resp.status) if type(exc.resp.status) is int else 'http_error'
    if isinstance(exc, RefreshError):
        return 'authentication_required'
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, ssl.SSLEOFError)):
        return 'network_failure'
    if isinstance(exc, ValueError):
        return 'invalid_provider_or_state'
    return 'provider_or_storage_failure'


def discovery_counts(state):
    progress = state.get('discovery', {})
    ids = progress.get('ids', {})
    return {'pages': progress.get('pages', 0), 'discovered': len(ids),
            'descriptors_completed': sum(value is not None for value in ids.values()),
            'listing_complete': bool(progress.get('listed')), 'resumable': bool(progress),
            'unavailable_messages': sum(isinstance(v, dict) and bool(v.get('discovery_unavailable')) for v in ids.values())}


def read_state(path, account):
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'version': 1, 'account': account, 'checkpoint': None, 'messages': {}, 'documents': {}}
    if (state.get('version') != 1 or state.get('account') != account
            or not isinstance(state.get('messages'), dict) or not isinstance(state.get('documents'), dict)):
        raise SyncError('invalid_delta_state', 'Restore private delta state before retry')
    stages = {'downloaded', 'extracted', 'validated', 'enriched', 'published', 'published_recheck', 'review', 'review_existing', 'review_not_donation', 'review_deferred_year', 'legacy_published'}
    if (any(not isinstance(d, dict) or d.get('stage') not in stages for d in state['documents'].values())
            or any(not isinstance(m, dict) or m.get('stage') not in {'discovered', 'ingested', 'legacy_processed'}
                   for m in state['messages'].values())):
        raise SyncError('invalid_delta_state', 'Invalid durable processing stage')
    if state.get('checkpoint') is not None:
        stamp = datetime.fromisoformat(state['checkpoint'])
        if stamp.utcoffset() is None:
            raise SyncError('invalid_delta_checkpoint', 'Checkpoint must include timezone')
    return state


def discovery_window(state, now, operating_timezone):
    local = now.astimezone(ZoneInfo(operating_timezone))
    if state['checkpoint']:
        checkpoint = datetime.fromisoformat(state['checkpoint'])
        if checkpoint > now:
            raise SyncError('future_delta_checkpoint', 'Check system clock before retry')
        start = checkpoint - OVERLAP
    else:
        start = datetime(local.year, 1, 1, tzinfo=local.tzinfo)
    # Gmail epoch boundaries avoid Gmail's implicit Pacific timezone; include
    # exact midnight and run-start second. Repeated edges are intentional.
    query = f'has:attachment after:{int(start.timestamp()) - 1} before:{int(now.timestamp()) + 1}'
    return start, query


def parsed_date(meta, now):
    try:
        value = datetime.strptime(meta.receipt_date, '%d/%m/%Y').date()
        return value if 1900 <= value.year and value <= now.date() + timedelta(days=1) else None
    except ValueError:
        return None


class QueuedProvider:
    """Reuse production ingestion; queued message is durable before download."""
    def __init__(self, provider, message, item=None, receipts_dir=None, persist=None):
        self.provider, self.message, self.account_dir = provider, message, provider.account_dir
        self.item, self.receipts_dir, self.persist = item, receipts_dir, persist

    def fetch_emails(self):
        return [self.message]

    def download_attachment(self, message_id, attachment_id):
        saved = (self.item or {}).get('attachments', {}).get(attachment_id)
        if saved:
            source = safe_path(Path(self.receipts_dir)/self.account_dir.name/saved['path'])
            if not source.is_relative_to(safe_path(Path(self.receipts_dir)/self.account_dir.name)):
                raise SyncError('invalid_attachment_source', 'Unsafe saved attachment')
            if source.is_file():
                raw = source.read_bytes()
                if digest(raw) != saved['sha256']:
                    raise SyncError('document_changed', 'Saved attachment changed')
                return raw
        return self.provider.download_attachment(message_id, attachment_id)

    def attachment_saved(self, attachment_id, source, content):
        self.item.setdefault('attachments', {})[attachment_id] = {
            'path': source.relative_to(Path(self.receipts_dir)/self.account_dir.name).as_posix(),
            'sha256': digest(content)}
        self.persist()


def ingest(state, path, provider, receipts_dir, manifest_path, config, now, *, allow_downloads=True):
    for item in state['messages'].values():
        if not allow_downloads or item['stage'] != 'discovered':
            continue
        if item['message'].get('discovery_unavailable'):
            item['reason'] = 'message_not_found'
            atomic_json(path, state)
            continue  # Durable unresolved source; never silently mark handled.
        try:
            if item.get('reason') == 'ingestion_retry' and hasattr(type(provider), 'fetch_message'):
                fresh = provider.fetch_message(item['message']['message_id'])
                if fresh.get('message_id') != item['message']['message_id']:
                    raise SyncError('message_identity_changed', 'Refreshed Gmail identity changed')
                item['message'] = fresh
                atomic_json(path, state)
            # Reprocess ONLY a durable unfinished queue item. A crash after the
            # legacy manifest update must not strand its saved PDF unpublished.
            result = run_gmail(QueuedProvider(provider, item['message'], item, receipts_dir, lambda: atomic_json(path, state)), receipts_dir,
                               manifest_path, False, reprocess=True, resumable=True, pdf_only=True)[0]
            for value in result.get('planned_paths', []):
                source = safe_path(value)
                key = digest(source.read_bytes())
                entry = state['documents'].setdefault(key, {
                    'paths': [], 'logical_id': None, 'stage': 'downloaded'})
                relative = source.relative_to(safe_path(Path(receipts_dir) / state['account'])).as_posix()
                if relative not in entry['paths']:
                    entry['paths'].append(relative)
                item.setdefault('documents', [])
                if key not in item['documents']:
                    item['documents'].append(key)
            for key in item.get('documents', []):
                entry = state['documents'][key]
                if entry['stage'] != 'downloaded':
                    continue
                for value in item.get('known_saved_sources', []):
                    source = safe_path(value)
                    if source.is_relative_to(safe_path(Path(receipts_dir)/state['account'])) and source.is_file() and digest(source.read_bytes()) == key:
                        entry.update(stage='legacy_published', legacy_source_evidence={'source_sha256':key})
                        break
            item['ingestion_result'] = result['status']
            if result.get('retry_required'):
                item['reason'] = 'ingestion_retry'
            if not result.get('retry_required'):
                item.pop('reason', None)
                item['stage'] = 'ingested'  # Does NOT mean published.
            atomic_json(path, state)
        except Exception:
            # Never persist exception text, subject, donor content in logs.
            item['reason'] = 'ingestion_retry'
            atomic_json(path, state)
    from src.manifest import load_attachment_origins
    origins = load_attachment_origins(manifest_path, state['account'])
    for sha, entry in state['documents'].items():
        if sha in origins:
            entry['attachment_origins'] = origins[sha]
    for entry in state['documents'].values():
        if 'metadata' in entry or entry['stage'] == 'legacy_published':
            continue
        base = safe_path(Path(receipts_dir) / state['account'])
        paths = [safe_path(base / relative) for relative in entry['paths']]
        if any(not candidate.is_relative_to(base) for candidate in paths):
            raise SyncError('document_outside_account', 'Queued source is outside the account')
        source = next((candidate for candidate in paths if candidate.is_file()), None)
        if source is None:
            entry.update(stage='review', reason='document_source_unavailable')
            atomic_json(path, state)
            continue
        key = digest(source.read_bytes())
        if state['documents'].get(key) is not entry:
            raise SyncError('document_changed', 'Queued original PDF changed')
        meta = _extract_row(source, state['account'], config, now.year)
        entry.update(metadata=asdict(meta), stage='extracted', table_amount_revision=1)
        entry.pop('reason', None)
        atomic_json(path, state)


def managed_rows(wb):
    """Read only actual ledger identities; historical baseline receives no IDs."""
    entries, rows = {}, {}
    if INDEX in wb.sheetnames:
        index = wb[INDEX]
        if index['A1'].value != 'Donation ID' or index['B1'].value != 'Publication v1':
            raise SyncError('invalid_publication_index', 'Publication index invalid')
        for key, payload in index.iter_rows(min_row=2, max_col=2, values_only=True):
            if key is None and payload is None:
                continue
            if not key or key in entries:
                raise SyncError('invalid_publication_index', 'Duplicate publication identity')
            entries[key] = json.loads(payload)
    rebind_published_locations(wb, entries)
    for ws in donation_sheets(wb):
        col = headers(ws).get(ID_HEADER)
        if col:
            for row in range(2, ws.max_row + 1):
                if archived_duplicate(ws, row):
                    continue
                key = ws.cell(row, col).value
                if key:
                    if key not in entries or key in rows:
                        raise SyncError('invalid_publication_index', 'Unindexed or duplicate donation identity')
                    rows[key] = (ws, row)
    if set(rows) != set(entries):
        raise SyncError('removed_published_row', 'Managed donation missing; restore locally')
    validate_historical_archives(wb, entries)
    return entries, rows


def all_rows(wb):
    # Ephemeral coordinates for totals, NEVER persisted as donation identities.
    return {(ws.title, row): (ws, row) for ws in donation_sheets(wb)
            for row in range(2, ws.max_row + 1)
            if not archived_duplicate(ws, row)
            and any(ws.cell(row, col).value is not None for col in headers(ws).values())}


def targeted_history_conflict(wb, managed, candidate):
    owned = {(ws.title, row) for ws, row in managed.values()}
    for coord, (ws, row) in all_rows(wb).items():
        if coord not in owned and _possibly_same(_identity(candidate), _identity(metadata(ws, row))):
            # Similarity, even a complete metadata match, is NOT physical identity.
            # No source scan, fabricated legacy IDs, merging or dropping history.
            return True
    return False


def candidate_identity_uncertain(wb, managed, candidate):
    """Withhold annual additions lacking reliable identity exclusion evidence.

    Matching/consolidation still belongs to receipt_deduplication. Missing identity
    is not evidence that a receipt is new, especially around corrected old rows.
    """
    key=_identity(candidate)
    if any(value is None for value in key):
        return True
    owned={(ws.title,row) for ws,row in managed.values()}
    for coord,(ws,row) in all_rows(wb).items():
        if coord in owned:
            continue
        old=_identity(metadata(ws,row))
        if old[0]==key[0] and old[1] is None:
            return True
        if old[0] is None and old[1] in (None,key[1]):
            return True
    return False


def publish_pending(state, state_path, receipts_dir, reports_dir, root, config, allowed, now, baseline_year=None, public_registry=None, identity_scope=None):
    counts = Counter(published=0, reports_updated=0, review=0, api_requests=0)
    from src.sync_state import stage_saved_table_amounts, stage_saved_tax_layout
    stage_saved_table_amounts(state, receipts_dir, state['account'], config, allowed)
    stage_saved_tax_layout(state, receipts_dir, state['account'], config, allowed)
    records = [(Path(key), effective_metadata(entry)) for key, entry in sorted(state['documents'].items())
               if 'metadata' in entry and entry['stage'] not in {'review_existing','review_not_donation'}
               and not (entry['stage']=='review_deferred_year' and parsed_date(effective_metadata(entry),now)
                        and parsed_date(effective_metadata(entry),now).year not in allowed)]
    distinct=[(path,meta) for path,meta in records if state['documents'][str(path)].get('review_decision',{}).get('action')=='distinct']
    normal=[(path,meta) for path,meta in records if state['documents'][str(path)].get('review_decision',{}).get('action')!='distinct']
    groups = consolidate_receipts(normal) + [Donation(meta,(path,)) for path,meta in distinct]
    books = {}
    live = None
    try:
        if baseline_year is not None:
            output = safe_path(Path(reports_dir)/state['account']/str(baseline_year)/f'donation_summary_{baseline_year}.xlsx')
            if not output.is_file():
                raise SyncError('baseline_workbook_missing', 'Existing annual workbook required')
            if any(output.with_name(name).exists() for name in ('~$'+output.name, '~$'+output.name[2:])):
                raise SyncError('workbook_open', 'Close Excel before annual baseline')
            wb = load(output)
            from src.annual_baseline import check_layout
            check_layout(wb)
            original = signature(wb)
            entries, rows = managed_rows(wb)
            from src.sync_review import check_existing
            for entry in state['documents'].values():
                if entry['stage']=='review_existing' and entry['review_decision']['proof']['tax_year']==baseline_year:
                    check_existing(entry,wb)
            books[baseline_year] = dict(wb=wb, path=output, before=output.read_bytes(), original=original,
                entries=entries, rows=rows, additions={}, assignments={}, rechecks={})
        for group in groups:
            keys = [str(p) for p in group.sources]
            docs = [state['documents'][key] for key in keys]
            if all(d.get('stage') == 'published' for d in docs):
                continue
            date = parsed_date(group.metadata, now)
            reason = ('invalid_donation_date' if date is None else
                      'unauthorized_tax_year' if date.year not in allowed else
                      'invalid_donation_amount' if receipt_amount(group.metadata.amount) is None else
                      'candidate_duplicate_review' if group.suspected_group is not None else None)
            if reason:
                for doc in docs:
                    if doc.get('stage', '').startswith('published'):
                        doc['recheck_deferred_reason'] = reason
                        counts['deferred_rechecks'] += 1
                    else:
                        doc.update(stage='review', reason=reason)
                        counts['review'] += 1
                continue
            year = date.year
            if year not in books:
                output = safe_path(Path(reports_dir) / state['account'] / str(year) / f'donation_summary_{year}.xlsx')
                if any(output.with_name(name).exists() for name in
                       ('~$' + output.name, '~$' + output.name[2:])):
                    raise SyncError('workbook_open', 'Close Excel before retry; pending work retained')
                before = output.read_bytes() if output.exists() else None
                wb = load(output) if before else openpyxl.Workbook()
                if not before:
                    wb.remove(wb.active)
                original = signature(wb)
                entries, rows = managed_rows(wb)
                books[year] = dict(wb=wb, path=output, before=before, original=original,
                                   entries=entries, rows=rows, additions={}, assignments={}, rechecks={})
            book = books[year]
            matched = {key for key, entry in book['entries'].items() if set(entry['documents']).intersection(keys)}
            known = {doc['logical_id'] for doc in docs if doc.get('logical_id')}
            if len(matched | known) > 1:
                if matched and known <= matched and same_historical_review(book['entries'], matched):
                    # Existing explicitly unresolved multi-row source association:
                    # preserve both publications and queue a review, never select
                    # one arbitrarily or insert another donation on Delta resume.
                    for doc in docs:
                        doc.update(stage='review', reason='historical_duplicate_candidate',
                                   logical_id=None, historical_review_ids=sorted(matched))
                    counts['review'] += len(docs)
                    continue
                if matched or any(d.get('stage','').startswith('published') for d in docs):
                    raise SyncError('conflicting_publications', 'Candidate references multiple publications')
                # Pending sidecar IDs are NOT successful workbook publications.
                # Retain an unresolved candidate instead of blocking old rows.
                for doc in docs:doc.update(stage='review',reason='candidate_duplicate_review')
                counts['review'] += 1
                continue
            donation_id = next(iter(matched | known), identity(identity_scope or state['account'], keys))
            if donation_id not in book['entries']:
                if known and any(doc.get('stage', '').startswith('published') for doc in docs):
                    raise SyncError('removed_published_row', 'Previously published donation absent')
                explicitly_distinct=all(doc.get('review_decision',{}).get('action')=='distinct' for doc in docs)
                historical_conflict=targeted_history_conflict(book['wb'], book['rows'], group.metadata)
                needs_strict_check=baseline_year is not None or any(d.get('needs_identity_review') for d in docs)
                identity_uncertain=needs_strict_check and candidate_identity_uncertain(book['wb'],book['rows'],group.metadata)
                if not explicitly_distinct and (historical_conflict or identity_uncertain):
                    for doc in docs:
                        doc.update(stage='review', reason='historical_duplicate_candidate')
                        if identity_uncertain:doc['needs_identity_review']=True
                    counts['review'] += 1
                    continue
                paths = document_paths(receipts_dir, state['account'], state, keys)
                if not paths:
                    for doc in docs:
                        doc.update(stage='review', reason='document_source_unavailable')
                    counts['review'] += 1
                    continue
                from src.sync_workbook import preferred_group_paths
                preferred = preferred_group_paths(paths, state['account'], state, group.metadata)
                meta = _finalize_row(group.metadata, config, year)
                if preferred != paths:
                    meta.file_name = preferred[0].name
                book['additions'][donation_id] = Donation(meta, preferred)
            else:
                book['rechecks'][donation_id] = book['rows'][donation_id]
            book['assignments'][donation_id] = keys
            for doc in docs:
                doc.update(logical_id=donation_id, stage='validated')
        # Open only authorized existing publications with a staged source repair.
        for item in state['documents'].values():
            if not (item.get('table_amount_candidate') or item.get('tax_layout_candidate')) or not item.get('logical_id') or not item.get('metadata'):
                continue
            date = parsed_date(effective_metadata(item), now)
            if date is None or date.year not in allowed or date.year in books:
                continue
            output = safe_path(Path(reports_dir) / state['account'] / str(date.year) / f'donation_summary_{date.year}.xlsx')
            if not output.is_file():
                continue
            if any(output.with_name(name).exists() for name in ('~$'+output.name, '~$'+output.name[2:])):
                raise SyncError('workbook_open', 'Close Excel before retry; pending work retained')
            wb = load(output)
            original = signature(wb)
            entries, rows = managed_rows(wb)
            if item['logical_id'] not in entries:
                wb.close()
                continue
            books[date.year] = dict(wb=wb, path=output, before=output.read_bytes(), original=original,
                entries=entries, rows=rows, additions={}, assignments={}, rechecks={})
        # Persist candidates and review BEFORE API/publication; validated != published.
        atomic_json(state_path, state)
        if baseline_year is not None or any(b['assignments'] for b in books.values()):
            live = LiveSection46(root / 'section46-live.json', OrganizationCache(public_registry or root / 'organizations.json'))
            dates = defaultdict(set)
            for book in books.values():
                metas = [item.metadata for item in book['additions'].values()]
                metas += [metadata(ws, row) for ws, row in book['rechecks'].values()]
                if book is books.get(baseline_year):
                    metas += [metadata(ws, row) for ws, row in all_rows(book['wb']).values()]
                for meta in metas:
                    date = parsed_date(meta, now)
                    if date and re.fullmatch(r'[0-9]{9}', meta.registration_number):
                        dates[meta.registration_number].add(date)
            for number, donated_dates in dates.items():
                evidence = [live.registry.lookup_tax(number, date) for date in donated_dates]
                covered = all(e['section46_status'] != 'unknown' and not e['stale'] and not e['conflict'] for e in evidence)
                live.prepare(number, identity_only=covered)
        for year, book in sorted(books.items()):
            from src.sync_workbook import apply_verified_table_amounts, apply_verified_tax_layout
            repaired = apply_verified_table_amounts(book['entries'], book['rows'], state,
                                                   Path(receipts_dir) / state['account'], year)
            tax_repaired = apply_verified_tax_layout(book['entries'], book['rows'], state,
                                                    Path(receipts_dir) / state['account'], year)
            if not book['assignments'] and year != baseline_year and not repaired and not tax_repaired:
                continue
            counts['amount_repairs'] += repaired
            counts['tax_layout_repairs'] += tax_repaired
            if live is None:
                live = LiveSection46(root / 'section46-live.json', OrganizationCache(public_registry or root / 'organizations.json'))
            wb, entries = book['wb'], book['entries']
            if year == baseline_year:
                audit=wb['Sync review decisions'] if 'Sync review decisions' in wb.sheetnames else wb.create_sheet('Sync review decisions')
                if audit['A1'].value is None:
                    for col,label in enumerate(['Document SHA256','Decision JSON'],1):audit.cell(1,col,label)
                existing={(r[0],r[1]) for r in audit.iter_rows(min_row=2,values_only=True)}
                for key,entry in state['documents'].items():
                    for decision in entry.get('review_history',[]):
                        value=json.dumps(decision,sort_keys=True,ensure_ascii=True)
                        if (key,value) not in existing:
                            audit.append([key,value]);existing.add((key,value))
                from src.annual_baseline import enrich_existing
                with TemporaryDirectory(prefix='donation-annual-') as folder:
                    baseline_entries = enrich_existing(wb, all_rows(wb), root, live, config, Path(folder), digest(book['before']))
                for key, (ws, row) in book['rows'].items():
                    entries[key]['managed'].update(baseline_entries[(ws.title, row)]['managed'])
            # New/pending rows retain stable publication identity.
            changed_rows = dict(book['rechecks'])
            with TemporaryDirectory(prefix='donation-delta-') as folder:
                enrich_candidates(wb, entries, changed_rows, book['additions'], root, live, config,
                                  Path(folder), digest(book['before']) if book['before'] else None)
            book['rows'].update(changed_rows)
            for key, doc_keys in book['assignments'].items():
                ws, row = book['rows'][key]
                for doc in doc_keys:
                    state['documents'][doc].update(stage='enriched',
                        identity_status='official_registry' if live.registry.official_identity(
                            metadata(ws, row).registration_number) else 'unverified',
                        section46_status=ws.cell(row, headers(ws)[LIVE_HEADERS[0]]).value)
            atomic_json(state_path, state)
            for key, docs in book['assignments'].items():
                entries[key]['documents'] = sorted(set(entries[key]['documents']) | set(docs))
            from src.sync_workbook import apply_group_copy_preference
            # Only newly assigned/rechecked Delta publications, not unrelated historical rows.
            assigned_rows = {key: book['rows'][key] for key in book['assignments']}
            preferred_rows = apply_group_copy_preference(entries, assigned_rows, state, Path(receipts_dir) / state['account'])
            from src.report_layout import _pdf_audit, _report_field_columns, _write_pdf_link
            for key in preferred_rows:
                ws, row = book['rows'][key]
                cell = ws.cell(row, _report_field_columns(ws)['file_name'])
                current = cell.hyperlink.target if cell.hyperlink else None
                prior = _pdf_audit(wb).get(f'{ws.title}!{cell.coordinate}', (None, {}))[1]
                sources = document_paths(receipts_dir, state['account'], state, entries[key]['documents'])
                preferred = document_paths(receipts_dir, state['account'], state, [entries[key]['representative_document']])
                if (preferred and cell.comment is None and cell.data_type != 'f'
                        and current and (current == prior.get('target') or key in book['additions'])
                        and any(current in (path.resolve().as_uri(), str(path.resolve())) for path in sources)):
                    _write_pdf_link(wb, cell, key, (preferred[0], 'verified', 'dedup_representative'),
                                    book['rows'], entries, counts)
            write_sources(wb, entries, book['rows'], state, receipts_dir, state['account'])
            if baseline_year is not None or state.get('annual_completed'):
                from src.annual_baseline import normalize_months, check_layout
                check_layout(wb)
                normalize_months(wb)
            archive = None
            if year == baseline_year and 'סיכום' in wb.sheetnames:
                archive = wb.copy_worksheet(wb['סיכום'])
                archive.title = 'סיכום לפני baseline'
            write_totals(wb, all_rows(wb), INDEX in wb.sheetnames, preserve_formulas=bool(repaired or tax_repaired))
            if archive is not None:
                from src.sync_workbook import summary_cells
                if any(summary_cells(ws) == summary_cells(archive) for ws in wb if ws is not archive):
                    wb.remove(archive)
                else:
                    archive.sheet_state = 'hidden'
            refresh_published_rows(entries, book['rows'], state, config, year)
            write_index(wb, entries)
            if summary_name(year) in wb:
                stamp_published_locations(wb, entries, book['rows'])
                write_summary(wb, year, book['rows'])
            if signature(wb) != book['original']:
                if (repaired or tax_repaired or baseline_year is not None or state.get('annual_completed')
                        or INDEX in wb and wb[INDEX]['D1'].value == 'Full report layout v1'):
                    replace_verified(book['path'], book['before'], wb,
                        backup_dir=root/'report-backups'/state['account']/str(year))
                else:
                    replace_verified(book['path'], book['before'], wb)
                counts['reports_updated'] += 1
            # Workbook ledger was committed atomically FIRST. A crash before
            # this acknowledgement recovers by matched document hashes above.
            counts['published'] += len(book['additions'])
            for key, docs in book['assignments'].items():
                ws, row = book['rows'][key]
                status = ws.cell(row, headers(ws)[LIVE_HEADERS[0]]).value
                for doc in docs:
                    state['documents'][doc].update(stage='published' if (status in ('מאושר', 'אין אישור מוכח לתאריך')
                                                   and state['documents'][doc].get('identity_status') == 'official_registry')
                                                   else 'published_recheck', section46_status=status)
                    state['documents'][doc].pop('reason', None)
                    state['documents'][doc].pop('recheck_deferred_reason', None)
            atomic_json(state_path, state)
        if live:
            counts.update(live.counts)
        return dict(counts)
    finally:
        for book in books.values():
            book['wb'].close()


def run_delta(*, provider, receipts_dir, reports_dir, accounts_dir, manifest_path,
              account, year=None, root=None, plan=False, allowed_years=None, now=None, baseline=False, resume_pending=False, public_registry=None, identity_scope=None):
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None or not re.fullmatch(r'[A-Za-z0-9_-]+', account):
        raise ValueError('Invalid account or clock')
    if safe_path(provider.account_dir) != safe_path(Path(accounts_dir) / account):
        raise ValueError('Provider account mismatch')
    root = registry_root(root)
    config = load_account_config(accounts_dir, account)
    zone = getattr(config, 'operating_timezone', 'Asia/Jerusalem')
    current = now.astimezone(ZoneInfo(zone)).year
    allowed = set(allowed_years) if allowed_years is not None else ({year} if year else {current - 1, current})
    if (not allowed or any(type(y) is not int or not 1900 <= y <= 9998 for y in allowed)
            or (year is not None and year not in allowed)):
        raise SyncError('invalid_year_authorization', 'Invalid authorized years')
    if baseline and (year is None or allowed != {year}):
        raise SyncError('annual_scope', 'Annual baseline requires exactly one explicit year')
    if resume_pending and not baseline:
        raise SyncError('resume_requires_baseline','Resume pending requires the explicit annual baseline')
    state_path = safe_path(root / f'delta-{account}.json')
    baseline_output = safe_path(Path(reports_dir)/account/str(year)/f'donation_summary_{year}.xlsx') if baseline else None

    def inspect():
        state = read_state(state_path, account)
        if not baseline and state.get('annual_pending_boundary'):
            raise SyncError('annual_baseline_incomplete', 'Resume annual baseline before delta')
        start, query = discovery_window(state, now, zone)
        details = {}
        if baseline:
            from src.annual_baseline import check_layout, FINAL_HEADERS
            if not baseline_output.is_file():
                raise SyncError('baseline_workbook_missing', 'Existing annual workbook required')
            wb = load(baseline_output)
            try:
                check_layout(wb)
                managed_rows(wb)
                details = {'baseline_rows': len(all_rows(wb)), 'final_columns': FINAL_HEADERS}
            finally:
                wb.close()
            start = datetime(year, 1, 1, tzinfo=ZoneInfo(zone))
            query = f'has:attachment after:{int(start.timestamp())-1} before:{int(now.timestamp())+1}'
        active = state.get('discovery')
        if active and not resume_pending:
            if active.get('workflow') != ('annual_baseline' if baseline else 'delta') or active.get('year') != year:
                raise SyncError('discovery_mode_mismatch', 'Resume the same interrupted sync mode/year')
            query = active['query']
            start = datetime.fromisoformat(active['start'])
        boundary=None
        if resume_pending:
            if active:
                raise SyncError('gmail_discovery_incomplete', 'Resume original sync before queue-only recovery')
            boundary=state.get('annual_pending_boundary') or state.get('checkpoint')
            if boundary is None:
                raise SyncError('missing_discovery_boundary','No persisted completed discovery to resume')
            stamp=datetime.fromisoformat(boundary)
            if stamp.utcoffset() is None or stamp>now:
                raise SyncError('invalid_discovery_boundary','Invalid persisted discovery boundary')
            query=None  # No Gmail query is executed in queue-only recovery.
        return state, {'plan_only': True, 'initial_discovery': state['checkpoint'] is None and not resume_pending,
                       'checkpoint': state['checkpoint'], 'search_start': start.isoformat(),
                       'query': query, 'operating_timezone': zone, 'overlap_hours': 48,
                       'authorized_years': sorted(allowed),
                       'pending_messages': sum(m['stage'] == 'discovered' for m in state['messages'].values()),
                       'document_stages': dict(Counter(d['stage'] for d in state['documents'].values())),
                       'discovery_progress': discovery_counts(state),
                       'api_requests': 0, 'workflow': 'annual_baseline' if baseline else 'delta',
                       'resume_pending':resume_pending,'gmail_discovery_planned':not resume_pending,'reused_discovery_boundary':boundary,
                       'review_reason_counts':dict(Counter(d.get('reason') if d.get('reason') in {'historical_duplicate_candidate','candidate_duplicate_review','invalid_donation_date','unauthorized_tax_year','invalid_donation_amount','document_source_unavailable'} else 'review_required' for d in state['documents'].values() if d['stage']=='review')),
                       **details}
    if plan:
        return inspect()[1]  # NO lock/file creation, extraction, Gmail or API.
    if not safe_path(public_registry or root / 'organizations.json').is_file():
        raise SyncError('missing_registry', 'Existing operational registry required')
    with operational_lock(root), redirect_stdout(io.StringIO()):
        state, info = inspect()
        if not resume_pending:provider._query = info['query']
        discovery_boundary = now.isoformat()
        if resume_pending:
            messages = []
        elif callable(getattr(type(provider), 'fetch_emails_resumable', None)):
            progress = state.setdefault('discovery', {
                'query': info['query'], 'start': info['search_start'], 'boundary': discovery_boundary,
                'workflow': 'annual_baseline' if baseline else 'delta', 'year': year,
                'ids': {}, 'pages': 0, 'listed': False})
            atomic_json(state_path, state)
            discovery_boundary = progress['boundary']
            try:
                messages = provider.fetch_emails_resumable(progress, lambda: atomic_json(state_path, state))
            except Exception as exc:
                counts = discovery_counts(state)
                raise SyncError('gmail_discovery_interrupted',
                    'Gmail discovery stopped; pending state retained; rerun same command after resolving reason='
                    + discovery_error_reason(exc) + '; '
                    + ' '.join(f'{k}={v}' for k, v in counts.items())) from None
        else:
            messages = provider.fetch_emails()
        processed_entries = ({e['message_id']: e for e in load_manifest(manifest_path) if e['account'] == account}
                             if not resume_pending else {})
        processed = set(processed_entries)
        known_sources = set()
        if baseline and not resume_pending:
            from src.annual_baseline import linked_sources, stored_source
            wb = load(baseline_output)
            try:
                known_sources = linked_sources(wb, baseline_output)
            finally:
                wb.close()
            # Only candidate-specific legacy messages require looking for link
            # evidence in other existing canonical annual reports. No PDF scan.
            legacy_in_window=[processed_entries[msg['message_id']] for msg in messages if msg.get('message_id') in processed_entries]
            if any(e.get('saved_files') and any(stored_source(v,Path.cwd()) not in known_sources for v in e['saved_files'])
                   for e in legacy_in_window):
                for folder in baseline_output.parent.parent.iterdir():
                    other=folder/f'donation_summary_{folder.name}.xlsx'
                    if folder.name.isdigit() and other.is_file() and other != baseline_output:
                        try:
                            wb=load(other)
                        except Exception:
                            # Unrelated unreadable history is not proof of either
                            # publication or missing PDFs. Candidate remains reviewable.
                            continue
                        try:known_sources.update(linked_sources(wb,other))
                        finally:wb.close()
        for message in messages:
            key = message.get('message_id')
            if not isinstance(key, str) or not key or key == 'unknown':
                raise SyncError('invalid_message_identity', 'Discovery has no stable Gmail identity')
            if baseline and key in processed and (key not in state['messages'] or state['messages'][key]['stage'] == 'legacy_processed'):
                saved = processed_entries[key].get('saved_files', [])
                # Candidate-specific path evidence, no global PDF reconstruction.
                linked = saved and all(stored_source(value, Path.cwd()) in known_sources for value in saved)
                if not linked:
                    state['messages'][key] = {'stage': 'discovered', 'message': message, 'documents': [],
                        'known_saved_sources':[stored_source(v,Path.cwd()) for v in saved if stored_source(v,Path.cwd()) in known_sources]}
            if key in state['messages'] and state['messages'][key]['stage'] == 'discovered':
                state['messages'][key]['message'] = message
            if key not in state['messages']:
                # Existing ingestion record is not proof of financial publication.
                # Preserve baseline; do not re-extract/re-import its original PDFs.
                state['messages'][key] = ({'stage': 'legacy_processed'} if key in processed else
                                          {'stage': 'discovered', 'message': message, 'documents': []})
        if baseline:
            state['annual_pending_boundary'] = info['reused_discovery_boundary'] if resume_pending else discovery_boundary
        else:
            state['checkpoint'] = discovery_boundary
        state.pop('discovery', None)
        # The queue and watermark are ONE atomic commit. Publication may fail;
        # all undispatched message descriptors survive outside the overlap.
        atomic_json(state_path, state)
        ingest(state, state_path, provider, receipts_dir, manifest_path, config, now, allow_downloads=not resume_pending)
        result = publish_pending(state, state_path, receipts_dir, reports_dir, root, config, allowed, now, baseline_year=year if baseline else None, public_registry=public_registry, identity_scope=identity_scope)
        result['unavailable_messages'] = sum(m.get('reason') == 'message_not_found' for m in state['messages'].values())
        result['pending_messages'] = sum(m['stage'] == 'discovered' for m in state['messages'].values())
        result['pending_review'] = sum(d['stage'] == 'review' for d in state['documents'].values())
        result['deferred_year_candidates'] = sum(d['stage']=='review_deferred_year' for d in state['documents'].values())
        result['explicit_existing_decisions'] = sum(d['stage']=='review_existing' for d in state['documents'].values())
        result['pending_recheck'] = sum(d['stage'] == 'published_recheck' for d in state['documents'].values())
        result['candidate_reconciliation_complete']=not (result['pending_messages'] or result['pending_review'] or result['deferred_year_candidates'])
        # Manual review is durable work, not a reason to withhold the enriched
        # authoritative baseline or to fail every later successful delta.
        if not baseline and (result['pending_messages'] or (result['pending_review'] and not state.get('annual_completed'))):
            raise SyncError('delta_pending_review_or_retry', 'Pending work retained; inspect private state before retry')
        if baseline:
            state['checkpoint'] = state.pop('annual_pending_boundary')
            state['annual_completed'] = {'year': year, 'boundary': state['checkpoint'],
                                         'report_sha256': digest(baseline_output.read_bytes()),
                                         'deferred_year_candidates': result['deferred_year_candidates'],
                                         'baseline_complete': True,
                                         'candidate_reconciliation_complete': result['candidate_reconciliation_complete'],
                                         'pending_review': result['pending_review'],
                                         'pending_messages': result['pending_messages'],
                                         'pending_recheck': result['pending_recheck']}
            atomic_json(state_path, state)
            result['baseline_complete'] = True
            result['checkpoint'] = state['checkpoint']
            result['discovery_reused']=resume_pending
        return result
