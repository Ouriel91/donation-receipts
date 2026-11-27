"""Private document catalog. This records extraction, never successful publication."""
from contextlib import closing, nullcontext
from copy import deepcopy
from dataclasses import asdict
from src.logger import stage, emit, metric, snapshot
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from src.attachment_saver import MONTH_NAMES
from src.user_scope import annual_report
from src.private_runtime import atomic_json, new_bytes, operational_lock, safe_path
from src.receipt_deduplication import consolidate_receipts
from src.receipt_metadata_extractor import IDENTIFIER_REVIEW_NOTES, ReceiptMetadata
from src.receipt_summary import _extract_row


class SyncError(ValueError):
    """Static, safe-to-log recovery code; never includes private provider text."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(account, documents):
    return 'd-' + digest((account + ':' + min(documents)).encode())


def donation_date(meta):
    try:
        date = datetime.strptime(meta.receipt_date, '%d/%m/%Y').date()
        return date if 1900 <= date.year and date <= (datetime.now(timezone.utc) + timedelta(days=1)).date() else None
    except ValueError:
        return None


IDENTIFIER_FIELDS = ("receipt_number", "tax_report_number", "donor_id")
# Bump when the extractor or reader changes identifier recognition.
IDENTIFIER_REFRESH_VERSION = 2
IDENTIFIER_REVIEW_SOURCE = "identifier_refresh_reviewed"
AMOUNT_REVIEW_SOURCE = "historical_amount_reviewed"
REVIEW_STATUS = 'לא ידוע / דורש בירור'
PUBLICATION_ID = re.compile(r'(?:d|legacy|retained)-[0-9a-f]{64}(?:-h[0-9a-f]{16})?')


def _refresh_identifiers(entry, meta, year, *, published=None, replaceable=()):
    changes = {}
    for field in IDENTIFIER_FIELDS:
        before = entry['metadata'].get(field, '')
        after = getattr(meta, field)
        donor_mislabel = (field == 'donor_id' and published is not None and bool(meta.receipt_number)
                          and published.get(field) == meta.receipt_number
                          and before == meta.receipt_number
                          and after != meta.receipt_number)
        if field == 'donor_id' and not donor_mislabel:
            continue
        if field == 'tax_report_number' and published is not None and published.get(field) == '' and not after:
            after = meta.tax_confirmation_candidate
        if ((field in entry.get('verified_identifier_updates', {}) and field not in replaceable)
                or (published is not None and field not in published)):
            continue
        if field in replaceable and not (
            after and published[field] == meta.tax_report_number
            and not any(note in meta.notes for note in IDENTIFIER_REVIEW_NOTES.values())
        ):
            continue
        baseline = before if published is None else published[field]
        mislabelled_receipt = (
            field == 'receipt_number' and bool(baseline) and baseline == meta.tax_report_number
            and not any(note in meta.notes for note in IDENTIFIER_REVIEW_NOTES.values())
        )
        if after != baseline and (after or mislabelled_receipt or donor_mislabel):
            changes[field] = {'before': before, 'value': after}
            if published is not None:
                changes[field]['published_before'] = baseline
            if field in replaceable:
                changes[field]['supersedes'] = replaceable[field]
    entry['identifier_refresh'] = {
        'version': IDENTIFIER_REFRESH_VERSION, 'year': year, 'proposals': changes,
    }
    return len(changes)


def _save_review_catalog(path, root, account, before, state):
    backup = safe_path(root / 'review-backups' / f'sync-{account}-{digest(before)}.json')
    if backup.exists():
        if backup.read_bytes() != before:
            raise SyncError('identifier_backup_mismatch', 'Catalog backup could not be verified')
    else:
        new_bytes(backup, before)
    if backup.read_bytes() != before or path.read_bytes() != before:
        raise SyncError('identifier_review_stale', 'Catalog or its backup changed during review')
    atomic_json(path, state)


def _applied_identifier_proof(entry, index_entry, sha, field, value):
    proof = entry.get('verified_identifier_updates', {}).get(field)
    if (field != 'receipt_number' or not value or not isinstance(proof, dict)
            or proof.get('value') != value or entry['metadata'].get(field) != value
            or proof.get('source') not in (IDENTIFIER_REVIEW_SOURCE, 'task_a_2026_reviewed')):
        return None
    fingerprint = digest(json.dumps(proof, sort_keys=True).encode())
    if index_entry.get('identifier_approvals', {}).get(f'{sha}:{field}') == fingerprint:
        return fingerprint
    return None


def refresh_selected_identifiers(receipts_dir, reports_dir, accounts_dir, account, root, year, selected, *, preview=False):
    """Read verified PDFs and compare to their unique indexed publication."""
    from src.account_config import load_account_config
    from src.report_layout import _report_field_columns
    from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, donation_sheets, headers, load

    if (not re.fullmatch(r'[A-Za-z0-9_-]+', account) or not 1900 <= year <= 9998
            or not selected or len(set(selected)) != len(selected)
            or any(not re.fullmatch(r'[0-9a-f]{64}', sha) for sha in selected)):
        raise SyncError('identifier_selection_invalid', 'Select distinct source SHA-256 values and a report year')
    catalog = safe_path(root / f'sync-{account}.json')
    workbook = annual_report(reports_dir, account, year)
    with (nullcontext() if preview else operational_lock(root)):
        before, workbook_digest = catalog.read_bytes(), digest(workbook.read_bytes())
        state = json.loads(before)
        if state.get('version') != 1 or not isinstance(state.get('documents'), dict):
            raise SyncError('identifier_catalog_invalid', 'Invalid sync catalog')
        config = load_account_config(accounts_dir, account)
        output = []
        receipts_base = safe_path(Path(receipts_dir) / account)
        with closing(load(workbook)) as wb:
            if INDEX not in wb:
                raise SyncError('identifier_review_layout', 'Published index is missing')
            index = {}
            for key, raw in wb[INDEX].iter_rows(min_row=2, max_col=2, values_only=True):
                if not key and not raw:
                    continue
                item = json.loads(raw) if isinstance(raw, str) else None
                if (not key or key in index or not isinstance(item, dict)
                        or not isinstance(item.get('documents'), list)):
                    raise SyncError('identifier_review_layout', 'Published index is invalid')
                index[key] = item
            active = {}
            for ws in donation_sheets(wb):
                columns = headers(ws)
                if ID_HEADER not in columns:
                    continue
                for row in range(2, ws.max_row + 1):
                    key = ws.cell(row, columns[ID_HEADER]).value
                    if key in index and not (ARCHIVE in columns and ws.cell(row, columns[ARCHIVE]).value):
                        if key in active:
                            raise SyncError('identifier_owner_ambiguous', 'Duplicate active publication')
                        active[key] = (ws, row)
            for sha in selected:
                entry = state['documents'].get(sha)
                paths = document_paths(receipts_dir, account, state, (sha,))
                owners = [key for key, item in index.items() if sha in item['documents']]
                if not entry or not paths or len(owners) != 1 or owners[0] not in active:
                    raise SyncError('identifier_owner_ambiguous', 'Select a verified source with one active publication')
                cached = effective_metadata(entry)
                date = donation_date(cached)
                if not date or date.year != year:
                    raise SyncError('identifier_review_year', 'Donation is outside the requested report year')
                key = owners[0]
                ws, row = active[key]
                published = {}
                visible_values = {}
                protected = []
                replaceable = {}
                columns = _report_field_columns(ws)
                managed = index[key].get('managed', {})
                review_fields = IDENTIFIER_FIELDS if account == 'manual' else IDENTIFIER_FIELDS[:2]
                for field in review_fields:
                    if field == 'donor_id' and field not in columns:
                        continue
                    if field not in columns:
                        raise SyncError('identifier_review_layout', 'Published identifier column is missing')
                    cell = ws.cell(row, columns[field])
                    value = '' if cell.value is None else str(cell.value)
                    visible_values[field] = value
                    label = ws.cell(1, cell.column).value
                    prior = _applied_identifier_proof(entry, index[key], sha, field, value)
                    if (cell.comment or cell.data_type in ('b', 'e', 'f')
                            or (field in entry.get('verified_identifier_updates', {}) and not prior)
                            or (label in managed and str(managed[label] or '') != value)):
                        protected.append(field)
                    else:
                        published[field] = value
                        if prior and label in managed:
                            replaceable[field] = prior
                        elif prior:
                            del published[field]
                            protected.append(field)
                source = paths[0]
                if not source.is_relative_to(receipts_base):
                    raise SyncError('identifier_source_outside_project', 'Verified source is outside its account')
                storage_year = source.relative_to(receipts_base).parts[0]
                if not re.fullmatch(r'[0-9]{4}', storage_year):
                    raise SyncError('identifier_source_layout', 'Source is outside an annual receipt directory')
                if account == 'manual':
                    extracted = _extract_row(source, account, config, int(storage_year),
                                             source_name=entry['metadata']['file_name'])
                else:
                    extracted = _extract_row(source, account, config, int(storage_year))
                extracted_date = donation_date(effective_metadata({'metadata': asdict(extracted)}))
                agrees = (extracted_date in (None, date)
                          and (not cached.registration_number
                               or extracted.registration_number in ('', cached.registration_number)))
                readable = 'לא ניתן לקרוא את הקובץ' not in extracted.notes
                if digest(source.read_bytes()) != sha:
                    raise SyncError('identifier_source_changed', 'Source changed during extraction')
                trial = deepcopy(entry)
                can_propose = agrees and readable and len(set(index[key]['documents'])) == 1
                if can_propose:
                    _refresh_identifiers(trial, extracted, year, published=published, replaceable=replaceable)
                else:
                    trial['identifier_refresh'] = {'version': IDENTIFIER_REFRESH_VERSION,
                                                   'year': year, 'proposals': {}}
                protected.extend(field for field in replaceable
                                 if field not in trial['identifier_refresh']['proposals'])
                output.append({'document_sha256': sha, 'publication_id': key, 'month': date.month,
                               'published': visible_values, 'extracted': {field: getattr(extracted, field)
                                                                         for field in (*IDENTIFIER_FIELDS, 'amount')},
                               'protected_fields': protected,
                               'multiple_sources': len(set(index[key]['documents'])) > 1,
                               'review_required': not can_propose or bool(protected) or any(
                                   note in extracted.notes for note in IDENTIFIER_REVIEW_NOTES.values()),
                               'proposals': trial['identifier_refresh']['proposals']})
                if not preview:
                    entry['identifier_refresh'] = trial['identifier_refresh']
        if digest(workbook.read_bytes()) != workbook_digest or catalog.read_bytes() != before:
            raise SyncError('identifier_review_stale', 'Workbook or catalog changed during inspection')
        if not preview and state != json.loads(before):
            _save_review_catalog(catalog, root, account, before, state)
        return output


def _review_target(reports_dir, account, year, publication, field, before, *, sha=None, allow_unmanaged=False):
    from src.report_layout import _report_field_columns
    from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, donation_sheets, headers, load, metadata

    path = annual_report(reports_dir, account, year)
    with closing(load(path)) as wb:
        if INDEX not in wb:
            raise SyncError('historical_review_owner', 'An indexed canonical publication is required')
        entries = [json.loads(record[1]) for record in wb[INDEX].iter_rows(
            min_row=2, max_col=2, values_only=True) if record[0] == publication]
        active = [(ws, cell.row) for ws in donation_sheets(wb)
                  if ID_HEADER in (columns := headers(ws))
                  for (cell,) in ws.iter_rows(min_row=2, min_col=columns[ID_HEADER], max_col=columns[ID_HEADER])
                  if cell.value == publication
                  and not (ARCHIVE in columns and ws.cell(cell.row, columns[ARCHIVE]).value)]
        if len(entries) != 1 or len(active) != 1 or (sha and sha not in entries[0]['documents']):
            raise SyncError('historical_review_owner', 'The published row does not uniquely own the reviewed source')
        ws, row = active[0]
        column = _report_field_columns(ws).get(field)
        if not column:
            raise SyncError('historical_review_layout', 'The canonical field is missing')
        cell = ws.cell(row, column)
        current = '' if cell.value is None else str(cell.value)
        if current != before:
            raise SyncError('historical_review_baseline', 'The canonical field differs from the reviewed baseline')
        managed = entries[0].get('managed', {})
        label = ws.cell(1, cell.column).value
        if sha and (cell.comment or cell.data_type in ('b', 'e', 'f')
                    or (label in managed and managed[label] != cell.value)
                    or (field in IDENTIFIER_FIELDS and label not in managed and not allow_unmanaged)):
            raise SyncError('amount_manual_conflict' if field == 'amount' else 'identifier_manual_conflict',
                            'Protected cell; original preserved')
        return metadata(ws, row)


def _validate_identifier_replacement(reports_dir, account, year, sha, field, before, entry, fingerprint):
    from src.sync_workbook import INDEX, load

    workbook = annual_report(reports_dir, account, year)
    with closing(load(workbook)) as wb:
        owners = []
        if INDEX in wb:
            for publication, raw in wb[INDEX].iter_rows(min_row=2, max_col=2, values_only=True):
                if publication and raw:
                    indexed = json.loads(raw)
                    if sha in indexed.get('documents', []):
                        owners.append((publication, indexed))
        if (len(owners) != 1 or set(owners[0][1]['documents']) != {sha}
                or _applied_identifier_proof(entry, owners[0][1], sha, field, before) != fingerprint):
            raise SyncError('identifier_review_stale', 'Applied publication changed; inspect again')
    _review_target(reports_dir, account, year, owners[0][0], field, before, sha=sha)


def review_identifiers(receipts_dir, account, root, year, decisions=(), *,
                       amounts=(), eligibility=(), revoke_amounts=(), revoke_eligibility=(),
                       reports_dir=Path("reports"), oc_pairs=(), revoke_oc=(), public_registry=None, clear_manual_tax=()):
    """List local proposals or record explicitly reviewed, hash-bound decisions."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', account) or not 1900 <= year <= 9998:
        raise ValueError('Invalid account or year')
    path = safe_path(root / f'sync-{account}.json')
    with operational_lock(root):
        before = path.read_bytes()
        state = json.loads(before)
        if state.get('version') != 1 or not isinstance(state.get('documents'), dict):
            raise ValueError('Invalid sync catalog')
        proposals = {}
        for sha, entry in state['documents'].items():
            refresh = entry.get('identifier_refresh', {})
            if refresh.get('version') == IDENTIFIER_REFRESH_VERSION and refresh.get('year') == year:
                for field, change in refresh.get('proposals', {}).items():
                    proposals[sha, field] = change

        if not (decisions or amounts or eligibility or revoke_amounts or revoke_eligibility or oc_pairs or revoke_oc or clear_manual_tax):
            return [{'document_sha256': sha, 'field': field, **change}
                    for (sha, field), change in sorted(proposals.items())]

        for sha, field, value, published_before in decisions:
            proposal = proposals.pop((sha, field), None)
            entry = state['documents'].get(sha)
            if (field not in IDENTIFIER_FIELDS or not proposal
                    or (field == 'donor_id' and account != 'manual')
                    or (not value and (field not in {'receipt_number', 'donor_id'} or not published_before))
                    or value != proposal['value'] or entry['metadata'].get(field, '') != proposal['before']
                    or ('published_before' in proposal and published_before != proposal['published_before'])
                    or not document_paths(receipts_dir, account, state, (sha,))):
                raise SyncError('identifier_review_stale', 'Proposal or source changed; review the current local data')
            date = donation_date(effective_metadata(entry))
            if not date or date.year != year:
                raise SyncError('identifier_review_year', 'Donation is outside the requested report year')
            previous = entry.get('verified_identifier_updates', {}).get(field)
            if previous is not None:
                # The selected refresh proved this exact applied approval and
                # only permits changing a receipt token reclassified as tax.
                if (field != 'receipt_number' or not value
                        or proposal.get('supersedes') != digest(json.dumps(previous, sort_keys=True).encode())):
                    raise SyncError('identifier_review_stale', 'Previous approval changed; inspect again')
                _validate_identifier_replacement(reports_dir, account, year, sha, field,
                                                 published_before, entry, proposal['supersedes'])
                entry.setdefault('identifier_approval_history', {}).setdefault(field, []).append(previous)
            entry['metadata'][field] = value
            entry.setdefault('verified_identifier_updates', {})[field] = {
                'source': IDENTIFIER_REVIEW_SOURCE, 'year': year,
                'value': value, 'before': published_before,
            }
            del entry['identifier_refresh']['proposals'][field]

        for sha, publication, value, reason in clear_manual_tax:
            entry = state['documents'].get(sha)
            date = donation_date(effective_metadata(entry)) if entry else None
            if (account != 'manual' or not value or not reason.strip() or not date or date.year != year
                    or not PUBLICATION_ID.fullmatch(publication)
                    or not document_paths(receipts_dir, account, state, (sha,))
                    or 'tax_report_number' in entry.get('verified_identifier_updates', {})):
                raise SyncError('manual_tax_clear_invalid', 'A verified unprotected MANUAL tax value is required')
            _review_target(reports_dir, account, year, publication, 'tax_report_number', value, sha=sha,
                           allow_unmanaged=entry['metadata'].get('tax_report_number') == value)
            entry['metadata']['tax_report_number'] = ''
            entry.setdefault('verified_identifier_updates', {})['tax_report_number'] = {
                'source': IDENTIFIER_REVIEW_SOURCE, 'year': year, 'value': '', 'before': value,
                'operator_verified_absent': reason.strip(), 'reviewed_at': datetime.now(timezone.utc).isoformat(),
            }
            entry.get('identifier_refresh', {}).get('proposals', {}).pop('tax_report_number', None)

        for sha in revoke_amounts:
            updates = state['documents'].get(sha, {}).get('verified_identifier_updates', {})
            proof = updates.get('amount', {})
            if proof.get('source') != AMOUNT_REVIEW_SOURCE or proof.get('year') != year:
                raise SyncError('amount_review_missing', 'No reviewed amount to revoke')
            del updates['amount']

        for sha, publication, value, published_before in amounts:
            from src.receipt_deduplication import receipt_amount
            from src.sync_workbook import receipt_source_evidence
            entry = state['documents'].get(sha)
            paths = document_paths(receipts_dir, account, state, (sha,))
            if entry is None or not paths or not PUBLICATION_ID.fullmatch(publication) or not isinstance(published_before, str):
                raise SyncError('amount_review_source', 'Amount review requires a verified source')
            meta = effective_metadata(entry)
            donated = donation_date(meta)
            expected = receipt_amount(value)
            evidence = receipt_source_evidence(paths[0], meta)
            if (not donated or donated.year != year or expected is None
                    or evidence['sha256'] != sha or evidence['receipt_numbers'] != [meta.receipt_number]
                    or len(evidence['amounts']) != 1 or receipt_amount(evidence['amounts'][0]) != expected):
                raise SyncError('amount_review_evidence', 'PDF must prove one receipt and the approved amount')
            _review_target(reports_dir, account, year, publication, 'amount', published_before, sha=sha)
            updates = entry.setdefault('verified_identifier_updates', {})
            if 'amount' in updates:
                raise SyncError('amount_review_exists', 'Amount approval already recorded')
            updates['amount'] = {'source': AMOUNT_REVIEW_SOURCE, 'year': year, 'publication': publication,
                                 'value': value, 'before': published_before}

        approvals = state.get('historical_section46', {})
        for publication in revoke_eligibility:
            proof = approvals.get(publication)
            if not isinstance(proof, dict) or not str(proof.get('date', '')).startswith(f'{year:04d}-'):
                raise SyncError('section46_review_missing', 'No Section 46 approval to revoke for this year')
            del approvals[publication]

        for publication, before_status, number, date_text in eligibility:
            from datetime import date
            from src.organization_cache import OrganizationCache
            from src.organization_enrichment import dated_section46
            from src.section46_live import LiveSection46

            donated = date.fromisoformat(date_text)
            if (donated.isoformat() != date_text or donated.year != year
                    or before_status != REVIEW_STATUS or not re.fullmatch(r'[0-9]{9}', number)
                    or not PUBLICATION_ID.fullmatch(publication)):
                raise SyncError('section46_review_input', 'Use a published identity, unknown status and exact donation date')
            meta = _review_target(reports_dir, account, year, publication, 'section46_status', before_status)
            if meta.registration_number.strip() != number or donation_date(meta) != donated:
                raise SyncError('section46_review_identity', 'The canonical organization or donation date differs')
            cache = OrganizationCache(public_registry or root / 'organizations.json')
            offline = LiveSection46(root / 'section46-live.json', cache)
            if (not cache.official_identity(number)
                    or dated_section46(cache, offline, number, donated)[1] != 'approved'):
                raise SyncError('section46_review_evidence', 'Fresh official approval covering this organization and date is required')
            approvals = state.setdefault('historical_section46', {})
            if publication in approvals:
                raise SyncError('section46_review_exists', 'Section 46 approval already recorded')
            approvals[publication] = {'approval_id': uuid4().hex, 'before': before_status,
                                      'number': number, 'date': date_text}

        if revoke_oc:
            approvals = state.get('reviewed_oc_pairs', {})
            for key in revoke_oc:
                if key not in approvals or approvals[key]['date'][:4] != str(year):
                    raise SyncError('oc_review_missing', 'No reviewed O/C pair for this original publication')
                del approvals[key]

        if oc_pairs:
            from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, donation_sheets, headers, load, reviewed_oc_pair

            workbook = annual_report(reports_dir, account, year)
            baseline = digest(workbook.read_bytes())
            with closing(load(workbook)) as wb:
                if INDEX not in wb:
                    raise SyncError('oc_review_owner', 'An indexed annual publication is required')
                entries = {key: json.loads(raw) for key, raw in wb[INDEX].iter_rows(
                    min_row=2, max_col=2, values_only=True) if key and raw}
                rows = {}
                for ws in donation_sheets(wb):
                    columns = headers(ws)
                    if ID_HEADER not in columns:
                        continue
                    for row in range(2, ws.max_row + 1):
                        key = ws.cell(row, columns[ID_HEADER]).value
                        if key not in entries or (ARCHIVE in columns and ws.cell(row, columns[ARCHIVE]).value):
                            continue
                        if key in rows:
                            raise SyncError('oc_review_owner', 'Duplicate active publication ID')
                        rows[key] = ws, row
                approvals = state.setdefault('reviewed_oc_pairs', {})
                for original, copied in oc_pairs:
                    proof = reviewed_oc_pair(rows, entries, state, safe_path(Path(receipts_dir) / account),
                                             original, copied, year=year)
                    if any({original, copied} & {item['original'], item['copy']}
                           for item in approvals.values() if item != proof):
                        raise SyncError('oc_review_overlap', 'Publication already belongs to a reviewed O/C pair')
                    approvals[original] = proof
            if digest(workbook.read_bytes()) != baseline:
                raise SyncError('oc_review_stale', 'Annual workbook changed during O/C review')

        _save_review_catalog(path, root, account, before, state)
        result = {'approved': len(decisions) + len(amounts) + len(eligibility) + len(oc_pairs) + len(clear_manual_tax)}
        if revoke_amounts:
            result['revoked_amounts'] = len(revoke_amounts)
        if revoke_eligibility:
            result['revoked_section46'] = len(revoke_eligibility)
        if revoke_oc:
            result['revoked_oc_pairs'] = len(revoke_oc)
        return result


