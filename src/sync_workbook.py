"""Workbook reconciliation and transactional publication for unified sync."""
from collections import Counter, defaultdict
from src.logger import stage as report_stage, emit, metric, snapshot
from copy import copy, deepcopy
from decimal import Decimal, InvalidOperation
from datetime import datetime
from io import BytesIO
from openpyxl.styles import Font
import json
from math import isfinite
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
import warnings
from zipfile import BadZipFile, ZipFile

import openpyxl
from openpyxl.utils import quote_sheetname

from src.attachment_saver import MONTH_NAMES
from src.organization_enrichment import (CONFLICT_DETAILS, HEADERS, LIVE_HEADERS, _donation_date,
                                         dated_section46, enrich_workbook)
from src.private_runtime import atomic_bytes, safe_path
from src.receipt_deduplication import Donation, consolidate_receipts
from src.receipt_metadata_extractor import IDENTIFIER_REVIEW_NOTES, ReceiptMetadata
from src.receipt_summary import (COLUMN_HEADERS_HE, COLUMNS, METRIC_LABELS, SOURCE_SHEET,
                                 _MONTH_SHEET_NAMES, _write_month_sheet)
from src.sync_state import REVIEW_STATUS, SyncError, digest, donation_date, identity, document_paths, effective_metadata

ID_HEADER = 'Sync donation ID'
INDEX = '_Sync publications'
SOURCES = 'מקורות סנכרון'
OFFICIAL = 'שם רשמי (מרשם)'
ORIGINAL_NAME = 'שם מקורי (קבלה)'
SUMMARY = 'סיכום'
ARCHIVE = 'Historical duplicate archive v1'
SORT_ROW = 'Published row position v1'
REVIEW_NOTE = 'זהות היסטורית משותפת — נדרשת בדיקה ידנית; הרשומות נשמרו בנפרד'


def headers(ws):
    cells = [c for c in ws[1] if not (c.comment and c.comment.text.startswith('donation-receipts:primary:'))]
    values = [c.value for c in cells]
    if len([v for v in values if v is not None]) != len(set(v for v in values if v is not None)):
        raise ValueError('Ambiguous workbook headers')
    return {cell.value: cell.column for cell in cells if cell.value is not None}


def donation_sheets(wb):
    for ws in wb:
        values = [c.value for c in ws[1]]
        if all(COLUMN_HEADERS_HE[k] in values for k in ('registration_number', 'receipt_date', 'amount')):
            yield ws


def metadata(ws, row):
    from src.report_layout import _report_field_columns
    columns = _report_field_columns(ws)
    values = {}
    for field in COLUMNS:
        cell = ws.cell(row, columns[field]) if field in columns else None
        value = cell.value if cell else None
        if field == 'receipt_date' and cell:
            parsed = _donation_date(cell)
            value = parsed.strftime('%d/%m/%Y') if parsed else ''
        values[field] = str(value) if value is not None else ''
    return ReceiptMetadata(**values)


def load(path):
    # Fail closed for Excel features openpyxl cannot safely preserve.
    with ZipFile(path) as package:
        unsupported = ('customXml/', 'xl/slicers/', 'xl/slicerCaches/', 'xl/activeX/',
                       'xl/ctrlProps/', 'xl/embeddings/', 'xl/threadedComments/',
                       'xl/richData/', 'xl/vbaProject', 'xl/model/',
                       'xl/connections', 'xl/queryTables/')
        if any(name.startswith(unsupported) for name in package.namelist()):
            raise SyncError('unsupported_workbook', 'Unsupported Excel features; original workbook preserved')
        from src.excel_compat import capture
        person_metadata = capture(package)
    # Unsupported extensions must not be silently stripped by openpyxl.
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        wb = openpyxl.load_workbook(path, keep_links=True)
        wb._sync_person_metadata = person_metadata
        return wb


def _external_document(cell, workbook_path, receipts_base, documents, *, scoped=False):
    link = cell.hyperlink
    target = (link.target or link.location) if link else None
    if not target or target.startswith('#'):
        return None
    parsed = urlparse(target)
    if parsed.scheme not in ('', 'file') and not (len(parsed.scheme) == 1 and os.name == 'nt'):
        return None
    value = url2pathname(parsed.path) if parsed.scheme == 'file' else unquote(target)
    if parsed.scheme == 'file' and parsed.netloc:
        value = '//' + parsed.netloc + value
    from src.workbook_maintenance import resolve_encoding_target
    from src.annual_baseline import stored_source
    state = {'documents': documents}
    active_base = receipts_base.resolve()

    def active(key):
        return key in documents and bool(document_paths(active_base.parent, active_base.name, state, [key]))

    # A live old source proves content identity. Never remap a conflicting live
    # file by its filename, even when an active relative path looks similar.
    source, reason = resolve_encoding_target(target, workbook_path.parent, display_name=getattr(cell,'value',None),
                                             permitted_root=active_base if scoped else None)
    if source is not None:
        key = digest(source.read_bytes())
        return key if active(key) else None
    if reason not in ('pdf_not_found_in_same_directory',):
        return None
    mapped = Path(stored_source(value, active_base.parent.parent))
    if mapped.is_relative_to(active_base):
        source, _ = resolve_encoding_target(str(mapped), workbook_path.parent)
        if source is not None:
            key = digest(source.read_bytes())
            return key if active(key) else None
    # Old project paths may be gone. Reuse the catalog's exact account/year/month
    # suffix and the existing reversible basename resolver, never a fuzzy name
    # or donation-identity match. Multiple distinct content hashes stay unresolved.
    parts = value.replace('\\', '/').split('/')
    matches = set()
    for index in range(len(parts)-4):
        if parts[index:index+2] != ['receipts', active_base.name]:
            continue
        suffix = parts[index+2:]
        if len(suffix) != 3 or not suffix[0].isdigit():
            continue
        folder = active_base.joinpath(*suffix[:2])
        source, reason = resolve_encoding_target(str(folder/suffix[2]), workbook_path.parent)
        if reason == 'ambiguous_existing_names':
            return None
        # Catalog aliases retain the original document hash even after a safe
        # local rename. Reuse that recorded association only while another
        # active alias still proves the same bytes.
        relative = '/'.join(suffix)
        for key, item in documents.items():
            if relative in item.get('paths', []) and active(key):
                matches.add(key)
        if source is not None:
            key = digest(source.read_bytes())
            if active(key):
                matches.add(key)
    return next(iter(matches)) if len(matches) == 1 else None


ARCHIVE_AUDIT = '_Historical archive audit'


def _row_snapshot(ws, row):
    values = []
    for name, col in headers(ws).items():
        if name in (ID_HEADER, ARCHIVE, SORT_ROW):
            continue
        cell = ws.cell(row, col)
        if cell.value is None and cell.hyperlink is None and cell.comment is None:
            continue
        value = cell.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(Decimal(str(value)).normalize())
        else:
            value = str(value) if value is not None else None
        link = cell.hyperlink
        values.append([str(name), value, [link.target, link.location] if link else None,
                       cell.comment.text if cell.comment else None])
    return sorted(values)


def _snapshot_fingerprint(snapshot):
    return digest(json.dumps(sorted(snapshot), ensure_ascii=True).encode())


def _row_fingerprint(ws, row):
    # Byte-compatible with the preexisting audit marker.
    return _snapshot_fingerprint(_row_snapshot(ws, row))


def _archive_identity(snapshot):
    from src.report_layout import COLUMN_HEADERS_HE as display_headers
    values = {item[0]: item[1] for item in snapshot}
    result = {}
    for field in ('account','registration_number','receipt_number','receipt_date','amount','donor_id','tax_report_number'):
        value = values.get(COLUMN_HEADERS_HE[field], values.get(display_headers[field]))
        if field == 'receipt_date' and value:
            parsed = _donation_date(type('DateCell', (), {'value':value,'data_type':'s'})())
            if parsed:
                value = parsed.isoformat()
        result[field] = value
    return result


def set_archive_proof(ws, row, primary):
    snapshot = _row_snapshot(ws, row)
    proof = {'version':2, 'primary':primary, 'fingerprint':_snapshot_fingerprint(snapshot), 'snapshot':snapshot}
    raw = json.dumps(proof, ensure_ascii=True, sort_keys=True)
    if len(raw) > 32767:
        raise SyncError('historical_archive_capacity', 'Archive evidence exceeds safe Excel cell capacity; no publication')
    ws.cell(row, _column(ws, ARCHIVE), raw)


def _journal_archive(ws, row, old, snapshot, reference, reason, changed, manual=False):
    wb = ws.parent
    if ARCHIVE_AUDIT not in wb:
        journal = wb.create_sheet(ARCHIVE_AUDIT)
        journal.append(['Archive location','Archive migration v2'])
        journal.sheet_state = 'veryHidden'
    journal = wb[ARCHIVE_AUDIT]
    if [journal.cell(1,c).value for c in (1,2)] != ['Archive location','Archive migration v2']:
        raise SyncError('historical_archive_audit_invalid', 'Unknown archive audit schema; no publication')
    record = {'from_version':old.get('version',1), 'to_version':2, 'old_hash':old['fingerprint'],
              'new_hash':_snapshot_fingerprint(snapshot), 'reference':reference, 'reason':reason,
              'changed_fields':changed, 'manual_review':manual, 'previous_snapshot':old.get('snapshot')}
    # For v1, the corroborated full before-state is supplied by migration.
    if '_reference_snapshot' in old:
        record['previous_snapshot'] = old['_reference_snapshot']
    raw = json.dumps(record, ensure_ascii=True, default=str)
    if len(raw)>32767:
        raise SyncError('historical_archive_capacity','Archive audit cannot preserve evidence; no publication')
    journal.append([f'{ws.title}!{row}',raw])
    for cell in journal[journal.max_row]: cell.data_type='s'
    wb._archive_counts=getattr(wb,'_archive_counts',{})
    wb._archive_counts['archive_migrated']=wb._archive_counts.get('archive_migrated',0)+1
    if manual:wb._archive_counts['archive_manual_review']=wb._archive_counts.get('archive_manual_review',0)+1
    metric('archive_migrated')
    if manual: metric('archive_manual_review')


def _reference_archive(ws, row, proof, context):
    if proof.get('version') == 2:
        snapshot = proof.get('snapshot')
        if not isinstance(snapshot,list) or _snapshot_fingerprint(snapshot)!=proof['fingerprint']:
            raise SyncError('historical_archive_proof_invalid','Archive snapshot does not match its fingerprint; no publication')
        return snapshot, 'v2_snapshot'
    # A bounded exact reconstruction proves the known old default-fill bug.
    current = _row_snapshot(ws,row)
    from src.report_layout import _report_field_columns
    col = _report_field_columns(ws).get('section46_status')
    if col and ws.cell(row,col).value == 'לא ידוע / דורש בירור' and not ws.cell(row,col).comment:
        label = str(ws.cell(1,col).value)
        trial = [item for item in current if item[0]!=label]
        if _snapshot_fingerprint(trial)==proof['fingerprint']:
            return trial, 'exact_v1_default_status_reconstruction'
    entry = context['entries'].get(proof['primary'],{})
    # Link before-values already recorded by our writer can explain this exact
    # mutation; never guess prior text or approve an unexplained new digest.
    col = _report_field_columns(ws).get('file_name')
    if col:
        label = str(ws.cell(1,col).value)
        for repair in entry.get('historical_link_repairs',[]):
            if repair.get('sheet')!=ws.title or repair.get('row')!=row:
                continue
            for target in ([repair.get('previous_uri'),None],[None,repair.get('previous_uri')]):
                trial = [([item[0],repair.get('previous_name'),target,item[3]] if item[0]==label else item) for item in current]
                if _snapshot_fingerprint(trial)==proof['fingerprint']:
                    return trial, 'exact_v1_recorded_link_reconstruction'
    # Existing verified full-workbook backups, not arbitrary user workbooks.
    backup_folder = context['backup_folder']
    if backup_folder is None or not backup_folder.is_dir():
        backup_files = ()
    else:
        backup_files = sorted(backup_folder.glob('.backup-*.xlsx'))
    cache = context.setdefault('backup_snapshots', {})
    for path in backup_files:
        if path not in cache:
            cache[path] = _backup_archive_snapshots(path)
        checksum, snapshots = cache[path]
        candidate = snapshots.get((proof['primary'], ws.title, row, proof['fingerprint']))
        if candidate and candidate[1] and candidate[1] <= set(entry.get('documents', [])):
            return candidate[0], 'verified_backup:' + checksum
    raise SyncError('historical_archive_reference_missing','Archive v1 changed without a provable before-state; canonical preserved')


def _backup_archive_snapshots(path):
    """Use a candidate only if it reproduces the existing archive fingerprint."""
    try:
        raw = safe_path(path).read_bytes()
        checksum = digest(raw)
        if path.name != '.backup-' + checksum + '.xlsx':
            return '', {}
        backup = load(BytesIO(raw))
        try:
            found = {}
            if INDEX not in backup:
                return checksum, found
            for key, raw_entry in backup[INDEX].iter_rows(min_row=2, max_col=2, values_only=True):
                if key is None and raw_entry is None:
                    continue
                entry = json.loads(raw_entry)
                for alias in entry.get('historical_aliases', []):
                    target = backup[alias['sheet']]
                    row = alias['row']
                    snapshot = _row_snapshot(target, row)
                    fingerprint = _snapshot_fingerprint(snapshot)
                    proof = json.loads(target.cell(row, headers(target)[ARCHIVE]).value)
                    if proof.get('primary') == key and proof.get('fingerprint') == fingerprint:
                        found[key, target.title, row, fingerprint] = snapshot, set(entry['documents'])
            return checksum, found
        finally:
            backup.close()
    except (OSError, BadZipFile, ValueError, KeyError, TypeError, AttributeError):
        return '', {}


