"""Unified ingestion, conservative consolidation, and transactional annual sync."""
from collections import Counter, defaultdict
from src.logger import stage, emit, metric, snapshot
from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import openpyxl

from src.account_config import load_account_config
from src.user_scope import annual_report
from src.annual_excel_summary import summary_name, write_summary
from src.gmail_runner import run_gmail
from src.organization_cache import OrganizationCache
from src.private_runtime import atomic_json, operational_lock, registry_root, safe_path
from src.receipt_deduplication import Donation
from src.receipt_summary import _finalize_row
from src.section46_live import LiveSection46
from src.sync_state import IDENTIFIER_REFRESH_VERSION, SyncError, catalog_documents, digest, document_paths, donation_date, effective_metadata, groups, identity
from src.sync_workbook import (bootstrap, enrich_candidates, load, replace_verified, signature,
                               write_index, write_sources, write_totals, stamp_published_locations, metadata, same_historical_review, classify_receipts, filter_verified_pdf_rows, sync_approved_identifiers, refresh_published_rows, apply_verified_table_amounts)


def historical_review_counts(consolidated, legacy, published_docs, *, require_source_binding=False):
    """Suspected equality is not source loss. Only NEW ambiguous bindings block.

    A purely historical component is already represented by separate published
    rows and can remain separate. A physical document not in that published
    mapping must not be assigned to an unresolved legacy row by guesswork.
    """
    components = defaultdict(set)
    counts = Counter(historical_review_groups=0, historical_review_rows=0,
                     unresolved_historical_document_groups=0)
    for donation in consolidated:
        keys = {str(path) for path in donation.sources}
        if donation.suspected_group is not None:
            components[donation.suspected_group].update(keys)
        elif require_source_binding and keys.intersection(legacy) and keys.difference(published_docs):
            # Equal extracted values are only a candidate match; they do
            # not prove that a new file belongs to an unbound workbook row.
            counts['unresolved_historical_document_groups'] += 1
    for keys in components.values():
        if keys.intersection(legacy):
            if keys.difference(published_docs):
                counts['unresolved_historical_document_groups'] += 1
            else:
                counts['historical_review_groups'] += 1
                counts['historical_review_rows'] += len(set().union(*(published_docs[key] for key in keys)))
    return counts