def stage_table_amount_candidate(entry, fresh, source, sha):
    """Reuse the Full parser-revision gate for all authorized source catalogs."""
    if 'לא ניתן לקרוא את הקובץ' in fresh.notes or digest(source.read_bytes()) != sha:
        return False
    old = effective_metadata(entry)
    from src.receipt_deduplication import receipt_amount
    old_amount, new_amount = receipt_amount(old.amount), receipt_amount(fresh.amount)
    core = ('registration_number', 'receipt_number', 'receipt_date')
    if (fresh.payment_total_verified and not fresh.full_source_serial
            and new_amount is not None and old_amount != new_amount
            and all(getattr(old, f) and getattr(old, f) == getattr(fresh, f) for f in core)
            and not any(n in fresh.notes for n in IDENTIFIER_REVIEW_NOTES.values())
            and entry.get('donation_amount_override') is None
            and 'amount' not in entry.get('verified_identifier_updates', {})):
        if entry.get('logical_id') or entry.get('historical_review_ids'):
            entry['table_amount_candidate'] = {'before': old.amount, 'after': fresh.amount}
        else:
            entry['metadata']['amount'] = fresh.amount
            entry['metadata']['payment_total_verified'] = True
    entry['table_amount_revision'] = 1
    return 'table_amount_candidate' in entry