def _rebind_archive_owner(ws, row, old_owner, new_owner):
    """Change ownership without endorsing new archive values."""
    cell = ws.cell(row, headers(ws)[ARCHIVE])
    try:
        proof = json.loads(cell.value)
        if (proof['primary'] != old_owner or proof['fingerprint'] != _row_fingerprint(ws, row)
                or proof.get('version', 1) not in (1, 2)
                or (proof.get('version') == 2
                    and _snapshot_fingerprint(proof['snapshot']) != proof['fingerprint'])):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise SyncError('historical_archive_changed', 'Cannot rebind unverified archive; canonical preserved') from None
    proof['primary'] = new_owner
    cell.value = json.dumps(proof, ensure_ascii=True, sort_keys=True)


def _carry_archive_owner(ws, row, old_owner, new_owner):
    id_column = headers(ws).get(ID_HEADER)
    if id_column and ws.cell(row, id_column).value is not None:
        raise SyncError('historical_archive_orphan', 'Archive alias collides with an active row')
    try:
        _rebind_archive_owner(ws, row, old_owner, new_owner)
    except SyncError:
        # Ledger ownership moves; the unverified marker remains untouched.
        _archive_warning(ws, row)


def _migrate_archive(ws,row,proof,context):
    current=_row_snapshot(ws,row)
    if _snapshot_fingerprint(current)==proof['fingerprint']:
        before=current;reference='matching_v1_fingerprint'
    else:
        before,reference=_reference_archive(ws,row,proof,context)
    if _archive_identity(before)!=_archive_identity(current):
        raise SyncError('historical_archive_identity_changed','Archived donation identity or essential source fields changed; canonical preserved')
    entry=context['entries'].get(proof['primary'])
    if not entry or {'sheet':ws.title,'row':row} not in entry.get('historical_aliases',[]):
        raise SyncError('historical_archive_orphan','Archive is not owned by the publication index; canonical preserved')
    # Changed link spelling is derived only while it still binds to the same
    # indexed source; arbitrary source replacement is never accepted as a note.
    from src.report_layout import _report_field_columns
    col=_report_field_columns(ws).get('file_name')
    if col:
        label=str(ws.cell(1,col).value)
        old={item[0]:item for item in before}.get(label)
        now={item[0]:item for item in current}.get(label)
        if old!=now:
            from types import SimpleNamespace
            prior_link = old[2] if old else None
            previous = SimpleNamespace(value=old[1] if old else None, hyperlink=(
                SimpleNamespace(target=prior_link[0], location=prior_link[1]) if prior_link else None))
            arguments = (context['path'], context['receipts_base'], context['state']['documents'])
            prior_key = _external_document(previous, *arguments, scoped=context['scoped'])
            key = _external_document(ws.cell(row, col), *arguments, scoped=context['scoped'])
            if key not in entry['documents'] or key != prior_key:
                raise SyncError('historical_archive_source_changed','Archived PDF change is not explained by indexed source identity; canonical preserved')
    previous={item[0]:item for item in before};actual={item[0]:item for item in current}
    changed=sorted(name for name in previous.keys()|actual.keys() if previous.get(name)!=actual.get(name))
    # Do not endorse edits to derived fields as tax/name evidence. Keep them as
    # reviewable manual archive edits; active enrichment uses its own verified data.
    derived={COLUMN_HEADERS_HE['file_name']}
    if reference=='exact_v1_default_status_reconstruction':derived.add(LIVE_HEADERS[0])
    manual=any(name not in derived for name in changed)
    if manual:
        entry['historical_manual_conflicts']=sorted(set(entry.get('historical_manual_conflicts',[]))|set(changed))
    old={**proof,'_reference_snapshot':before}
    _journal_archive(ws,row,old,current,reference,'verified_upgrade' if not changed else 'identity_stable_change',changed,manual)
    set_archive_proof(ws,row,proof['primary'])


def _validated_archive_owner(ws, row):
    col=headers(ws).get(ARCHIVE)
    raw=ws.cell(row,col).value if col else None
    if not raw:
        if getattr(ws.parent, '_annual_archive_owners', {}).get((ws.title, row)):
            raise SyncError('historical_archive_changed', 'Owned archive has no fingerprint')
        return None
    try:
        proof=json.loads(raw)
        if proof.get('version',1) not in (1,2) or not proof['primary'] or ws.cell(row,headers(ws)[ID_HEADER]).value is not None:
            raise ValueError()
        actual=_row_fingerprint(ws,row)
        if proof.get('version')==2 and _snapshot_fingerprint(proof['snapshot'])!=proof['fingerprint']:
            raise ValueError()
    except (ValueError,KeyError,TypeError):
        raise SyncError('historical_archive_changed','Invalid historical archive evidence; canonical preserved') from None
    owners = getattr(ws.parent, '_annual_archive_owners', None)
    if owners is not None and owners.get((ws.title, row)) != proof['primary']:
        raise SyncError('historical_archive_orphan', 'Archive owner does not match publication index')
    context=getattr(ws.parent,'_archive_context',None)
    if actual!=proof['fingerprint'] or (context and proof.get('version',1)==1):
        if not context:
            raise SyncError('historical_archive_changed','Archived historical duplicate changed; review retained rows locally')
        _migrate_archive(ws,row,proof,context)
    ws.row_dimensions[row].hidden=True
    return proof['primary']


def _archive_warning(ws, row):
    # Aggregate counts only; do not print evidence, coordinates or values.
    seen = getattr(ws.parent, '_reported_archive_warnings', set())
    coordinate = (ws.title, row)
    if coordinate not in seen:
        seen.add(coordinate)
        ws.parent._reported_archive_warnings = seen
        metric('archive_proof_review_rows')
        snapshot('DEDUP', 'archive_proof_review', 'archive_proof_review_rows')


def archived_duplicate(ws, row):
    columns = headers(ws)
    id_column = columns.get(ID_HEADER)
    if id_column and ws.cell(row, id_column).value is not None:
        # A marker attached to an active row is an ownership conflict,
        # not an inactive evidence defect.
        return _validated_archive_owner(ws, row)
    owner = getattr(ws.parent, '_annual_archive_owners', {}).get((ws.title, row))
    col = columns.get(ARCHIVE)
    marker = ws.cell(row, col).value if col else None
    if not owner and marker and ws.row_dimensions[row].hidden:
        _archive_warning(ws, row)
        return True  # Inactive orphan: known retained row, no proved owner.
    try:
        return _validated_archive_owner(ws, row)
    except SyncError:
        if not owner:
            raise
        _archive_warning(ws, row)
        ws.row_dimensions[row].hidden = True
        return owner


def validate_historical_archives(wb, entries):
    expected = {(alias['sheet'], alias['row']): key for key, entry in entries.items()
                for alias in entry.get('historical_aliases', [])}
    observed = {}
    for ws in donation_sheets(wb):
        for row in range(2, ws.max_row + 1):
            primary = archived_duplicate(ws, row)
            if primary and primary is not True:
                observed[(ws.title, row)] = primary
    if observed != expected:
        raise SyncError('historical_archive_changed', 'Historical duplicate archive no longer matches publication index')


def _historical_manual_conflicts(records):
    """Record conflicting non-identity cells; Dedup alone decides consolidation.

    Primary values are retained, and every conflicting original value/formula
    remains in its archived row. These conflicts never create a second donation.
    """
    ignored = {ID_HEADER, ARCHIVE, COLUMN_HEADERS_HE['notes']}
    ignored |= {COLUMN_HEADERS_HE[k] for k in ('file_name', 'source_files', 'source_count')}
    names = {name for ws, row, entry in records for name in headers(ws)} - ignored
    conflicts = []
    for name in sorted(names, key=str):
        values = set()
        for ws, row, entry in records:
            col = headers(ws).get(name)
            value = ws.cell(row, col).value if col else None
            if value is not None and str(value).strip():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = str(Decimal(str(value)).normalize())
                values.add(str(value).strip())
        if len(values) > 1:
            conflicts.append(str(name))
    if any(ws.cell(row, headers(ws)[COLUMN_HEADERS_HE['notes']]).data_type == 'f'
           for ws, row, _ in records if COLUMN_HEADERS_HE['notes'] in headers(ws)):
        conflicts.append(COLUMN_HEADERS_HE['notes'])
    return conflicts


def same_historical_review(entries, ids):
    tokens = {entries[key].get('historical_review') for key in ids}
    return len(tokens) == 1 and None not in tokens



def receipt_source_evidence(path, meta):
    """Receipt scope shared by historical Dedup and monthly link verification."""
    from src import receipt_summary as reader
    from src.hebrew_text_normalizer import normalize_hebrew_text
    from src.receipt_metadata_extractor import receipt_numbers_in_text, receipt_amounts_in_text
    from src.receipt_deduplication import _identity, receipt_amount
    raw = ''
    for extract in (reader.extract_text_from_pdf, reader.extract_text_from_pdf_pymupdf):
        try:
            value = extract(path)
            if value:
                raw += '\n' + value
        except reader.PdfExtractionError:
            pass
    excluded = {meta.registration_number, meta.tax_report_number, meta.donor_id} - {''}
    numbers = receipt_numbers_in_text(raw, excluded) | receipt_numbers_in_text(normalize_hebrew_text(raw), excluded)
    identity_values = _identity(meta)
    amounts = {receipt_amount(value) for value in receipt_amounts_in_text(raw)} - {None}
    source_fields={field:getattr(meta,field) for field in ('registration_number','receipt_number','receipt_date','amount')}
    if len(numbers)==1:source_fields['receipt_number']=next(iter(numbers))
    return {'sha256': digest(path.read_bytes()), 'source_fields':source_fields, 'receipt_numbers':sorted(numbers),
            'multiple_amounts':len(amounts)>1, 'amounts': sorted(str(amount) for amount in amounts), 'core':(identity_values[0],identity_values[2],identity_values[3])}


def _historical_source_candidates(candidates, state, receipts_base, workbook_path):
    """Regroup indexed historical rows by proven physical source, not by filename."""
    source_rows, proofs = defaultdict(list), {}
    owners = defaultdict(set)
    for original, records in candidates.items():
        for _, _, entry in records:
            for doc in entry['documents']:
                owners[doc].add(original)
    for original, records in list(candidates.items()):
        for ws, row, entry in records:
            docs = entry['documents']
            from src.report_layout import _report_field_columns
            columns=_report_field_columns(ws)
            if any(ws.cell(row,columns[field]).data_type=='f' for field in ('registration_number','receipt_number','receipt_date','amount') if field in columns):
                proofs[(ws.title,row)]={'blocked':True}
                continue
            if docs:
                from src.report_layout import _resolve_pdf_link
                link_col = headers(ws).get(COLUMN_HEADERS_HE['file_name'])
                paths = document_paths(receipts_base.parent, receipts_base.name, state, docs)
                result = _resolve_pdf_link(ws.cell(row, link_col), paths, state, receipts_base.parent,
                    receipts_base.name, workbook_path, None, owners, original) if link_col else (None,)
                if result[0] is None:
                    # Read-only safety preflight: quarantine is applied monthly.
                    # Never merge a row until its source has a proven binding.
                    proofs[(ws.title, row)] = {'blocked': True}
                    continue
                doc = digest(result[0].read_bytes())
                source_rows[doc].append((original, (ws, row, entry)))
    for doc, members in source_rows.items():
        if len(members) < 2:
            continue
        paths = document_paths(receipts_base.parent, receipts_base.name, state, [doc])
        item = state['documents'][doc]
        proof = receipt_source_evidence(paths[0], effective_metadata(item))
        # Stable worksheet/row order: the first occurrence is always primary.
        original = members[0][0]
        for key, record in members:
            candidates[key].remove(record)
            if not candidates[key]:
                del candidates[key]
            proofs[(record[0].title, record[1])] = proof
        candidates.setdefault(original, []).extend(record for _, record in members)
    return proofs


def _numeric_oc_filename(path):
    if path.suffix.lower() != '.pdf':
        return None
    match = re.fullmatch(r'([0-9]+)([oc])', path.stem, re.IGNORECASE)
    if match:
        return match[1], match[2].upper(), False, match[1]
    return None


def _oc_role(path):
    if path.suffix.lower() != '.pdf':
        return None
    numeric = _numeric_oc_filename(path)
    if numeric:
        return ('compact', numeric[0]), numeric[1]
    match = re.fullmatch(r'(.+?)[ _-]+(original|copy)', path.stem, re.IGNORECASE)
    if match:
        return ('labelled', match[1].casefold()), match[2][0].upper()
    return None


def _oc_source_names(path, account, item):
    names = list(item.get('attachment_origins', []))
    if account == 'manual':
        names.append(item.get('metadata', {}).get('file_name', ''))
    return [path, *(Path(name) for name in names if isinstance(name, str)
                    and name and '/' not in name and chr(92) not in name)]