def publish(receipts_dir, reports_dir, accounts_dir, account, year, root, *, plan=False, allowed_years=None, full_report=False, resume_processing=False, refresh_base=False, public_registry=None, user=None, identity_scope=None, prepared_catalog=None, manual_review=None):
    """Reconcile ALL account documents and tax years; year is the requested report.

    Extraction catalog IDs may be pending. Only the index inside an atomically
    published workbook proves successful publication. No sidecar success flag.
    """
    stage("CONFIG")
    config = load_account_config(accounts_dir, account)
    manual = account == 'manual'
    if prepared_catalog is not None:
        catalog_path, state = prepared_catalog
    elif full_report and not plan:
        from src.full_report import prepare
        catalog_path, state = prepare(receipts_dir, reports_dir, accounts_dir, account, year, root,
                                      resume=resume_processing, refresh_base=refresh_base)
    else:
        catalog_path, state = catalog_documents(receipts_dir, account, root, config)
    if not plan and manual:
        from src.sync_state import stage_saved_tax_layout
        stage_saved_tax_layout(state, receipts_dir, account, config, {year})
    base = safe_path(Path(reports_dir) / account)
    years = {year}
    if not manual:
        for value in state['documents'].values():
            date = donation_date(effective_metadata(value))
            if date:
                years.add(date.year)
    if not manual and base.exists():
        years |= {int(p.name) for p in base.iterdir() if p.name.isdigit() and 1900 <= int(p.name) <= 9998
                  and (p / f'donation_summary_{p.name}.xlsx').is_file()}
    workbooks, locations, legacy, published_docs = {}, {}, {}, defaultdict(set)
    historical_entries = {}
    try:
        stage("WORKBOOK")
        for tax_year in sorted(years):
            output = annual_report(reports_dir, account, tax_year)
            before = output.read_bytes() if output.exists() else None
            wb = load(output) if before else openpyxl.Workbook()
            if before is None:
                wb.remove(wb.active)
            original = signature(wb)
            entries, rows, historical, indexed = bootstrap(wb, output, Path(receipts_dir) / account, account, state, reconcile_sources=full_report, identity_scope=identity_scope or account, scoped=user is not None, archive_backup_dir=root / "report-backups" / account / str(tax_year))
            workbooks[tax_year] = dict(wb=wb, path=output, before=before, original=original,
                                      entries=entries, rows=rows, indexed=indexed, additions={})
            legacy.update(historical)
            for donation_id, entry in entries.items():
                if donation_id in locations:
                    raise ValueError('Donation published in multiple tax-year workbooks')
                locations[donation_id] = tax_year
                for doc in entry['documents'] or ['legacy:' + donation_id]:
                    owners = published_docs[doc] | {donation_id}
                    historical_entries[donation_id] = entry
                    if len(owners) > 1 and not same_historical_review(historical_entries, owners):
                        raise ValueError('Physical receipt linked to multiple historical donations')
                    published_docs[doc].add(donation_id)
        counts = Counter(published=0, pending_date=0, reports_updated=0, cross_year=0)
        counts.update(state.get('extraction_counts',{}))
        counts['correction_proposals_pending'] = sum(
            len(refresh.get('proposals', {})) for item in state['documents'].values()
            if (refresh := item.get('identifier_refresh', {})).get('year') == year
            and refresh.get('version') == IDENTIFIER_REFRESH_VERSION
        )
        for book in workbooks.values():
            counts.update(getattr(book['wb'],'_historical_merge_counts',{}))
        assignments = defaultdict(set)
        stage('DEDUP')
        consolidated = ([Donation(effective_metadata(item),(Path(key),))
                         for key,item in state['documents'].items()] +
                        [Donation(meta,(Path(key),)) for key,meta in legacy.items()]
                        if full_report and year==2026 else groups(state, legacy))
        counts['merged_groups']+=sum(len(g.sources)>1 and g.suspected_group is None for g in consolidated)
        counts['merged_copies']+=sum(len(g.sources)-1 for g in consolidated if g.suspected_group is None)
        counts['suspected_groups']=len({g.suspected_group for g in consolidated if g.suspected_group is not None})
        metric('merged_groups', sum(len(g.sources)>1 and g.suspected_group is None for g in consolidated))
        metric('merged_copies', sum(len(g.sources)-1 for g in consolidated if g.suspected_group is None))
        metric('suspected_groups', len({g.suspected_group for g in consolidated if g.suspected_group is not None}), absolute=True)
        snapshot('DEDUP', 'complete', 'merged_groups', 'merged_copies', 'suspected_groups')
        # Full 2026 publishes from the SHA-owned rows, but still needs the
        # historical candidate graph to check unresolved document bindings.
        review_groups = groups(state, legacy) if full_report and year == 2026 and legacy else consolidated
        review = historical_review_counts(review_groups, legacy, published_docs,
                                          require_source_binding=full_report)
        if review['unresolved_historical_document_groups']:
            raise SyncError('historical_source_conflict',
                            'Ambiguous historical/document binding; source absence has not been established')
        counts.update(review)
        collision_rows = {key for key, entry in historical_entries.items() if entry.get('historical_review')}
        counts['historical_review_rows'] = max(counts['historical_review_rows'], len(collision_rows))
        metric('historical_review_rows', counts['historical_review_rows'], absolute=True)
        counts['historical_review_groups'] = max(counts['historical_review_groups'], len({historical_entries[key]['historical_review'] for key in collision_rows}))
        metric('historical_review_groups', counts['historical_review_groups'], absolute=True)
        emit('DEDUP', 'complete', historical_review_rows=counts['historical_review_rows'],
             historical_review_groups=counts['historical_review_groups'])
        counts['historical_merged_copies'] = sum(len(e.get('historical_aliases', [])) for e in historical_entries.values())
        for group in consolidated:
            if manual:
                donated = donation_date(group.metadata)
                if donated is None or donated.year != year:
                    continue
            keys = {str(p) for p in group.sources}
            published = set().union(*(published_docs[k] for k in keys if k in published_docs))
            if len(published) > 1:
                if not same_historical_review(historical_entries, published):
                    raise SyncError('historical_consolidation_conflict', 'Historical donations now match; manual reconciliation required before publication')
                # These historical rows were explicitly retained by existing
                # Dedup/manual-conflict review. Preserve the many-to-many source
                # association; never choose a row or create a third donation.
                physical = keys.intersection(state['documents'])
                for donation_id in published:
                    assignments[donation_id] |= physical
                for key in physical:
                    state['documents'][key]['logical_id'] = None
                    state['documents'][key]['historical_review_ids'] = sorted(published)
                continue
            if published:
                donation_id = published.pop()
                tax_year = locations[donation_id]
            else:
                known = {state['documents'][k]['logical_id'] for k in keys
                         if k in state['documents'] and state['documents'][k]['logical_id']}
                donation_id = min(known) if known else identity(identity_scope or account, keys)
                date = donation_date(group.metadata)
                if not date:
                    counts['pending_date'] += 1
                    continue
                tax_year = date.year
                if donation_id in locations:
                    # Previously consolidated group split by conflicting new evidence.
                    raise ValueError('Consolidation changed; manual review required')
                paths = document_paths(receipts_dir, account, state, keys)
                if not paths:
                    raise ValueError('Pending donation has no recoverable original document')
                from src.sync_workbook import preferred_group_paths
                preferred = preferred_group_paths(paths, account, state, group.metadata)
                meta = _finalize_row(replace(group.metadata), config, tax_year,
                                     duplicate_review=group.suspected_group is not None)
                if preferred != paths:
                    meta.file_name = preferred[0].name
                paths = preferred
                if group.suspected_group is not None:
                    meta.notes += '; חשד לכפילות — נדרשת בדיקה ידנית'
                workbooks[tax_year]['additions'][donation_id] = Donation(meta, paths, group.suspected_group)
                locations[donation_id] = tax_year
                counts['published'] += 1
                counts['cross_year'] += tax_year != year
            assignments[donation_id] |= {k for k in keys if k in state['documents']}
            for key in keys:
                if key in state['documents']:
                    state['documents'][key]['logical_id'] = donation_id
        # Gmail arrival can reveal a year absent from the disk-only plan. Guard
        # the COMPLETE write set before ANY official request or XLSX publication.
        affected_years = {y for y, book in workbooks.items() if book['rows'] or book['additions']}
        authorized = {year} if allowed_years is None else set(allowed_years)
        if full_report and affected_years - {year}:
            raise SyncError('full_single_canonical', 'Full publishes exactly the requested annual canonical')
        if not affected_years <= authorized:
            raise SyncError('unauthorized_tax_year', 'Tax year not authorized; ingestion retained, no workbook published')
        if plan:
            return {'plan_only': True, 'pending_date': counts['pending_date'],
                    'years': {str(y): {'historical_rows': len(b['rows']),
                                       'new_donations': len(b['additions']),
                                       'migration': not b['indexed']}
                              for y, b in workbooks.items() if b['rows'] or b['additions']},
                    'api_requests': 0, **dict(review)}
        if full_report and counts['pending_date']:
            raise SyncError('pending_donation_date', 'Full base retains undated sources; publication blocked')
        stage('REGISTRY')
        registry = OrganizationCache(public_registry or root / 'organizations.json')
        emit('REGISTRY', 'cache_loaded')
        stage('SECTION46')
        live = LiveSection46(root / 'section46-live.json', registry)
        # Prepare each organization against ALL tax years before rendering any
        # one year; a cache-covered early year must not suppress a needed check
        # for another year through LiveSection46.seen.
        dates, names = defaultdict(set), {}
        for book in workbooks.values():
            metas = [metadata(ws, row) for ws, row in book['rows'].values()]
            metas += [item.metadata for item in book['additions'].values()]
            for meta in metas:
                date = donation_date(meta)
                number = meta.registration_number.strip()
                if date and re.fullmatch(r'[0-9]{9}', number):
                    dates[number].add(date)
                    names[number] = meta.organization_name
        emit('SECTION46', 'begin', organizations_total=len(dates))
        for number_index, (number, donated_dates) in enumerate([] if full_report else dates.items(), 1):
            cached = [live.registry.lookup_tax(number, date) for date in donated_dates]
            covered = all(r['section46_status'] != 'unknown' and not r['stale'] and not r['conflict'] for r in cached)
            live.prepare(number, names[number], identity_only=covered)
            if number_index % 10 == 0 or number_index == len(dates):
                emit('SECTION46', 'batch', organizations_completed=number_index, organizations_total=len(dates),
                     api_requests=live.counts['api_requests'], api_errors=live.counts['api_errors'], cache_hits=live.counts['cache_hits'])
        # Pending logical assignments are recoverable, but confer NO publication.
        atomic_json(catalog_path, state)
        for tax_year, book in workbooks.items():
            wb, entries, rows = book['wb'], book['entries'], book['rows']
            if not rows and not book['additions'] and not ((full_report or manual) and tax_year == year):
                continue
            stage('EXCEL')
            corrections = Counter()
            if (full_report or manual) and tax_year == year:
                corrections['staged'] = sync_approved_identifiers(
                    entries, rows, state, assignments, year=tax_year,
                    legacy=account == 'primary' and tax_year == 2026,
                )
            if full_report or manual:
                corrections['staged'] += apply_verified_table_amounts(entries, rows, state, Path(receipts_dir) / account, tax_year)
                from src.sync_workbook import apply_verified_tax_layout
                corrections['staged'] += apply_verified_tax_layout(entries, rows, state, Path(receipts_dir) / account, tax_year)
            if full_report:
                # The base XLSX is complete before this phase. Keep global Dedup
                # and organization date coverage; enrich existing report rows one
                # month at a time with the established renderer/ownership rules.
                monthly = defaultdict(lambda: ({}, {}))
                for key, pair in rows.items():
                    date = donation_date(metadata(*pair))
                    monthly[date.month if date else 0][0][key] = pair
                for key, donation in book['additions'].items():
                    date = donation_date(donation.metadata)
                    monthly[date.month if date else 0][1][key] = donation
                for month, (month_rows, additions) in sorted(monthly.items()):
                    stage('EXCEL', month=month)
                    metas = [metadata(*pair) for pair in month_rows.values()]
                    metas += [d.metadata for d in additions.values()]
                    for number in dict.fromkeys(m.registration_number.strip() for m in metas):
                        if number not in dates:
                            continue
                        cached = [live.registry.lookup_tax(number, date) for date in dates[number]]
                        covered = all(r['section46_status'] != 'unknown' and not r['stale']
                                      and not r['conflict'] for r in cached)
                        stage('SECTION46', month=month)
                        live.prepare(number, names[number], identity_only=covered)
                    with TemporaryDirectory(prefix='donation-month-',
                            dir=root/'full-staging'/account/str(year)) as temporary:
                        enrich_candidates(wb, entries, month_rows, additions, root, live, config,
                                          Path(temporary), digest(book['before']) if book['before'] else None,
                                          preserve_verified=True, source_state=state, review_counts=corrections)
                    rows.update(month_rows)
                    for donation_id in entries:
                        entries[donation_id]['documents'] = sorted(set(entries[donation_id]['documents']) | assignments[donation_id])
                    write_sources(wb, entries, rows, state, receipts_dir, account)
                    from src.report_layout import apply_full_report
                    link_counts = apply_full_report(wb, rows, entries, state, receipts_dir, account, root,
                        workbook_path=book['path'], scoped=user is not None, registry=live.registry, selected_ids=set(month_rows), finalize=False, config=config)
                    counts.update(link_counts)
                    counts.update(classify_receipts(wb, entries, month_rows, live.registry, live))
                    emit('EXCEL', 'complete', month=month, **link_counts)
                stage('EXCEL', month=0)
            else:
                with TemporaryDirectory(prefix='donation-sync-') as temporary:
                    enrich_candidates(wb, entries, rows, book['additions'], root, live, config, Path(temporary),
                                      digest(book['before']) if book['before'] else None, preserve_verified=manual, source_state=state if manual else None,
                                      review_counts=corrections if manual else None)
            for donation_id in entries:
                entries[donation_id]['documents'] = sorted(set(entries[donation_id]['documents']) | assignments[donation_id])
            relink = set()
            if full_report:
                previous = {key: entry.get('representative_document') for key, entry in entries.items()}
                counts['merged_copies'] += filter_verified_pdf_rows(
                    wb, entries, rows, state, Path(receipts_dir) / account, year=tax_year)
                relink = {key for key, entry in entries.items()
                          if entry.get('representative_document') != previous.get(key)}
                atomic_json(catalog_path, state)
            if full_report or manual:
                from src.sync_workbook import apply_group_copy_preference
                relink.update(apply_group_copy_preference(entries, rows, state, Path(receipts_dir) / account))
            write_sources(wb, entries, rows, state, receipts_dir, account)
            active_rows = rows
            if manual:
                from src.manual_exclusion import apply_workbook_exclusions, excluded_publications
                active_rows = apply_workbook_exclusions(rows, excluded_publications(state, tax_year))
            write_totals(wb, active_rows, book['indexed'], preserve_formulas=full_report or manual)
            if full_report or manual:
                from src.report_layout import apply_full_report
                # Finalize orphan ledger links and source choices from late merges.
                link_counts = apply_full_report(wb, rows, entries, state, receipts_dir, account, root,
                    workbook_path=book['path'],
                    selected_ids=set(rows) if manual else relink, finalize=True, config=config, scoped=user is not None, registry=live.registry)
                counts.update(link_counts)
            refresh_published_rows(entries, rows, state, config, tax_year)
            write_index(wb, entries)
            if full_report:
                wb['_Sync publications']['D1'] = 'Full report layout v1'
            if full_report or manual or summary_name(tax_year) in wb:
                stamp_published_locations(wb, entries, rows)
                write_summary(wb, tax_year, active_rows)
            if manual_review is not None:
                from src.manual_receipts import write_review
                write_review(wb, Path(receipts_dir) / account, manual_review)
            if signature(wb) != book['original']:
                stage('PUBLISH')
                replace_verified(book['path'], book['before'], wb,
                    backup_dir=root/'report-backups'/account/str(tax_year) if full_report or manual else None)
                counts['corrections_published'] += corrections['staged']
                counts['reports_updated'] += 1
                emit('PUBLISH', 'complete', reports_updated=counts['reports_updated'])
        live.counts['unique_organizations'] = len(dates)
        counts.update(live.counts)
        counts.setdefault('api_requests', 0)
        return dict(counts)
    finally:
        for book in workbooks.values():
            book['wb'].close()