def stage_saved_table_amounts(state, receipts_dir, account, config, years):
    """One local retry per parser revision; no download, decision or publication."""
    for sha, item in state['documents'].items():
        if ('metadata' not in item or item.get('table_amount_revision') == 1
                or item.get('stage') in {'review', 'review_existing', 'review_not_donation', 'review_deferred_year'}
                or item.get('review_decision', {}).get('action') == 'distinct'):
            continue
        old = effective_metadata(item)
        donated = donation_date(old)
        if not donated or donated.year not in years:
            continue
        paths = document_paths(receipts_dir, account, state, [sha])
        if paths:
            fresh = _extract_row(paths[0], account, config, donated.year)
            stage_table_amount_candidate(item, fresh, paths[0], sha)



def stage_saved_tax_layout(state, receipts_dir, account, config, years, *, fresh_cache=None):
    """Re-read only missing tax fields once per layout revision, never approvals."""
    from src.receipt_deduplication import receipt_amount
    for sha, item in state['documents'].items():
        if ('metadata' not in item or item.get('tax_layout_revision') == 1
                or item.get('stage') in {'review', 'review_existing', 'review_not_donation', 'review_deferred_year'}
                or item.get('review_decision', {}).get('action') == 'distinct'
                or 'tax_report_number' in item.get('verified_identifier_updates', {})):
            continue
        old = effective_metadata(item)
        date = donation_date(old)
        if old.tax_report_number or not date or date.year not in years:
            continue
        paths = document_paths(receipts_dir, account, state, [sha])
        if not paths:
            continue
        fresh = (fresh_cache or {}).get(sha)
        if fresh is None:
            fresh = _extract_row(paths[0], account, config, date.year)
        if 'לא ניתן לקרוא את הקובץ' in fresh.notes or digest(paths[0].read_bytes()) != sha:
            continue
        if (fresh.tax_layout_verified and fresh.tax_report_number
                and all(getattr(old, f) and getattr(old, f) == getattr(fresh, f)
                        for f in ('registration_number', 'receipt_number', 'receipt_date'))
                and receipt_amount(old.amount) is not None and receipt_amount(old.amount) == receipt_amount(fresh.amount)
                and not any(n in fresh.notes for n in IDENTIFIER_REVIEW_NOTES.values())):
            item['tax_layout_candidate'] = fresh.tax_report_number
            # Only stage evidence here. A published blank is filled only at the
            # protected workbook publication gate; pending new docs may use it.
            if not item.get('logical_id') and not item.get('historical_review_ids'):
                apply_tax_layout_metadata(item, fresh.tax_report_number)
        item['tax_layout_revision'] = 1