def _oc_source_role(path, account, item):
    roles = {_oc_role(name) for name in _oc_source_names(path, account, item)}
    roles.discard(None)
    return next(iter(roles)) if len(roles) == 1 else None


def _reserved_oc_names(first, second):
    """Keep disputed copy naming in the explicit O/C workflow.

    Detect rejected compact claims solely to prevent generic archival. These
    spellings never establish roles, shared stems, or donation identity.
    """
    if any(_oc_role(name) for name in (*first, *second)):
        return True

    def endings(names):
        return {(match[1].casefold(), match[2].upper()) for name in names
                if name.suffix.lower() == '.pdf'
                and (match := re.fullmatch(r'(.+?)([oc])(?:__[0-9]+)?', name.stem, re.I))}

    return any(left_role != right_role and (
        left == right or (left[-1].isdigit() and right[-1].isdigit())
    ) for left, left_role in endings(first) for right, right_role in endings(second))


def _oc_pdf_evidence(path, account, year, sha):
    from src.receipt_deduplication import _identity
    from src.receipt_summary import _extract_row

    fresh = _extract_row(path, account, None, year)
    core = _identity(fresh)
    proof = receipt_source_evidence(path, fresh)
    if (None in core or proof['sha256'] != sha or proof['receipt_numbers'] != [core[1]]
            or proof['multiple_amounts'] or 'תאריך מתוך שם הקובץ' in fresh.notes
            or any(note in fresh.notes for note in IDENTIFIER_REVIEW_NOTES.values())):
        return None
    return core, fresh


def verified_copy_source(sources, expected):
    """Return a COPY path only for two independently proven complementary PDFs.

    Sources are (sha, path, account, private catalog entry). Never infer a receipt
    number from an attachment name; never broaden the original compact rule.
    """
    from src.receipt_deduplication import _identity, _consistent_optional_fields

    if len(sources) != 2 or len({source[0] for source in sources}) != 2 or None in expected:
        return None
    evidence = []
    for sha, path, account, item in sources:
        if (not item.get('metadata') or item.get('review_decision', {}).get('action') == 'distinct'
                or item.get('verified_identifier_updates') or item.get('donation_amount_override') is not None
                or item.get('donation_date_override') is not None):
            return None
        extracted = effective_metadata(item)
        if _identity(extracted) != expected:
            return None
        proof = _oc_pdf_evidence(path, account, expected[2].year, sha)
        if not proof or proof[0] != expected:
            return None
        evidence.append((path, account, item, proof[1], extracted))
    if not all(_consistent_optional_fields([entry[index] for entry in evidence])
               for index in (3, 4)):
        return None
    roles = [_oc_source_role(path, account, item)
             for path, account, item, _, _ in evidence]
    if None in roles:
        return None
    first, second = roles
    if first[0] != second[0] or {first[1], second[1]} != {'O', 'C'}:
        return None
    return evidence[0 if first[1] == 'C' else 1][0]


def preferred_group_paths(paths, account, state, meta):
    """Keep the existing path order unless the shared O/C proof succeeds."""
    from src.receipt_deduplication import _identity
    if len(paths) != 2:
        return paths
    sources = []
    for path in paths:
        sha = digest(path.read_bytes())
        sources.append((sha, path, account, state['documents'].get(sha, {})))
    preferred = verified_copy_source(sources, _identity(meta))
    if preferred is None:
        return paths
    return tuple([preferred] + [path for path in paths if path != preferred])


def apply_group_copy_preference(entries, rows, state, receipts_base):
    """Add representation evidence to already consolidated, owned rows only."""
    from src.report_layout import _report_field_columns
    changed = set()
    for key, (ws, row) in rows.items():
        entry = entries[key]
        if (not entry.get('owned_source_fields') or len(entry.get('documents', [])) != 2
                or entry.get('representative_document') or entry.get('identifier_approvals')
                or entry.get('historical_review') or entry.get('historical_manual_conflicts')):
            continue
        cols = _report_field_columns(ws)
        if any(f not in cols or ws.cell(row, cols[f]).comment
               or ws.cell(row, cols[f]).data_type in ('f', 'b', 'e')
               for f in ('registration_number', 'receipt_number', 'receipt_date', 'amount', 'tax_report_number', 'file_name')):
            continue
        managed = entry.get('managed', {})
        if any(COLUMN_HEADERS_HE[f] in managed and ws.cell(row, cols[f]).value != managed[COLUMN_HEADERS_HE[f]]
               for f in ('registration_number', 'receipt_number', 'receipt_date', 'amount', 'tax_report_number', 'file_name')):
            continue
        published = metadata(ws, row)
        paths = document_paths(receipts_base.parent, receipts_base.name, state, entry['documents'])
        if len(paths) != 2:
            continue
        from src.receipt_deduplication import _identity
        candidates = []
        for path in paths:
            sha = digest(path.read_bytes())
            candidates.append((sha, path, receipts_base.name, state['documents'].get(sha, {})))
        preferred = verified_copy_source(candidates, _identity(published))
        if preferred is None:
            continue
        sha = digest(preferred.read_bytes())
        if sha not in entry['documents']:
            continue
        # Only add verified complementary reporting evidence. Preserve a
        # nonempty human value and a deliberately cleared managed cell.
        tax_cell = ws.cell(row, cols['tax_report_number'])
        tax_label = COLUMN_HEADERS_HE['tax_report_number']
        fresh = effective_metadata(state['documents'][sha])
        if published.tax_report_number and fresh.tax_report_number and published.tax_report_number != fresh.tax_report_number:
            continue
        if fresh.tax_report_number and tax_cell.value in (None, ''):
            from src.receipt_deduplication import _identity
            proof = _oc_pdf_evidence(preferred, receipts_base.name, _identity(published)[2].year, sha)
            if proof and proof[1].tax_report_number == fresh.tax_report_number:
                tax_cell.value = fresh.tax_report_number
                tax_cell.data_type = 's'
                entry.setdefault('managed', {})[tax_label] = tax_cell.value
        entry['representative_document'] = sha
        changed.add(key)
    return changed


def _original_copy_pairs(rows, entries, state, receipts_base):
    """Pair distinct, SHA-verified sources with matching receipt identities."""
    from src.receipt_deduplication import _identity, _consistent_optional_fields

    verified_by_identity = defaultdict(list)
    for key, (ws, row) in rows.items():
        row_meta = metadata(ws, row)
        core = _identity(row_meta)
        if None in core or entries[key].get('representative_document') in entries[key]['documents']:
            continue
        for doc in entries[key]['documents']:
            item = state['documents'].get(doc, {})
            if not item.get('metadata') or item.get('review_decision', {}).get('action') == 'distinct':
                continue
            extracted = effective_metadata(item)
            if _identity(extracted) != core or any(note in extracted.notes for note in IDENTIFIER_REVIEW_NOTES.values()):
                continue
            verified_by_identity[core].append((key, doc, item, extracted, row_meta))

    groups = defaultdict(list)
    for core, candidates in verified_by_identity.items():
        if len({doc for _, doc, *_ in candidates}) != 2:
            continue
        proven = []
        for key, doc, item, extracted, row_meta in candidates:
            paths = document_paths(receipts_base.parent, receipts_base.name, state, [doc])
            if paths:
                evidence = _oc_pdf_evidence(paths[0], receipts_base.name, core[2].year, doc)
                if evidence and evidence[0] == core:
                    proven.append((key, doc, item, extracted, row_meta, paths, evidence[1]))
        if len({doc for _, doc, *_ in proven}) < 2:
            continue
        for key, doc, item, extracted, row_meta, paths, fresh in proven:
            roles = {_oc_role(name) for path in paths
                     for name in _oc_source_names(path, receipts_base.name, item)}
            roles.discard(None)
            if len(roles) == 1:
                stem, role = next(iter(roles))
                groups[core, stem].append((key, doc, role, extracted, row_meta, fresh))

    pairs = {}
    for (core, _), group in groups.items():
        options = {(key, doc, role): (extracted, row_meta, path)
                   for key, doc, role, extracted, row_meta, path in group}
        if len(options) != 2 or {item[2] for item in options} != {'O', 'C'}:
            continue
        first, second = options
        if first[1] == second[1]:
            continue
        extracted, published, fresh = zip(*options.values())
        if not all(_consistent_optional_fields(values) for values in (extracted, published, fresh)):
            continue
        representative = next(doc for _, doc, role in options if role == 'C')
        pairs.setdefault(first[0], []).append((second[0], representative))
        if first[0] != second[0]:
            pairs.setdefault(second[0], []).append((first[0], representative))
    return {key: values[0] for key, values in pairs.items()
            if len(values) == len(pairs[values[0][0]]) == 1}


def _verified_identity_pairs(rows, entries, state, receipts_base):
    """Different PDF bytes can represent one receipt. Require independent source proof.

    Do not guess from hashes or filenames; no merge on competing identities,
    missing source evidence, manual identity edits, or explicit distinct decisions.
    """
    from src.receipt_deduplication import _identity, _consistent_optional_fields
    from src.report_layout import _report_field_columns

    candidates = defaultdict(list)
    for key, pair in rows.items():
        core = _identity(metadata(*pair))
        if None not in core:
            candidates[core].append(key)
    pairs = {}
    for core, keys in candidates.items():
        if len(keys) != 2:  # No ambiguous bridge or multi-publication guess.
            continue
        verified = []
        for key in keys:
            entry = entries[key]
            ws, row = rows[key]
            # Restrict auto reconciliation to managed, unambiguous publications.
            if (not entry.get('owned_source_fields') or len(entry['documents']) != 1
                    or entry.get('historical_review') or entry.get('historical_manual_conflicts')
                    or entry.get('identifier_approvals')):
                break
            columns = _report_field_columns(ws)
            if any(field not in columns or ws.cell(row, columns[field]).comment
                   or ws.cell(row, columns[field]).data_type in ('f', 'b', 'e')
                   for field in ('registration_number', 'receipt_number', 'receipt_date', 'amount',
                                 'tax_report_number', 'donor_id')):
                break
            doc = entry['documents'][0]
            item = state['documents'].get(doc, {})
            if (not item.get('metadata') or item.get('review_decision', {}).get('action') == 'distinct'
                    or item.get('verified_identifier_updates') or item.get('donation_amount_override') is not None
                    or item.get('donation_date_override') is not None):
                break
            extracted = effective_metadata(item)
            if _identity(extracted) != core:
                break
            paths = document_paths(receipts_base.parent, receipts_base.name, state, [doc])
            if not paths:
                break
            proof = _oc_pdf_evidence(paths[0], receipts_base.name, core[2].year, doc)
            if not proof or proof[0] != core:
                break
            published = metadata(ws, row)
            if any(getattr(published, field) != getattr(extracted, field)
                   or (getattr(published, field) and getattr(published, field) != getattr(proof[1], field))
                   for field in ('tax_report_number', 'donor_id')):
                break
            verified.append((key, doc, proof[1], published,
                             [name for path in paths for name in _oc_source_names(path, receipts_base.name, item)]))
        else:
            if verified[0][1] == verified[1][1] or _reserved_oc_names(verified[0][4], verified[1][4]):
                continue
            if not (_consistent_optional_fields([v[2] for v in verified])
                    and _consistent_optional_fields([v[3] for v in verified])):
                continue
            # This is a generic match, not an O/C assertion. Prefer the source
            # with verified complementary reporting evidence, then stable SHA.
            preferred = min(verified, key=lambda v: (not bool(v[2].tax_report_number), v[1]))
            a, b = keys
            pairs[a] = (b, preferred[1])
            pairs[b] = (a, preferred[1])
    return pairs