def run_sync(*, provider, receipts_dir, reports_dir, accounts_dir, manifest_path, account, year, root=None, plan=False, allowed_years=None, full_report=False, reprocess=False, resume_pending=False, refresh_base=False, public_registry=None, user=None, identity_scope=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', account) or not 1900 <= year <= 9998:
        raise ValueError('Invalid account or year')
    if allowed_years is not None and (not allowed_years or year not in allowed_years
            or any(type(y) is not int or not 1900 <= y <= 9998 for y in allowed_years)):
        raise SyncError('invalid_year_authorization', 'Explicit year authorization must include the requested year')
    if refresh_base and (not full_report or not resume_pending or plan):
        raise ValueError('Refresh base requires full saved-source processing')
    if resume_pending and (not full_report or reprocess):
        raise ValueError('Full resume cannot reprocess Gmail')
    if provider is None and not (full_report and resume_pending):
        raise ValueError('Gmail provider required')
    if provider is not None and safe_path(provider.account_dir) != safe_path(Path(accounts_dir) / account):
        raise ValueError('Provider account does not match requested account')
    stage("CONFIG")
    root = registry_root(root)
    if not safe_path(public_registry or root / 'organizations.json').is_file():
        raise ValueError('Existing operational registry required')
    if full_report and root.is_relative_to(safe_path(reports_dir)):
        raise ValueError('Full runtime staging must be outside reports')
    with operational_lock(root):
        ingestion_error = None
        results = []
        # Legacy runner emits private paths and errors; sync emits counts/types only.
        with redirect_stdout(io.StringIO()):
            try:
                if not plan and not resume_pending:
                    results = run_gmail(provider, receipts_dir, manifest_path, False,
                                        resumable=True, pdf_only=True, reprocess=reprocess,
                                        recover_origins=full_report)
            except Exception as exc:
                ingestion_error = exc
            # Full must fail before any canonical publication once ingestion failed.
            # Successful sources remain on disk and incomplete messages stay retryable.
            # Legacy Sync/Delta behavior is deliberately unchanged.
            if full_report:
                if ingestion_error is not None:
                    raise ingestion_error
                if any(r.get('retry_required') for r in results):
                    raise RuntimeError('Attachment download failures; rerun required')
            counts = publish(receipts_dir, reports_dir, accounts_dir, account, year, root,
                plan=plan, allowed_years=allowed_years, full_report=full_report, resume_processing=resume_pending, refresh_base=refresh_base, public_registry=public_registry, user=user, identity_scope=identity_scope)
        if ingestion_error is not None:
            raise ingestion_error
        if any(r.get('retry_required') for r in results):
            raise RuntimeError('Attachment download failures; rerun required')
        counts['rejected_attachments'] = sum(len(r.get('skipped_attachments', [])) for r in results)
        if not plan and counts.get('pending_date'):
            raise SyncError('pending_donation_date', 'Receipts pending a valid donation date; original documents retained in catalog')
        return counts