def apply_tax_layout_metadata(item, value):
    data = item['metadata']
    data['tax_report_number'] = value
    data['tax_layout_verified'] = True
    data['tax_confirmation_candidate'] = ''
    data['notes'] = '; '.join(n for n in data.get('notes', '').split('; ')
                              if n and n != IDENTIFIER_REVIEW_NOTES['tax_report_number'])

def catalog_documents(receipts_dir, account, root, config, *, refresh_year=None):
    stage('EXTRACTION')
    scanned = extracted = proposals_created = dates_recovered = 0
    reused = set()
    refreshed = set()
    date_attempted = set()
    amount_attempted = set()
    fresh_cache = {}

    def read_fresh(source, key, year):
        nonlocal extracted
        if key not in fresh_cache:
            fresh_cache[key] = _extract_row(source, account, config, year)
            extracted += 1
        return fresh_cache[key]
    path = safe_path(root / f'sync-{account}.json')
    before = path.read_bytes() if path.exists() else None
    state = json.loads(before) if before is not None else {'version': 1, 'documents': {}}
    if state.get('version') != 1 or not isinstance(state.get('documents'), dict):
        raise ValueError('Invalid sync catalog; restore locally before retry')
    documents = state['documents']
    from src.manifest import load_attachment_origins
    origins = load_attachment_origins(root / 'processed_messages.json', account)
    cached_keys=set(documents)
    base = safe_path(Path(receipts_dir) / account)
    for year in sorted(base.iterdir()) if base.exists() else []:
        if not year.name.isdigit() or not 1900 <= int(year.name) <= 9998:
            continue
        for month in MONTH_NAMES.values():
            folder = year / month
            for source in sorted(folder.iterdir()) if folder.is_dir() else []:
                if source.suffix.lower() != '.pdf':
                    continue
                source = safe_path(source)
                scanned += 1
                key = digest(source.read_bytes())
                relative = source.relative_to(base).as_posix()
                if key in cached_keys:
                    reused.add(key)
                if key not in documents:
                    meta = read_fresh(source, key, int(year.name))
                    if digest(source.read_bytes()) != key:
                        raise SyncError('catalog_source_changed', 'A receipt source changed during extraction; retry')
                    documents[key] = {'metadata': asdict(meta), 'paths': [], 'logical_id': None}
                    if 'לא ניתן לקרוא את הקובץ' not in meta.notes:
                        documents[key].update(table_amount_revision=1, tax_layout_revision=1)
                        date = donation_date(meta)
                        if date:
                            _refresh_identifiers(documents[key], meta, date.year)
                entry = documents[key]
                if key in origins:
                    entry['attachment_origins'] = origins[key]
                if (refresh_year is not None and key in cached_keys
                        and key not in refreshed and key not in date_attempted):
                    date = donation_date(effective_metadata(entry))
                    revision = entry.get('identifier_refresh', {})
                    if (date is None and not entry.get('logical_id')
                            and not entry.get('historical_review_ids')
                            and entry.get('donation_date_override') is None
                            and not entry['metadata'].get('receipt_date')):
                        # Only retry unpublished, genuinely missing dates. Do not
                        # change identifiers, amounts, approvals or existing dates.
                        fresh = read_fresh(source, key, int(year.name))
                        date_attempted.add(key)
                        recovered = donation_date(fresh)
                        if (recovered and recovered.year == refresh_year
                                and 'תאריך מתוך שם הקובץ' not in fresh.notes
                                and digest(source.read_bytes()) == key):
                            entry['metadata']['receipt_date'] = fresh.receipt_date
                            notes = []
                            for note in entry['metadata'].get('notes', '').split('; '):
                                if note.startswith('חסר: '):
                                    remaining = [part for part in note[5:].split(', ') if part != 'תאריך']
                                    if remaining:
                                        notes.append('חסר: ' + ', '.join(remaining))
                                elif note:
                                    notes.append(note)
                            entry['metadata']['notes'] = '; '.join(notes)
                            dates_recovered += 1
                        # Retry only once per content hash and leave other fields intact.
                    elif (date and date.year == refresh_year
                            and revision.get('version') != IDENTIFIER_REFRESH_VERSION):
                        meta = read_fresh(source, key, int(year.name))
                        refreshed.add(key)
                        if 'לא ניתן לקרוא את הקובץ' not in meta.notes:
                            proposals_created += _refresh_identifiers(entry, meta, refresh_year)
                # Normal Full revalidates old extraction once per parser revision.
                # Keep the old value until a managed workbook publication can be
                # safely reconciled. No human review is fabricated.
                if (refresh_year is not None and key in cached_keys
                        and key not in amount_attempted
                        and entry.get('table_amount_revision') != 1):
                    old = effective_metadata(entry)
                    old_date = donation_date(old)
                    if old_date and old_date.year == refresh_year:
                        amount_attempted.add(key)
                        fresh = read_fresh(source, key, int(year.name))
                        stage_table_amount_candidate(entry, fresh, source, key)
                if relative not in documents[key]['paths']:
                    documents[key]['paths'].append(relative)
                if scanned % 50 == 0:
                    emit('EXTRACTION', 'batch', source_pdfs_scanned=scanned, documents_extracted=extracted)
    if refresh_year is not None:
        stage_saved_tax_layout(state, receipts_dir, account, config, {refresh_year}, fresh_cache=fresh_cache)
    state['extraction_counts']={'source_pdfs_scanned':scanned,'documents_extracted':extracted,
                                'dates_recovered':dates_recovered,
                                'documents_reused':len(reused),'catalog_documents':len(documents),
                                'identifier_documents_refreshed':len(refreshed),
                                'correction_proposals_created':proposals_created}
    for name,value in state['extraction_counts'].items():metric(name,value,absolute=True)
    if refresh_year is not None:
        metric('identifier_documents_refreshed', len(refreshed), absolute=True)
    # Atomic extraction checkpoint BEFORE any workbook/API attempt.
    if dates_recovered:
        _save_review_catalog(path, root, account, before, state)
        emit('EXTRACTION', 'batch', dates_recovered=dates_recovered)
    else:
        atomic_json(path, state)
    emit('EXTRACTION', 'complete', source_pdfs_scanned=scanned, documents_extracted=extracted, catalog_documents=len(documents))
    return path, state