def reviewed_oc_pair(rows, entries, state, receipts_base, original, copied, *, year=None):
    """Verify current PDF identities before reading candidate role names."""
    from src.receipt_deduplication import _identity, _consistent_optional_fields

    account = receipts_base.name

    def sources(key):
        if key not in rows:
            raise SyncError('oc_review_owner', 'Both publication IDs must be active')
        core = _identity(metadata(*rows[key]))
        if any(core[i] is None for i in (0, 2, 3)) or (year is not None and core[2].year != year):
            raise SyncError('oc_review_identity', 'A valid organization, report date and amount are required')
        verified = []
        for sha in entries[key]['documents']:
            item = state['documents'].get(sha, {})
            if item.get('review_decision', {}).get('action') == 'distinct':
                raise SyncError('oc_review_distinct', 'A source was explicitly reviewed as distinct')
            for path in document_paths(receipts_base.parent, account, state, [sha]):
                if not path.is_relative_to(receipts_base):
                    raise SyncError('oc_review_source', 'Source is outside the receipt account')
                evidence = _oc_pdf_evidence(path, account, core[2].year, sha)
                if evidence is None:
                    raise SyncError('oc_pdf_identity_unverified', 'PDF receipt identity could not be verified')
                fresh_core, fresh = evidence
                if any(fresh_core[i] != core[i] for i in (0, 2, 3)) or (core[1] and fresh_core[1] != core[1]):
                    raise SyncError('oc_pdf_identity_conflict',
                                    'PDF evidence contradicts the published identity; one verified numeric O/C source is required')
                verified.append((sha, path, item, fresh_core, fresh))
        if not verified:
            raise SyncError('oc_review_source', 'Each publication must have one verified numeric O/C source or explicit role label')
        return verified

    originals, copies = sources(original), sources(copied)
    first_core, second_core = originals[0][3], copies[0][3]
    if (any(item[3] != first_core for item in originals)
            or any(item[3] != second_core for item in copies)):
        raise SyncError('oc_review_source', 'Each publication must have one unambiguous PDF identity')
    for field, index in (('organization', 0), ('date', 2), ('amount', 3), ('receipt', 1)):
        if first_core[index] != second_core[index]:
            raise SyncError(f'oc_pair_{field}_conflict', 'The reviewed O/C files do not share a consistent identity')
    if not _consistent_optional_fields([item[4] for item in originals + copies]):
        raise SyncError('oc_pair_evidence_conflict', 'The reviewed O/C files do not share a consistent identity')

    def by_role(sources, wanted):
        matches = {}
        roles_by_sha = defaultdict(dict)
        for sha, path, item, _, _ in sources:
            for name in _oc_source_names(path, account, item):
                role = _oc_role(name)
                if role:
                    roles_by_sha[sha][role] = path
        for sha, roles in roles_by_sha.items():
            if len(roles) == 1:
                (stem, role), path = next(iter(roles.items()))
                if role == wanted:
                    matches[(sha, stem)] = path
        if len(matches) != 1:
            raise SyncError('oc_review_source', 'Each publication must have one verified numeric O/C source or explicit role label')
        return next(iter(matches.items()))

    (original_sha, original_stem), _ = by_role(originals, 'O')
    (copy_sha, copy_stem), copy_path = by_role(copies, 'C')
    if original_stem != copy_stem:
        raise SyncError('oc_pair_stem_conflict', 'The reviewed O/C files do not share a consistent identity')
    if original == copied or original_sha == copy_sha:
        raise SyncError('oc_pair_same_publication_or_sha', 'The reviewed O/C files do not share a consistent identity')
    if any({original_sha, copy_sha}.intersection(item['documents'])
           for key, item in entries.items() if key not in (original, copied) and key in rows):
        raise SyncError('oc_review_owner', 'A reviewed source belongs to another active publication')
    organization, _, date, amount = first_core
    return {'original': original, 'copy': copied, 'original_sha': original_sha, 'copy_sha': copy_sha,
            'copy_name': copy_path.name, 'organization': organization, 'date': date.isoformat(),
            'amount': str(amount.normalize())}


def filter_verified_pdf_rows(wb, entries, rows, state, receipts_base, *, year=None):
    """Retain the first publication for SHA matches and verified O/C pairs.

    Retain every excluded row and its manual cells in place using the existing
    hidden-copy bookkeeping. Only source ownership follows the retained row.
    """
    order={ws.title:i for i,ws in enumerate(wb)}
    rank={key:(order[ws.title],row) for key,(ws,row) in rows.items()}
    pairs = _original_copy_pairs(rows, entries, state, receipts_base)
    # Explicit O/C evidence takes precedence over a generic logical match.
    for key, value in _verified_identity_pairs(rows, entries, state, receipts_base).items():
        partner = value[0]
        if key not in pairs and partner not in pairs:
            pairs[key] = value
            pairs[partner] = (key, value[1])
    reviewed = {}
    for proof in state.get('reviewed_oc_pairs', {}).values():
        if year is not None and proof['date'][:4] != str(year):
            continue
        original, copied = proof['original'], proof['copy']
        if original not in rows or copied not in rows:
            if any(key in rows and entries[key].get('reviewed_oc_pair') == proof for key in (original, copied)):
                continue
            raise SyncError('oc_review_stale', 'Reviewed O/C publications changed before reconciliation')
        if (original in reviewed or copied in reviewed
                or reviewed_oc_pair(rows, entries, state, receipts_base, original, copied, year=year) != proof):
            raise SyncError('oc_review_stale', 'Reviewed O/C evidence changed before reconciliation')
        for key in (original, copied):
            auto = pairs.pop(key, None)
            if auto and auto[0] != key:
                pairs.pop(auto[0], None)
            reviewed[key] = proof
            # Install both reviewed pairings after removing automatic neighbours.
    for key, proof in reviewed.items():
        partner = proof['copy'] if key == proof['original'] else proof['original']
        pairs[key] = (partner, proof['copy_sha'])
    from src.receipt_deduplication import _identity, _consistent_optional_fields

    owners = defaultdict(set)
    source_evidence = {}
    removed = 0
    for key in sorted(rows, key=rank.get):
        if key not in rows:continue
        verified=set()
        for path in document_paths(receipts_base.parent,receipts_base.name,state,entries[key]['documents']):
            sha=digest(path.read_bytes())
            if sha not in entries[key]['documents']:
                raise SyncError('pdf_source_changed','PDF changed during SHA verification; canonical preserved')
            verified.add(sha)
            if owners[sha] and sha not in source_evidence:
                source_evidence[sha] = receipt_source_evidence(path, effective_metadata(state['documents'][sha]))
        previous = set()
        current = metadata(*rows[key])
        current_id = _identity(current)
        for sha in verified:
            if not owners[sha]:
                continue
            proof = source_evidence[sha]
            for owner in owners[sha]:
                older = metadata(*rows[owner])
                older_id = _identity(older)
                conflicts = any(getattr(current, field) and getattr(older, field)
                                and getattr(current, field) != getattr(older, field)
                                for field in ('tax_report_number', 'donor_id'))
                if conflicts:
                    continue
                single = (len(proof['receipt_numbers']) == 1 and not proof['multiple_amounts']
                          and None not in proof['core']
                          and all(all(identity[i] in (None, value)
                                      for i, value in zip((0, 2, 3), proof['core']))
                                  for identity in (current_id, older_id)))
                labelled_copy = (None not in current_id and current_id == older_id
                                 and current_id[1] in proof['receipt_numbers']
                                 and not proof['multiple_amounts']
                                 and _consistent_optional_fields([current, older]))
                if single or labelled_copy:
                    previous.add(owner)
        partner, representative = pairs.get(key, (None, None))
        if partner == key:
            entries[key]['representative_document'] = representative
        copy_match = partner in rows and rank[partner] < rank[key]
        if copy_match:
            previous.add(partner)
        if len(previous) > 1:
            raise SyncError('historical_source_conflict',
                            'Historical row matches multiple active publications')
        if previous:
            primary=min(previous|{key},key=rank.get)
            for duplicate in sorted((previous|{key})-{primary},key=rank.get):
                if duplicate not in rows:continue
                target=entries[primary];old=entries[duplicate];sheet,row=rows[duplicate]
                shared = set(target['documents']) & set(old['documents'])
                target['documents']=sorted(set(target['documents'])|set(old['documents']))
                primary_sheet, primary_row = rows[primary]
                from src.report_layout import _report_field_columns
                proven = [source_evidence[sha] for sha in shared if sha in source_evidence
                          and len(source_evidence[sha]['receipt_numbers']) == 1
                          and not source_evidence[sha]['multiple_amounts']
                          and None not in source_evidence[sha]['core']]
                if len(proven) == 1:
                    source = proven[0]['source_fields']
                    for field in ('receipt_number', 'receipt_date'):
                        column = _report_field_columns(primary_sheet).get(field)
                        if column is None:
                            continue
                        cell = primary_sheet.cell(primary_row, column)
                        if cell.value is None and not cell.comment and source[field]:
                            cell.value = source[field]
                            cell.data_type = 's'
                for field in ('organization_name', 'donor_name'):
                    dst = _report_field_columns(primary_sheet).get(field)
                    source_col = _report_field_columns(sheet).get(field)
                    if dst is None or source_col is None:
                        continue
                    current_cell = primary_sheet.cell(primary_row, dst)
                    correction = sheet.cell(row, source_col)
                    auto = target.get('managed', {}).get(COLUMN_HEADERS_HE[field])
                    label = primary_sheet.cell(1, dst).value
                    auto = target.get('managed', {}).get(label, auto)
                    if field == 'organization_name' and auto is None:
                        names = {effective_metadata(state['documents'][doc]).organization_name
                                 for doc in target['documents'] if doc in state['documents']}
                        if len(names) == 1:
                            auto = next(iter(names))
                    if (current_cell.value in (None, '', auto) and not current_cell.comment
                            and current_cell.data_type != 'f' and correction.comment
                            and correction.value not in (None, '') and correction.data_type != 'f'):
                        current_cell.value = correction.value
                        current_cell.data_type = correction.data_type
                        current_cell.comment = copy(correction.comment)
                # Carry user formulas into empty primary cells.
                standard = set(COLUMN_HEADERS_HE.values()) | {ID_HEADER, ARCHIVE, SORT_ROW}
                for label, col in headers(sheet).items():
                    cell = sheet.cell(row, col)
                    if label in standard or cell.data_type != 'f':
                        continue
                    destination = primary_sheet.cell(primary_row, _column(primary_sheet, label))
                    if destination.value is None:
                        from openpyxl.formula.translate import Translator
                        destination.value = Translator(cell.value, origin=cell.coordinate).translate_formula(destination.coordinate)
                if copy_match and duplicate in (key, partner):
                    target['representative_document'] = representative
                    if key not in reviewed:
                        field = 'tax_report_number'
                        target_col = _report_field_columns(primary_sheet).get(field)
                        if target_col is not None:
                            cell = primary_sheet.cell(primary_row, target_col)
                            value = effective_metadata(state['documents'][representative]).tax_report_number
                            label = COLUMN_HEADERS_HE[field]
                            managed = target.get('managed', {})
                            if (target.get('owned_source_fields') and value and cell.value in (None, '')
                                    and cell.comment is None and cell.data_type != 'f'
                                    and (label not in managed or managed[label] == cell.value)
                                    and not any(item.get('identifier_approvals') for item in (target, old))):
                                cell.value = value
                                cell.data_type = 's'
                                target.setdefault('managed', {})[label] = value
                    prior_approvals = old.get('identifier_approvals', {})
                    if prior_approvals:
                        approvals = target.setdefault('identifier_approvals', {})
                        if any(k in approvals and approvals[k] != v for k, v in prior_approvals.items()):
                            raise SyncError('oc_pair_approval_conflict', 'O/C publications have conflicting approvals')
                        approvals.update(prior_approvals)
                    if key in reviewed:
                        target['reviewed_oc_pair'] = reviewed[key]
                elif 'representative_document' not in target and old.get('representative_document'):
                    target['representative_document'] = old['representative_document']
                target['historical_publication_ids']=sorted((set(target.get('historical_publication_ids',[]))|
                    set(old.get('historical_publication_ids',[]))|{duplicate})-{primary})
                aliases=[*old.get('historical_aliases',[]),{'sheet':sheet.title,'row':row}]
                for alias in aliases:
                    if alias not in target.setdefault('historical_aliases', []):
                        target['historical_aliases'].append(alias)
                    if (alias['sheet'], alias['row']) != (sheet.title, row):
                        _carry_archive_owner(wb[alias['sheet']], alias['row'], duplicate, primary)
                sheet.cell(row,headers(sheet)[ID_HEADER]).value=None
                set_archive_proof(sheet,row,primary)
                sheet.column_dimensions[openpyxl.utils.get_column_letter(headers(sheet)[ARCHIVE])].hidden=True
                sheet.row_dimensions[row].hidden=True
                for sha in old['documents']:
                    if duplicate in owners[sha]:
                        owners[sha].remove(duplicate)
                        owners[sha].add(primary)
                for doc in old['documents']:
                    if doc in state['documents']:
                        state['documents'][doc]['logical_id']=primary
                        state['documents'][doc].pop('historical_review_ids',None)
                del rows[duplicate];del entries[duplicate];removed+=1
            for sha in verified:
                owners[sha].add(primary)
        else:
            for sha in verified:
                owners[sha].add(key)
    for key in rows:
        token = entries[key].get('historical_review')
        if isinstance(token, str) and token.startswith('sha-review-') and len(owners.get(token[11:], ())) < 2:
            entries[key].pop('historical_review')
    for sha, keys in owners.items():
        if len(keys) < 2:
            continue
        tokens = {entries[key].get('historical_review') for key in keys} - {None}
        if len(tokens) > 1:
            raise SyncError('historical_source_conflict', 'Overlapping historical source reviews')
        token = next(iter(tokens), 'sha-review-' + sha)
        for key in keys:
            entries[key]['historical_review'] = token
            ws, row = rows[key]
            notes = ws.cell(row, _column(ws, COLUMN_HEADERS_HE['notes']))
            if not notes.comment and notes.data_type != 'f' and REVIEW_NOTE not in str(notes.value or ''):
                notes.value = '; '.join(filter(None, [str(notes.value or ''), REVIEW_NOTE]))
    for proof in reviewed.values():
        survivors = [key for key in (proof['original'], proof['copy']) if key in rows]
        if len(survivors) != 1 or entries[survivors[0]].get('reviewed_oc_pair') != proof:
            raise SyncError('oc_review_stale', 'Reviewed pair did not reconcile uniquely')
    wb._annual_archive_owners={(a['sheet'],a['row']):key for key,item in entries.items()
                               for a in item.get('historical_aliases',[])}
    if removed:
        wb._historical_merge_counts=getattr(wb,'_historical_merge_counts',{'merged_groups':0,'merged_copies':0})
        wb._historical_merge_counts['merged_copies']+=removed
        metric('merged_copies',removed)
    return removed


def rebind_published_locations(wb, indexed):
    """Keep archive and legacy source pointers attached to whole sorted rows."""
    from src.report_layout import PDF_AUDIT
    from openpyxl.utils.cell import coordinate_from_string

    moves = {}
    anchored_sheets = set()
    for ws in donation_sheets(wb):
        columns = headers(ws)
        anchor = columns.get(SORT_ROW)
        if not anchor:
            continue  # First publication of Task B migrates the current positions.
        anchored_sheets.add(ws.title)
        seen = {}
        for row in range(2, ws.max_row + 1):
            old = ws.cell(row, anchor).value
            if old is None:
                continue
            if type(old) is not int or old < 2:
                raise SyncError('publication_sort_conflict', 'Invalid published row position; canonical preserved')
            if old in seen:
                first = seen[old]
                id_col = columns[ID_HEADER]
                key = ws.cell(row, id_col).value
                if not key or ws.cell(first, id_col).value != key or old not in (first, row):
                    raise SyncError('publication_sort_conflict', 'Ambiguous published row position; canonical preserved')
                row = old  # A copy of the indexed row is handled by existing Dedup.
            seen[old] = row
            moves[(ws.title, old)] = row
    for entry in indexed.values():
        for alias in entry.get('historical_aliases', []):
            location = alias['sheet'], alias['row']
            if alias['sheet'] in anchored_sheets and location not in moves:
                raise SyncError('publication_sort_conflict', 'Archived row position missing; canonical preserved')
            alias['row'] = moves.get(location, alias['row'])
    if anchored_sheets or indexed:
        wb._annual_archive_owners = {(alias['sheet'], alias['row']): key
                                    for key, entry in indexed.items()
                                    for alias in entry.get('historical_aliases', [])}
    if SOURCE_SHEET in wb:
        for cells in wb[SOURCE_SHEET].iter_rows(min_row=2, max_col=1):
            cell = cells[0]
            link = cell.hyperlink
            target = (link.target or link.location) if link else None
            if not target or '!' not in target:
                continue
            sheet, ref = target.rsplit('!', 1)
            title = sheet.lstrip('#').strip("'").replace("''", "'")
            if not ref.lstrip('$').startswith('A') or not ref.replace('$', '')[1:].isdigit():
                continue
            old = int(ref.replace('$', '')[1:])
            row = moves.get((title, old), old)
            if row != old:
                if cell.value == f'{title}!{old}':
                    cell.value = f'{title}!{row}'
                if link.target:
                    link.target = f'#{quote_sheetname(title)}!A{row}'
                else:
                    link.location = f'{quote_sheetname(title)}!A{row}'
    if PDF_AUDIT in wb:
        for location, in wb[PDF_AUDIT].iter_rows(min_row=2, max_col=1):
            if not isinstance(location.value, str) or '!' not in location.value:
                continue
            title, coordinate = location.value.rsplit('!', 1)
            column, old = coordinate_from_string(coordinate)
            row = moves.get((title, old), old)
            if row != old:
                location.value = f'{title}!{column}{row}'


def stamp_published_locations(wb, entries, rows):
    positions = defaultdict(set)
    for key, (ws, row) in rows.items():
        positions[ws.title].add(row)
        for alias in entries[key].get('historical_aliases', []):
            positions[alias['sheet']].add(alias['row'])
    for title, published in positions.items():
        ws = wb[title]
        col = _column(ws, SORT_ROW)
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].hidden = True
        for row in range(2, ws.max_row + 1):
            ws.cell(row, col).value = row if row in published else None
        if ws.auto_filter.ref:
            ws.auto_filter.ref = ws.dimensions


def bootstrap(wb, path, receipts_base, account, state, *, reconcile_sources=False, identity_scope=None, scoped=False, archive_backup_dir=None):
    """Use existing Dedup BEFORE constructing the unique publication index.

    In full 2026, verified content SHA alone controls first-row visibility;
    no receipt fields are compared or completed. Retained copies stay in place.
    Uncertain rows get distinct persistent IDs and an explicit shared-source
    review relation. A physical document is never arbitrary proof of one row.
    """
    sha2026=reconcile_sources and path.parent.name=='2026'
    indexed = {}
    had_index = INDEX in wb.sheetnames
    if had_index:
        index = wb[INDEX]
        if index['A1'].value != 'Donation ID' or index['B1'].value != 'Publication v1':
            raise ValueError('Invalid publication index')
        for key, payload in index.iter_rows(min_row=2, max_col=2, values_only=True):
            if key is None and payload is None:
                continue  # The managed summary can occupy C2 before any publication.
            if key in indexed or not key:
                raise ValueError('Duplicate publication identity')
            indexed[key] = json.loads(payload)
    rebind_published_locations(wb, indexed)
    if reconcile_sources:
        backup_folder = safe_path(archive_backup_dir) if archive_backup_dir else None
        wb._archive_context = dict(entries=indexed, state=state, path=path,
                                   receipts_base=receipts_base, scoped=scoped,
                                   backup_folder=backup_folder)
    candidates = defaultdict(list)
    archives = []
    source_links = defaultdict(set)
    for ws in wb:
        if ws.title in (INDEX, SOURCES):
            continue
        for row in ws.iter_rows(min_row=2):
            if len(row) < 3 or not row[0].hyperlink:
                continue
            target = row[0].hyperlink.target or row[0].hyperlink.location or ''
            if target.startswith('#'):
                target = target[1:]
            if '!' not in target:
                continue
            title, reference = target.rsplit('!', 1)
            title = title.strip("'").replace("''", "'")
            key = _external_document(row[2], path, receipts_base, state['documents'], scoped=scoped)
            if key:
                source_links[(title, reference.lstrip('$A').replace('$', ''))].add(key)
    for ws in donation_sheets(wb):
        columns = headers(ws)
        idcol = _column(ws, ID_HEADER)
        ws.column_dimensions[openpyxl.utils.get_column_letter(idcol)].hidden = True
        for row in range(2, ws.max_row + 1):
            primary = archived_duplicate(ws, row)
            if primary:
                archives.append(primary)
                continue
            if not any(ws.cell(row, col).value is not None for name, col in columns.items() if name != SORT_ROW):
                continue
            meta = metadata(ws, row)
            key = ws.cell(row, idcol).value
            if had_index:
                if key not in indexed:
                    raise SyncError('missing_publication_id', 'Donation row missing its publication identity; restore ID locally')
                entry = deepcopy(indexed[key])
            else:
                docs = {_external_document(cell, path, receipts_base, state['documents'], scoped=scoped) for cell in ws[row]}
                docs.discard(None)
                docs |= source_links[(ws.title, str(row))]
                if docs:
                    key = identity(identity_scope or account, docs)
                else:
                    stable = {k: getattr(meta, k) for k in ('registration_number', 'receipt_number', 'receipt_date',
                                                          'amount', 'donor_id', 'organization_name', 'file_name')}
                    key = 'legacy-' + digest((account + json.dumps(stable, sort_keys=True)).encode())
                entry = {'documents': sorted(docs), 'managed': {}}
            candidates[key].append((ws, row, entry))
    if had_index and set(indexed)-set(candidates):
        raise SyncError('removed_published_row', 'Published row removed; restore locally rather than silently republish')
    original_ids = {(ws.title, row): key for key, records in candidates.items() for ws, row, _ in records}
    proofs = _historical_source_candidates(candidates, state, receipts_base, path) if reconcile_sources and not sha2026 else {}
    source_locations = defaultdict(set)
    for records in candidates.values():
        for ws, row, entry in records:
            for doc in entry['documents']:
                source_locations[doc].add((ws.title, row))
    entries, rows, legacy = {}, {}, {}
    for original, records in candidates.items():
        tokens = {Path(str(i)): record for i, record in enumerate(records)}
        records = []
        for token, (ws, row, item) in tokens.items():
            meta = metadata(ws, row)
            note = item.get('source_review_note')
            if reconcile_sources and note and meta.notes == note['after']:
                # Generated source flags are re-applied monthly if still pending;
                # do not copy a resolved flag into the retained primary's notes.
                meta.notes = str(note['before'] or '')
            records.append((token, meta))
        donations = ([Donation(meta,(token,)) for token,meta in records] if sha2026 else consolidate_receipts(records,
            source_proofs={token: proofs[(ws.title, row)] for token, (ws, row, _) in tokens.items()
                           if (ws.title, row) in proofs}))
        parts = [([tokens[token] for token in donation.sources], donation.metadata) for donation in donations]
        for number, (members, merged) in enumerate(parts):
            ws, row, entry = members[0]
            previous_id = original_ids[(ws.title, row)]
            key = original if number == 0 else (previous_id if previous_id != original else
                original + '-h' + digest((ws.title + ':' + str(row)).encode())[:16])
            if key in entries or (key != original and key in candidates):
                raise SyncError('historical_identity_collision', 'Derived historical identity already exists')
            ws.cell(row, headers(ws)[ID_HEADER], key)
            entry['documents'] = sorted({doc for _, _, item in members for doc in item['documents']})
            if len(members) > 1:
                proof = proofs.get((ws.title, row))
                outside = set().union(*(source_locations[doc] for doc in entry['documents'])) - {(w.title, r) for w,r,_ in members}
                if len(parts) == 1 and not outside:
                    entry.pop('historical_review', None)
                    merged.notes = '; '.join(note for note in merged.notes.split('; ') if note != REVIEW_NOTE)
                if proof and len(proof.get('receipt_numbers', [])) == 1 and not proof.get('multiple_amounts'):
                    entry['source_identity_resolution'] = {'version': 1, 'sha256': proof['sha256'],
                        'receipt_numbers': proof['receipt_numbers'], 'rows_archived': len(members)-1}
                from src.receipt_summary import _cell_value
                from src.report_layout import _report_field_columns
                columns=_report_field_columns(ws)
                for field in ('registration_number','receipt_number','receipt_date','amount'):
                    cell=ws.cell(row,columns[field])
                    value=getattr(merged,field)
                    if cell.value in (None,'') and value and not cell.comment:
                        cell.value=_cell_value(field,merged)
                        if isinstance(cell.value,str):cell.data_type='s'
                # A uniquely evidenced human correction can replace an automatic
                # name/status, while the first physical row and its approved
                # manual values remain primary. Original rows remain archived.
                for field in ('organization_name','donor_name'):
                    col=columns.get(field)
                    if not col:continue
                    target=ws.cell(row,col);label=ws.cell(1,col).value
                    primary_managed=entry.get('managed',{})
                    old_auto=(target.value in (None,'') or any(target.value==primary_managed.get(k) for k in (label,COLUMN_HEADERS_HE[field]) if k in primary_managed))
                    if not old_auto and field=='organization_name':
                        old_auto=any(target.value==effective_metadata(state['documents'][doc]).organization_name for doc in entry['documents'] if doc in state['documents'])
                    if target.comment or target.data_type=='f' or not old_auto:continue
                    corrections=[]
                    for other,other_row,item in members[1:]:
                        other_col=_report_field_columns(other).get(field)
                        if not other_col:continue
                        source=other.cell(other_row,other_col);key_name=other.cell(1,other_col).value
                        managed=item.get('managed',{})
                        approved=source.comment is not None or any(source.value!=managed[k] for k in (key_name,COLUMN_HEADERS_HE[field]) if k in managed)
                        if approved and source.value not in (None,''):corrections.append(source)
                    if corrections and len({str(c.value) for c in corrections})==1:
                        entry.setdefault('historical_primary_updates',[]).append({'field':field,'before':target.value,'after':corrections[0].value,'reason':'preserved_manual_correction'})
                        target.value=corrections[0].value;target.data_type=corrections[0].data_type
                        target.comment=copy(corrections[0].comment)
                conflicts = _historical_manual_conflicts(members)
                if conflicts:
                    entry['historical_manual_conflicts'] = sorted(set(entry.get('historical_manual_conflicts', [])) | set(conflicts))
                    review_col = _column(ws, 'Historical identity review')
                    ws.cell(row, review_col, 'תיקונים ידניים סותרים נשמרו בשורות המקור המאוחדות — נדרשת בדיקה')
                    ws.column_dimensions[openpyxl.utils.get_column_letter(review_col)].hidden = True
                report_stage('DEDUP')
                metric('merged_groups'); metric('merged_copies', len(members)-1)
                wb._historical_merge_counts=getattr(wb,'_historical_merge_counts',{'merged_groups':0,'merged_copies':0})
                wb._historical_merge_counts['merged_groups']+=1
                wb._historical_merge_counts['merged_copies']+=len(members)-1
                for other, other_row, other_entry in members[1:]:
                    old_id = original_ids[(other.title, other_row)]
                    prior_ids = set(other_entry.get('historical_publication_ids', [])) | {old_id}
                    entry['historical_publication_ids'] = sorted((set(entry.get('historical_publication_ids', [])) | prior_ids) - {key})
                    for alias in other_entry.get('historical_aliases', []):
                        archived = wb[alias['sheet']]
                        _carry_archive_owner(archived, alias['row'], old_id, key)
                        if alias not in entry.setdefault('historical_aliases', []):
                            entry['historical_aliases'].append(alias)
                    # Complement blank manual/technical cells without overwriting
                    # primary values. Every original alias remains in place.
                    for name, col in headers(other).items():
                        if name in (ID_HEADER, ARCHIVE, SORT_ROW):
                            continue
                        source = other.cell(other_row, col)
                        target = ws.cell(row, _column(ws, name))
                        if target.value is None and source.value is not None:
                            target.value = source.value
                            copy_cell_style(source, target)
                            target.hyperlink = copy(source.hyperlink)
                            target.comment = copy(source.comment)
                    other.cell(other_row, headers(other)[ID_HEADER]).value = None
                    archive_col = _column(other, ARCHIVE)
                    other.column_dimensions[openpyxl.utils.get_column_letter(archive_col)].hidden = True
                    set_archive_proof(other,other_row,key)
                    other.row_dimensions[other_row].hidden = True
                    item = {'sheet':other.title, 'row':other_row}
                    if item not in entry.setdefault('historical_aliases', []):
                        entry['historical_aliases'].append(item)
                notes = ws.cell(row, _column(ws, COLUMN_HEADERS_HE['notes']))
                if COLUMN_HEADERS_HE['notes'] not in conflicts:
                    notes.value = merged.notes or None
            if len(parts) > 1 and not sha2026:
                entry['historical_review'] = original
                notes = ws.cell(row, _column(ws, COLUMN_HEADERS_HE['notes']))
                if REVIEW_NOTE not in str(notes.value or ''):
                    # Preserve formulas rather than replacing a manual expression.
                    if notes.data_type != 'f':
                        notes.value = '; '.join(filter(None, [str(notes.value or ''), REVIEW_NOTE]))
                _column(ws, 'Historical identity review')
                review_col = headers(ws)['Historical identity review']
                ws.cell(row, review_col, REVIEW_NOTE)
                ws.column_dimensions[openpyxl.utils.get_column_letter(review_col)].hidden = True
            entries[key], rows[key] = entry, (ws, row)
            if not entry['documents']:
                legacy['legacy:' + key] = metadata(ws, row)
    if sha2026:
        filter_verified_pdf_rows(wb,entries,rows,state,receipts_base)
        legacy={key:meta for key,meta in legacy.items() if key.removeprefix('legacy:') in rows}
    if reconcile_sources:
        wb._annual_archive_owners={(alias['sheet'],alias['row']):key for key,item in entries.items() for alias in item.get('historical_aliases',[])}
    archives = [archived_duplicate(ws, row) for ws in donation_sheets(wb) for row in range(2, ws.max_row+1)
                if ARCHIVE in headers(ws) and ws.cell(row, headers(ws)[ARCHIVE]).value]
    if not reconcile_sources and any(key is not True and key not in entries for key in archives):
        raise SyncError('historical_archive_orphan', 'Archived duplicate has no active publication')
    if reconcile_sources:
        wb._archive_context['entries'] = entries
    validate_historical_archives(wb, entries)
    return entries, rows, legacy, had_index