def effective_metadata(entry):
    meta = ReceiptMetadata(**entry['metadata'])
    if 'תאריך מתוך שם הקובץ' in meta.notes:
        # A legacy filename may encode EMAIL arrival; never route a new tax row
        # from that fallback as if it were a verified donation date.
        meta.receipt_date = ''
    if entry.get('donation_amount_override') is not None:
        from src.receipt_deduplication import receipt_amount
        value = entry['donation_amount_override']
        if not isinstance(value, str) or receipt_amount(value) is None:
            raise ValueError('Invalid human amount override')
        meta.amount = value
        meta.notes += '; סכום תרומה הושלם במפורש בבדיקה ידנית'
    override = entry.get('donation_date_override')
    if override is not None:
        if not isinstance(override, str):
            raise ValueError('Invalid local donation date override')
        meta.receipt_date = override
        if donation_date(meta) is None:
            raise ValueError('Invalid local donation date override')
        meta.notes += '; תאריך תרומה הושלם ידנית במטמון הסנכרון'
    return meta


def groups(state, legacy):
    records = [(Path(key), effective_metadata(value))
               for key, value in sorted(state['documents'].items())]
    records.extend((Path(key), meta) for key, meta in sorted(legacy.items()))
    return consolidate_receipts(records)


def document_paths(receipts_dir, account, state, keys):
    paths = []
    for key in sorted(keys):
        for relative in state['documents'].get(key, {}).get('paths', []):
            base = safe_path(Path(receipts_dir) / account)
            source = safe_path(base / relative)
            if not source.is_relative_to(base):
                raise ValueError('Catalog source is outside the account')
            if source.is_file() and digest(source.read_bytes()) == key:
                paths.append(source)
    return tuple(paths)