def _column(ws, name, *, semantic=False):
    mapping = headers(ws)
    if semantic and name == COLUMN_HEADERS_HE['organization_name']:
        from src.report_layout import _report_field_columns
        col = _report_field_columns(ws).get('organization_name')
        if col is not None:
            return col
    col = mapping.get(name)
    if col is None:
        col = ws.max_column + 1
        ws.cell(1, col, name).font = Font(bold=True)
    return col


def copy_cell_style(source, target):
    # Style IDs belong to their workbook; never copy _style across workbooks.
    for field in ('font', 'fill', 'border', 'alignment', 'protection'):
        setattr(target, field, copy(getattr(source, field)))
    target.number_format = source.number_format



def sync_approved_identifiers(entries, rows, state, assignments, *, year, legacy=False):
    """Publish explicitly reviewed PDF identifier changes to existing rows.

    Updates are keyed by content SHA through the current publication index,
    never by file name or row number. An untracked value, manual override,
    formula, comment, or ambiguous source aborts *before* changing any cells.
    The attestation lives in the private catalog entry, not in the workbook.
    """
    from src.report_layout import _report_field_columns
    from src.sync_state import AMOUNT_REVIEW_SOURCE, IDENTIFIER_FIELDS, IDENTIFIER_REVIEW_SOURCE
    from src.receipt_deduplication import receipt_amount

    owners = defaultdict(set)
    for donation_id, entry in entries.items():
        if donation_id not in rows:
            continue
        for sha in set(entry.get('documents', [])) | set(assignments.get(donation_id, ())):
            if sha in state['documents']:
                owners[sha].add(donation_id)

    planned = {}
    approvals = []
    for sha, item in sorted(state['documents'].items()):
        approved = item.get('verified_identifier_updates', {})
        if not approved:
            continue
        date = donation_date(effective_metadata(item))
        if not date or date.year != year:
            continue
        if not isinstance(approved, dict):
            raise SyncError('identifier_approval_invalid', 'Verified identifier metadata invalid; original preserved')
        approved = {
            field: proof for field, proof in approved.items()
            if not isinstance(proof, dict)
            or proof.get('source') != 'task_a_2026_reviewed' or legacy
        }
        if not approved:
            continue
        if len(owners[sha]) != 1:
            raise SyncError('identifier_owner_ambiguous', 'Approved source has no unique published donation; original preserved')
        donation_id = next(iter(owners[sha]))
        ws, row = rows[donation_id]
        columns = _report_field_columns(ws)
        managed = entries[donation_id].get('managed', {})
        for field, proof in sorted(approved.items()):
            amount = field == 'amount'
            if (field not in (*IDENTIFIER_FIELDS, 'amount')
                    or not isinstance(proof, dict)
                    or not ((proof.get('source') == AMOUNT_REVIEW_SOURCE if amount
                             else proof.get('source') == IDENTIFIER_REVIEW_SOURCE) and proof.get('year') == year
                            or legacy and not amount and proof.get('source') == 'task_a_2026_reviewed')
                    or not isinstance(proof.get('value'), str)
                    or not isinstance(proof.get('before'), str)
                    or (not proof['value'] and not (proof['source'] == IDENTIFIER_REVIEW_SOURCE and proof['before']
                        and (field in {'receipt_number', 'donor_id'} or
                             field == 'tax_report_number' and proof.get('operator_verified_absent'))))
                    or (receipt_amount(proof['value']) is None if amount
                        else proof['value'] != item.get('metadata', {}).get(field))
                    or field not in columns):
                raise SyncError('identifier_approval_invalid', 'Verified identifier metadata invalid; original preserved')
            entry = entries[donation_id]
            proof_key = f'{sha}:{field}'
            proof_hash = digest(json.dumps(proof, sort_keys=True).encode())
            if entry.get('identifier_approvals', {}).get(proof_key) == proof_hash:
                continue
            if proof['source'] in (IDENTIFIER_REVIEW_SOURCE, AMOUNT_REVIEW_SOURCE) and sha not in entry.get('documents', []):
                raise SyncError('identifier_owner_ambiguous', 'Reviewed source has no established publication; original preserved')
            cell = ws.cell(row, columns[field])
            old = '' if cell.value is None else str(cell.value)
            if proof['source'] == 'task_a_2026_reviewed':
                old = old.strip()
            new = float(receipt_amount(proof['value'])) if amount else proof['value']
            if amount and (proof.get('publication') != donation_id or not isfinite(new)
                           or cell.comment is not None or cell.data_type in ('f', 'b', 'e')):
                raise SyncError('amount_manual_conflict', 'Protected amount cell; original preserved')
            old_value = cell.value if amount else old
            key = (donation_id, field)
            if key in planned and planned[key][1] != new:
                raise SyncError('identifier_multiple_values', 'Approved PDFs disagree on published identifier; original preserved')
            # Include already-equal values in the agreement check, without claiming ownership.
            label = COLUMN_HEADERS_HE[field]
            planned[key] = (cell, new, entry, label, old_value != new)
            approvals.append((entry, proof_key, proof_hash))
            if old_value == new:
                continue
            managed_differs = label in managed and (
                managed[label] != cell.value if amount else str(managed[label] or '') != old
            )
            if (cell.comment is not None or cell.data_type == 'f'
                    or managed_differs
                    or old != proof['before']):
                raise SyncError('identifier_manual_conflict', 'Published identifier differs from approved baseline; original preserved')

    # No writes until the complete approval set has passed all conflict checks.
    updated = 0
    for cell, new, entry, label, changed in planned.values():
        if changed:
            cell.value = new or None
            if isinstance(new, str) and new:
                cell.data_type = 's'
            entry.setdefault('managed', {})[label] = new
            updated += 1
    for entry, proof_key, proof_hash in approvals:
        entry.setdefault('identifier_approvals', {})[proof_key] = proof_hash
    return updated

def _protected_status_display(ws, row):
    from src.report_layout import _DISPLAY_MARKER

    for header in ws[1]:
        if header.comment and header.comment.text == _DISPLAY_MARKER + 'section46_status':
            display = ws.cell(row, header.column)
            previous = 'donation-receipts:display:v1\n' + json.dumps(display.value, ensure_ascii=False)
            return display.comment is None or display.comment.text != previous
    return False


def enrich_candidates(wb, entries, rows, additions, root, live, config, stage, original_sha=None, *, preserve_verified=False, source_state=None, review_counts=None):
    """Use the existing renderer and enrichment once; preserve human-owned cells."""
    preserve_verified = preserve_verified or INDEX in wb and wb[INDEX]['D1'].value == 'Full report layout v1'
    original_columns = {ws.title: set(headers(ws)) for ws in donation_sheets(wb)}
    candidates = []
    keys = []
    for key, (ws, row) in rows.items():
        meta = metadata(ws, row)
        if entries[key].get('original_extracted_name') is not None:
            meta.organization_name = entries[key]['original_extracted_name']
        candidates.append(Donation(meta, (Path('historical.pdf'),)))
        keys.append(key)
    for key, donation in additions.items():
        candidates.append(donation)
        keys.append(key)
    if not candidates:
        return {}
    report_stage('EXCEL')
    template = openpyxl.Workbook()
    template.remove(template.active)
    sources = template.create_sheet(SOURCE_SHEET)
    sources.append(['תרומה בדוח', 'חודש שמירת מקור', 'פתיחת קובץ מקור'])
    _write_month_sheet(template, MONTH_NAMES[1], candidates, sources)
    ws = template[_MONTH_SHEET_NAMES[MONTH_NAMES[1]]]
    ws.title = 'Rows'
    idcol = _column(ws, ID_HEADER)
    for row, key in enumerate(keys, 2):
        ws.cell(row, idcol, key)
    source, enriched = stage / 'input.xlsx', stage / 'enriched.xlsx'
    template.save(source)
    template.close()
    report_stage('REGISTRY')
    enrich_workbook(source, live.registry._path if live is not None else root / 'organizations.json', enriched, live=live)
    report_stage('EXCEL')
    result = load(enriched)
    try:
        src = result['Rows']
        cols = headers(src)
        audit = {}
        if 'Registry identity audit' in result.sheetnames:
            for row in result['Registry identity audit'].iter_rows(min_row=2, values_only=True):
                audit[row[1]] = list(row)
        pending_audit = {}
        for number, key in enumerate(keys, 2):
            if key in additions:
                donated = datetime.strptime(additions[key].metadata.receipt_date, '%d/%m/%Y').date()
                title = _MONTH_SHEET_NAMES[MONTH_NAMES[donated.month]]
                target = wb[title] if title in wb.sheetnames else wb.create_sheet(title)
                if target.max_row == 1 and target['A1'].value is None:
                    target.sheet_view.rightToLeft = True
                    target.freeze_panes = 'A2'
                    for dimension, value in src.column_dimensions.items():
                        target.column_dimensions[dimension].width = value.width
                        target.column_dimensions[dimension].hidden = value.hidden
                    for cell in src[1]:
                        dest = target.cell(1, cell.column, cell.value)
                        copy_cell_style(cell, dest)
                        dest.font = Font(bold=True)
                row = target.max_row + 1
                for name, col in cols.items():
                    cell = src.cell(number, col)
                    dest = target.cell(row, _column(target, name), cell.value)
                    dest.data_type = cell.data_type
                    copy_cell_style(cell, dest)
                    if cell.hyperlink and name == COLUMN_HEADERS_HE['file_name']:
                        dest.hyperlink = copy(cell.hyperlink)
                rows[key] = (target, row)
                entries[key] = {'documents': [], 'managed': {}, 'owned_source_fields': True,
                                'original_extracted_name': additions[key].metadata.organization_name}
                name_col = _column(target, COLUMN_HEADERS_HE['organization_name'], semantic=True)
                entries[key]['managed'][COLUMN_HEADERS_HE['organization_name']] = target.cell(row, name_col).value
                original_cell = target.cell(row, _column(target, ORIGINAL_NAME), additions[key].metadata.organization_name)
                original_cell.data_type = 's'
            target, row = rows[key]
            entry = entries[key]
            managed = entry.setdefault('managed', {})
            official = audit.get(number, [None] * 5)[4]
            main_name = COLUMN_HEADERS_HE['organization_name']
            name_cell = target.cell(row, _column(target, main_name, semantic=True))
            original_cell = target.cell(row, _column(target, ORIGINAL_NAME))
            recorded = entry.get('original_extracted_name') or original_cell.value
            if recorded is None and source_state is not None:
                extracted = {effective_metadata(source_state['documents'][doc]).organization_name
                             for doc in entry.get('documents', []) if doc in source_state['documents']}
                if len(extracted) == 1:
                    recorded = extracted.pop()
                    entry['original_extracted_name'] = recorded
                    if original_cell.value is None:
                        original_cell.value = recorded; original_cell.data_type = 's'
            if recorded is not None and original_cell.value is None:
                original_cell.value = recorded; original_cell.data_type = 's'
            actual_name = target.cell(1, name_cell.column).value
            legacy_col = headers(target).get(main_name)
            managed_name = actual_name if (actual_name in managed or
                (legacy_col is not None and legacy_col != name_cell.column)) else main_name
            # An explicit managed snapshot takes precedence over raw-text equality:
            # changing a generated official name is a deliberate human override.
            auto_name = (name_cell.value == managed[managed_name] if managed_name in managed else
                         name_cell.value in (None, '') or (recorded is not None and name_cell.value == recorded))
            if official and auto_name and name_cell.comment is None and name_cell.data_type != 'f':
                if entry.get('original_extracted_name') is None:
                    entry['original_extracted_name'] = recorded if recorded is not None else name_cell.value
                if original_cell.value is None:
                    original_cell.value = entry['original_extracted_name']
                    original_cell.data_type = 's'
                name_cell.value = official; name_cell.data_type = 's'
                managed[managed_name] = official
            enrichment_headers = LIVE_HEADERS if live is not None else HEADERS
            updates = {**({OFFICIAL: official} if official is not None else {}),
                       **{name: src.cell(number, cols[name]).value for name in enrichment_headers}}

            def can_write(name, cell):
                return (cell.comment is None and cell.data_type != 'f' and
                        (key in additions or name not in original_columns.get(target.title, set()) or
                         (name in managed and cell.value == managed[name]) or
                         (cell.value in (None, '') and (name != LIVE_HEADERS[0]
                          or SORT_ROW not in original_columns.get(target.title, set())))))

            for name, value in updates.items():
                cell = target.cell(row, _column(target, name))
                allowed = can_write(name, cell)
                if live is not None and name == LIVE_HEADERS[0] and value == 'מאושר' and cell.value != value:
                    companions = [(label, target.cell(row, _column(target, label))) for label in LIVE_HEADERS[1:]]
                    if any(existing.value != updates[label] and not can_write(label, existing)
                           for label, existing in companions):
                        metric('section46_companions_preserved')
                        metric('section46_write_blocked')
                        continue
                applied_proof = None
                if (live is not None and name == LIVE_HEADERS[0] and value in ('מאושר', 'אין אישור מוכח לתאריך')
                        and not allowed and cell.value != value):
                    proof = (source_state or {}).get('historical_section46', {}).get(key)
                    meta = metadata(target, row)
                    donated = donation_date(meta)
                    if (value == 'מאושר' and isinstance(proof, dict) and donated
                            and proof.get('before') == cell.value == REVIEW_STATUS
                            and proof.get('number') == meta.registration_number.strip()
                            and proof.get('date') == donated.isoformat() and name not in managed
                            and proof.get('approval_id') and entry.get('historical_section46_applied') != proof
                            and cell.comment is None and cell.data_type != 'f'
                            and not _protected_status_display(target, row)
                            and live.registry.official_identity(proof['number'])):
                        allowed = True
                        applied_proof = proof
                    else:
                        metric('section46_write_blocked')
                if allowed:
                    if (preserve_verified and name == LIVE_HEADERS[0]
                            and not str(updates[LIVE_HEADERS[3]]).startswith(CONFLICT_DETAILS)
                            and cell.value in ('מאושר', 'אין אישור מוכח לתאריך')
                            and value not in ('מאושר', 'אין אישור מוכח לתאריך')):
                        continue
                    cell.value = value
                    if isinstance(value, str):
                        cell.data_type = 's'
                    managed[name] = value
                    if applied_proof:
                        entry['historical_section46_applied'] = applied_proof
                        if review_counts is not None:
                            review_counts['staged'] += 1
            audit_identity = ([audit[number][2], audit[number][4], *audit[number][6:9]] if number in audit else None)
            if number in audit and entry.get('identity_audit') != audit_identity:
                record = audit[number]
                record[0], record[1] = target.title, row
                record[9] = original_sha or 'new annual workbook'
                pending_audit[key] = record
                entry['identity_audit'] = audit_identity
            idcol = _column(target, ID_HEADER)
            target.column_dimensions[openpyxl.utils.get_column_letter(idcol)].hidden = True
        if pending_audit:
            name = 'Registry identity audit'
            audit_ws = next((sheet for sheet in wb if sheet.title.casefold() == name.casefold()), None)
            if audit_ws is None:
                audit_ws = wb.create_sheet(name)
            if audit_ws['A1'].value is None:
                for cell in result[name][1]:
                    audit_ws.cell(1, cell.column, cell.value)
            for record in pending_audit.values():
                audit_ws.append(record)
                for cell in audit_ws[audit_ws.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = 's'
        metric('registry_matches', len(audit))
        metric('rows_enriched', len(keys))
        for target, row in (rows.values() if live is not None else ()):
            label = target.cell(row, headers(target)[LIVE_HEADERS[0]]).value
            metric('approved_rows' if label == 'מאושר' else
                   'denied_rows' if label == 'אין אישור מוכח לתאריך' else 'other_status_rows')
        emit('EXCEL', 'complete', rows_enriched=len(keys))
        return rows
    finally:
        result.close()


def apply_verified_table_amounts(entries, rows, state, receipts_base, year):
    """Apply source-proven parser repairs only to untouched owned amount cells.

    The catalog keeps the old/new evidence through interrupted publications;
    retry accepts an already corrected value but never overwrites a manual one.
    """
    from src.receipt_deduplication import receipt_amount
    from src.receipt_summary import _extract_row
    from src.report_layout import _report_field_columns
    updated = 0
    for key, (ws, row) in rows.items():
        entry = entries[key]
        if (not entry.get('owned_source_fields') or len(entry.get('documents', [])) != 1
                or entry.get('historical_review') or entry.get('historical_manual_conflicts')):
            continue
        sha = entry['documents'][0]
        item = state['documents'].get(sha, {})
        candidate = item.get('table_amount_candidate')
        if not candidate or item.get('donation_amount_override') is not None or 'amount' in item.get('verified_identifier_updates', {}):
            continue
        col = _report_field_columns(ws).get('amount')
        if not col:
            continue
        cell = ws.cell(row, col)
        old, new = (receipt_amount(candidate[k]) for k in ('before', 'after'))
        published = metadata(ws, row)
        catalog = effective_metadata(item)
        core = ('registration_number', 'receipt_number', 'receipt_date')
        if (old is None or new is None or cell.comment or cell.data_type in ('f','b','e')
                or not all(getattr(published, f) and getattr(published, f) == getattr(catalog, f) for f in core)
                or receipt_amount(published.amount) not in (old, new)):
            continue
        label = COLUMN_HEADERS_HE['amount']
        managed = entry.get('managed', {})
        if label in managed and managed[label] != cell.value:
            continue
        paths = document_paths(receipts_base.parent, receipts_base.name, state, [sha])
        if not paths:
            continue
        path = paths[0]
        fresh = _extract_row(path, receipts_base.name, None, year)
        if (digest(path.read_bytes()) != sha or not fresh.payment_total_verified or fresh.full_source_serial
                or receipt_amount(fresh.amount) != new
                or not all(getattr(fresh, f) == getattr(catalog, f) for f in core)
                or any(n in fresh.notes for n in IDENTIFIER_REVIEW_NOTES.values())):
            continue
        if receipt_amount(published.amount) == old:
            cell.value = float(new)
            entry.setdefault('managed', {})[label] = cell.value
            updated += 1
        item['metadata']['amount'] = fresh.amount
        item['metadata']['payment_total_verified'] = True
    return updated



def apply_verified_tax_layout(entries, rows, state, receipts_base, year):
    """Fill a genuinely blank managed tax field with source-proven evidence.

    Do not change a nonempty field, an approved blank, a comment, formula,
    imported history, ambiguous publication, or a conflicting PDF.
    """
    from src.receipt_deduplication import receipt_amount
    from src.receipt_summary import _extract_row
    from src.report_layout import _report_field_columns
    from src.sync_state import apply_tax_layout_metadata
    owners = Counter(sha for key, entry in entries.items() if key in rows
                     for sha in set(entry.get('documents', ())))
    updated = 0
    for key, (ws, row) in rows.items():
        entry = entries[key]
        if (not entry.get('owned_source_fields') or len(entry.get('documents', [])) != 1
                or entry.get('historical_review') or entry.get('historical_manual_conflicts')):
            continue
        sha = entry['documents'][0]
        item = state['documents'].get(sha, {})
        value = item.get('tax_layout_candidate')
        if (not value or owners[sha] != 1 or 'tax_report_number' in item.get('verified_identifier_updates', {})
                or f'{sha}:tax_report_number' in entry.get('identifier_approvals', {})):
            continue
        col = _report_field_columns(ws).get('tax_report_number')
        if not col:
            continue
        cell = ws.cell(row, col)
        label = COLUMN_HEADERS_HE['tax_report_number']
        managed = entry.get('managed', {})
        if (cell.comment or cell.data_type in ('f','b','e') or cell.value not in (None, '', value)
                or (label in managed and managed[label] != cell.value)):
            continue
        published, catalog = metadata(ws, row), effective_metadata(item)
        core = ('registration_number', 'receipt_number', 'receipt_date')
        date = donation_date(catalog)
        if (not date or date.year != year or catalog.tax_report_number not in ('', value)
                or not all(getattr(published, f) and getattr(published, f) == getattr(catalog, f) for f in core)
                or receipt_amount(published.amount) is None or receipt_amount(published.amount) != receipt_amount(catalog.amount)):
            continue
        if cell.value == value and catalog.tax_layout_verified and catalog.tax_report_number == value:
            continue
        paths = document_paths(receipts_base.parent, receipts_base.name, state, [sha])
        if not paths:
            continue
        path = paths[0]
        fresh = _extract_row(path, receipts_base.name, None, year)
        if (digest(path.read_bytes()) != sha or not fresh.tax_layout_verified or fresh.tax_report_number != value
                or not all(getattr(fresh, f) == getattr(catalog, f) for f in core)
                or receipt_amount(fresh.amount) != receipt_amount(catalog.amount)
                or any(n in fresh.notes for n in IDENTIFIER_REVIEW_NOTES.values())):
            continue
        if cell.value in (None, ''):
            cell.value = value
            cell.data_type = 's'
            entry.setdefault('managed', {})[label] = cell.value
            updated += 1
        apply_tax_layout_metadata(item, value)
    return updated

def refresh_published_rows(entries, rows, state, config, year):
    from src.receipt_deduplication import receipt_amount
    from src.report_layout import PDF_REVIEW, _report_field_columns
    from src.receipt_summary import _cell_value, _DONOR_MATCH_HE, _SEVERITY_HE, _STATUS_HE, refresh_published_warnings

    for key, (ws, row) in rows.items():
        entry = entries[key]
        meta = metadata(ws, row)
        resolved = set()
        distinct = []
        for sha in entry.get('documents', ()):
            source = state['documents'].get(sha, {})
            approved = source.get('verified_identifier_updates', {})
            ledger = entry.get('identifier_approvals', {})
            for field, proof in approved.items():
                if f'{sha}:{field}' not in ledger or field not in ('receipt_number', 'tax_report_number', 'amount'):
                    continue
                value = proof.get('value')
                current = getattr(meta, field)
                if field == 'amount':
                    value, current = receipt_amount(value), receipt_amount(current)
                if value == current:
                    resolved.add(field)
            if (source.get('donation_amount_override') is not None
                    and receipt_amount(meta.amount) == receipt_amount(source['donation_amount_override'])):
                resolved.add('amount')
            if source.get('donation_date_override') == meta.receipt_date and meta.receipt_date:
                resolved.add('receipt_date')
            if (source.get('metadata', {}).get('tax_layout_verified')
                    and meta.tax_report_number and source['metadata'].get('tax_report_number') == meta.tax_report_number):
                resolved.add('tax_report_number')
            distinct.append(source.get('review_decision', {}).get('action') == 'distinct')
        duplicate_resolved = (not (entry.get('historical_review') or entry.get('historical_manual_conflicts')
                                   or entry.get('source_review_note'))
                              and (bool(distinct) and all(distinct) or bool(entry.get('source_identity_resolution'))))
        refresh_published_warnings(meta, config, year, resolved=resolved, duplicate_resolved=duplicate_resolved)
        columns = _report_field_columns(ws)
        # Imported history has no ownership proof for its existing display
        # review cells. A verified correction can refresh their warnings.
        if not resolved and not entry.get('owned_source_fields'):
            continue
        for field in ('notes', 'donor_match', 'severity', 'extraction_status'):
            if field not in columns:
                continue
            cell = ws.cell(row, columns[field])
            if cell.comment or cell.data_type == 'f':
                continue
            if field == 'severity' and cell.value not in (None, '', *_SEVERITY_HE.values()):
                # Preserve the separate registry classification and manual categories.
                continue
            if field == 'extraction_status' and cell.value not in (None, '', *_STATUS_HE.values()):
                continue
            if field == 'donor_match' and cell.value not in (None, '', *_DONOR_MATCH_HE.values()):
                continue
            value = _cell_value(field, meta) or None
            if cell.value != value:
                snapshot = entry.get('source_review_note') if field == 'notes' else None
                if snapshot and cell.value == snapshot['after']:
                    # Keep generated source flags reversible after rebuilding the other notes.
                    before = '; '.join(note for note in (value or '').split('; ') if note != PDF_REVIEW)
                    snapshot.update(before=before or None, after=value)
                cell.value = value
                if value is not None:
                    cell.data_type = 's'


def classify_receipts(wb, entries, rows, registry, live=None):
    """Classify monthly active rows using the same dated evidence as enrichment.

    Recorded values confer ownership, not approval. Human literals, comments and
    formulas are preserved; missing evidence never implies a tax denial.
    """
    from src.report_layout import _report_field_columns, _apply_primary_layout, _pdf_audit
    from src.receipt_deduplication import receipt_amount
    from src.receipt_summary import _SEVERITY_HE
    from src.receipt_metadata_extractor import _is_valid_corporate_registration_number
    counts=defaultdict(int)
    pending_sources={record.get('owner') for _,record in _pdf_audit(wb).values() if record.get('pending')}
    for key,(ws,row) in rows.items():
        entry=entries[key];meta=metadata(ws,row);columns=_report_field_columns(ws)
        number=meta.registration_number.strip();date=donation_date(meta)
        identified=bool(_is_valid_corporate_registration_number(number) and registry.official_identity(number))
        result=registry.lookup_tax(number,date) if identified and date else {}
        tax=result.get('section46_status','unknown')
        if result.get('stale') or result.get('conflict'):tax='unknown'
        if date and _is_valid_corporate_registration_number(number) and live is not None:
            _, decided, outcome = dated_section46(registry, live, number, date)
            if identified:
                tax = decided
            if outcome in ('approved', 'verified_denial'):
                metric('section46_decision_valid')
            elif outcome in ('historical_gap', 'stale_evidence', 'conflicting_evidence'):
                metric('section46_evidence_invalid')
            else:
                metric('section46_evidence_missing')
        elif live is not None:
            metric('section46_input_invalid')
        complete=bool(date and receipt_amount(meta.amount) and identified)
        review=bool(key in pending_sources or entry.get('source_review_note') or entry.get('historical_review') or entry.get('historical_manual_conflicts'))
        if complete and tax=='approved' and not review:
            label='הכי טוב' if meta.receipt_number.strip() else 'טוב'
        elif complete and tax=='denied' and not review:
            label='לא זכאית'
        else:label='דורשת בדיקה'
        col=columns.get('severity')
        if not col:continue
        cell=ws.cell(row,col);snapshot=entry.get('classification_v1')
        # Old extraction labels were product-generated. Existing richer labels
        # without a snapshot may be intentional and are left to their owner.
        allowed=(cell.comment is None and cell.data_type!='f' and
                 (cell.value in (None,'') or (snapshot is not None and cell.value==snapshot) or
                  (snapshot is None and cell.value in set(_SEVERITY_HE.values()))))
        if allowed:
            cell.value=label;cell.data_type='s';entry['classification_v1']=label
        counter={'הכי טוב':'classified_best','טוב':'classified_good','לא זכאית':'classified_denied'}.get(cell.value,'classified_review' if cell.value=='דורשת בדיקה' else 'classified_manual')
        counts[counter]+=1;metric(counter)
    for ws in {pair[0] for pair in rows.values()}:
        _apply_primary_layout(ws)
    return dict(counts)


def write_sources(wb, entries, rows, state, receipts_dir, account):
    ws = wb[SOURCES] if SOURCES in wb.sheetnames else wb.create_sheet(SOURCES)
    if ws['A1'].value is None:
        for col, name in enumerate(['Donation ID', 'Document SHA256', 'Original PDF'], 1):
            ws.cell(1, col, name)
    aliases = {old: key for key, entry in entries.items() for old in entry.get('historical_publication_ids', [])}
    for row in range(2, ws.max_row+1):
        if ws.cell(row, 1).value in aliases:
            ws.cell(row, 1, aliases[ws.cell(row, 1).value])
    from src.report_layout import _local_pdf_address
    existing = {}
    for row in range(2, ws.max_row + 1):
        existing.setdefault((ws.cell(row, 1).value, ws.cell(row, 2).value), row)
    for key, entry in entries.items():
        first = None
        for doc in sorted(entry['documents'], key=lambda doc: doc != entry.get('representative_document')):
            for path in document_paths(receipts_dir, account, state, [doc]):
                link = _local_pdf_address(path)
                source_id = key, doc
                if source_id not in existing:
                    ws.append([key, doc, path.name])
                    ws.cell(ws.max_row, 3).data_type = 's'
                    ws.cell(ws.max_row, 3).hyperlink = link
                    ws.cell(ws.max_row, 3).font = Font(size=11)
                    existing[source_id] = ws.max_row
                source = ws.cell(existing[source_id], 3)
                if (source.comment is None and source.hyperlink and
                        source.hyperlink.target == path.resolve().as_uri()):
                    source.hyperlink = link
                if first is None:
                    first = existing[source_id]
        target, row = rows[key]
        cols = headers(target)
        if entry.get('owned_source_fields'):
            col_count = _column(target, COLUMN_HEADERS_HE['source_count'])
            cell_count = target.cell(row, col_count)
            if 'source_count_value' not in entry or cell_count.value == entry['source_count_value']:
                cell_count.value = len(entry['documents'])
                entry['source_count_value'] = cell_count.value
        # New managed source pointer; legacy document links remain untouched.
        col = _column(target, 'מקורות סנכרון')
        if first:
            cell = target.cell(row, col)
            if cell.value is None and cell.hyperlink is None:
                cell.value = 'פתיחת המקורות'
                cell.hyperlink = f'#{quote_sheetname(SOURCES)}!A{first}'
            if entry.get('owned_source_fields'):
                source_cell = target.cell(row, _column(target, COLUMN_HEADERS_HE['source_files']))
                if source_cell.hyperlink is None:
                    source_cell.hyperlink = f'#{quote_sheetname(SOURCES)}!A{first}'


def write_index(wb, entries):
    full = INDEX in wb and wb[INDEX]['D1'].value == 'Full report layout v1'
    if full:
        from src.report_layout import _apply_primary_layout
        for month in donation_sheets(wb):
            archive_col = headers(month).get(ARCHIVE)
            skip = {r for r in range(2, month.max_row+1) if archive_col and month.cell(r, archive_col).value}
            _apply_primary_layout(month, skip_rows=skip)
    if INDEX in wb.sheetnames:
        wb.remove(wb[INDEX])
    ws = wb.create_sheet(INDEX)
    ws.append(['Donation ID', 'Publication v1'])
    for key, entry in sorted(entries.items()):
        ws.append([key, json.dumps(entry, sort_keys=True, ensure_ascii=True)])
    ws['C1'] = 'Managed summary v1'
    ws['C2'] = json.dumps(summary_cells(wb[SUMMARY]), ensure_ascii=True, default=str)
    ws.sheet_state = 'veryHidden'
    if full:
        ws['D1'] = 'Full report layout v1'


def summary_cells(ws):
    return [[cell.coordinate, cell.value] for row in ws for cell in row if cell.value is not None]


def write_totals(wb, rows, had_index, *, preserve_formulas=False):
    """Numbers come from current row values, including human corrections."""
    if not had_index:
        labels = set(METRIC_LABELS.values())
        for ws in list(wb):
            if ws.title == SUMMARY or any(ws.cell(r, 1).value in labels for r in range(1, ws.max_row + 1)):
                if ws in [pair[0] for pair in rows.values()]:
                    continue
                if ws.title == SUMMARY:
                    archive = wb.copy_worksheet(ws)
                    archive.title = 'סיכום קודם'
                    archive.sheet_state = 'hidden'
                else:
                    ws.sheet_state = 'hidden'
    ws = wb[SUMMARY] if SUMMARY in wb.sheetnames else wb.create_sheet(SUMMARY)
    if had_index and wb[INDEX]['C1'].value == 'Managed summary v1':
        previous = json.loads(wb[INDEX]['C2'].value)
        if summary_cells(ws) != previous:
            archive = wb.copy_worksheet(ws)
            archive.title = 'סיכום ידני קודם'
            archive.sheet_state = 'hidden'
    ws.delete_rows(1, ws.max_row)
    ws.append(['תקופה', 'תרומות', 'סכום', 'סכומים לבדיקה'])
    monthly = defaultdict(lambda: [0, Decimal('0'), 0])
    formula_months, references = set(), defaultdict(list)
    for target, row in rows.values():
        meta = metadata(target, row)
        date = _donation_date(target.cell(row, headers(target)[COLUMN_HEADERS_HE['receipt_date']]))
        bucket = date.strftime('%Y-%m') if date else 'תאריך לבדיקה'
        amount_cell = target.cell(row, headers(target)[COLUMN_HEADERS_HE['amount']])
        references[bucket].append(f'{quote_sheetname(target.title)}!{amount_cell.coordinate}')
        if amount_cell.data_type == 'f':
            if not preserve_formulas:
                raise ValueError('Formula donation amount requires an explicit value before sync')
            formula_months.add(bucket)
        try:
            amount = Decimal(meta.amount.replace(',', '').replace('₪', '').strip())
            if not amount.is_finite() or amount_cell.data_type == 'b':
                amount = None
        except InvalidOperation:
            amount = None
        totals = monthly[bucket]
        totals[0] += 1
        totals[1] += amount or Decimal('0')
        totals[2] += amount is None
    for label, totals in sorted(monthly.items()):
        amount = '=SUM(' + ','.join(references[label]) + ')' if label in formula_months else float(totals[1])
        ws.append([label, totals[0], amount, totals[2]])
    annual_amount = ('=SUM(' + ','.join(ref for refs in references.values() for ref in refs) + ')'
                     if formula_months else float(sum((v[1] for v in monthly.values()), Decimal('0'))))
    ws.append(['שנתי', len(rows), annual_amount,
               sum(v[2] for v in monthly.values())])
    ws.append(['הערה', 'הסיכום מחושב משורות התרומות בפועל, כולל תיקונים ידניים; סכומים חסרים אינם נחשבים אפס מאומת.'])
    ws.append(['ביקורת', 'סיכומים קודמים נשמרו בגיליונות מוסתרים; המקורות והביקורת ההיסטורית נשמרו.'])
    ws.sheet_view.rightToLeft = True
    ws.column_dimensions['A'].width = 25
    ws.column_dimensions['B'].width = 90
    ws.sheet_state = 'visible'


def signature(wb):
    from openpyxl.worksheet.formula import ArrayFormula

    styles = {}

    def value(cell):
        raw = cell.value
        return (raw.ref, raw.text) if isinstance(raw, ArrayFormula) else raw

    def style(cell):
        key = tuple(cell._style) if cell.has_style else None
        if key not in styles:
            styles[key] = (cell.number_format, str(cell.font), str(cell.fill),
                           str(cell.border), str(cell.alignment), str(cell.protection))
        return styles[key]

    return [(ws.title, ws.sheet_state, [(c.coordinate, value(c), c.data_type,
                  c.hyperlink.target if c.hyperlink else None,
                  c.hyperlink.location if c.hyperlink else None,
                  (c.comment.text, c.comment.author) if c.comment else None, style(c))
                  for row in ws for c in row if c.value not in (None, '') or c.comment or c.hyperlink],
                 ws.sheet_view.rightToLeft, ws.freeze_panes)
                 for ws in wb]


def interactive_signature(wb):
    from src.annual_excel_summary import MARKER
    return [(ws.title, ws.auto_filter.ref, tuple(sorted((key, dim.hidden) for key, dim in ws.column_dimensions.items())),
             tuple((item.type, str(item.sqref), item.formula1) for item in ws.data_validations.dataValidation),
             wb.calculation.calcMode, wb.calculation.fullCalcOnLoad)
            for ws in wb if ws['A1'].value == MARKER or SORT_ROW in [cell.value for cell in ws[1]]]


def replace_verified(path, before, wb, *, backup_dir=None):
    report_stage('PUBLISH')
    path.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = safe_path(backup_dir) if backup_dir is not None else path.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.sync-', dir=staging_parent) as folder:
        candidate = Path(folder) / '.candidate.xlsx'
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            wb.save(candidate)
        from src.excel_compat import restore
        restore(candidate, getattr(wb, '_sync_person_metadata', None))
        checked = load(candidate)
        try:
            if signature(checked) != signature(wb) or interactive_signature(checked) != interactive_signature(wb):
                raise ValueError('Workbook round-trip verification failed')
        finally:
            checked.close()
        if (path.read_bytes() if path.exists() else None) != before:
            raise ValueError('Workbook changed during sync; close Excel before retry')
        if before is not None:
            emit('PUBLISH', 'backup_begin')
            folder = safe_path(backup_dir) if backup_dir is not None else path.parent
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder / ('.backup-' + digest(before) + '.xlsx')
            if not backup.exists():
                with backup.open('xb') as stream:
                    stream.write(before)
                    stream.flush()
                    os.fsync(stream.fileno())
            if backup.read_bytes() != before:
                raise ValueError('Backup verification failed')
        # The index is committed in precisely the same atomic replacement as rows.
        emit('PUBLISH', 'commit_begin')
        atomic_bytes(path, candidate.read_bytes())
        emit('PUBLISH', 'verified')
