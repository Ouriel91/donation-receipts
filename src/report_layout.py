"""Existing 13-field layout helper reused by the Windows sync writer."""
from copy import copy
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import json
from pathlib import Path

PRIMARY_COLUMNS = [
    'account', 'file_name', 'organization_name', 'registration_number',
    'receipt_number', 'tax_report_number', 'receipt_date', 'amount', 'donor_name',
    'section46_status', 'tax_reporting_status', 'severity', 'notes',
]

COLUMNS = PRIMARY_COLUMNS + ['donor_id', 'donor_match', 'extraction_status']

SOURCE_COLUMNS = ['source_count', 'source_files']

REPORT_COLUMNS = COLUMNS + ['tax_provenance', 'extracted_organization_name', 'organization_identity_provenance'] + SOURCE_COLUMNS

SOURCE_SHEET = 'קובצי מקור'

COLUMN_HEADERS_HE: dict[str, str] = {
    "account": "חשבון",
    "file_name": "שם קובץ",
    "organization_name": "שם העמותה",
    "registration_number": "מספר עמותה",
    "receipt_number": "מספר קבלה",
    "tax_report_number": "מספר אישור דיווח",
    "receipt_date": "תאריך",
    "amount": "סכום",
    "donor_name": 'שם התורם',
    "donor_id": 'ת"ז תורם שזוהתה',
    "donor_match": "התאמת תורם",
    "severity": "סטטוס הקבלה",
    "extraction_status": "סטטוס",
    "notes": "הערות",
    "section46_status": "סעיף 46 בתאריך התרומה",
    "tax_reporting_status": "מצב דיווח לרשות המסים",
    "tax_provenance": "ראיות סעיף 46",
    "extracted_organization_name": "שם עמותה שחולץ — מקור",
    "organization_identity_provenance": "ראיות התאמת מספר עמותה",
    "source_count": "מספר קובצי מקור",
    "source_files": "כל קובצי המקור",
}

_DISPLAY_MARKER = 'donation-receipts:primary:'

_FIELD_ALIASES = {
    'organization_name': ('שם העמותה', 'שם עמותה'),
    'donor_name': ('שם התורם', 'שם תורם שזוהה', 'שם תורם'),
    'severity': ('סטטוס הקבלה', 'חומרה'),
    'section46_status': ('סעיף 46 בתאריך התרומה', 'Section 46 on donation date (cached)'),
}

def _report_field_columns(ws):
    columns = {}
    for field in REPORT_COLUMNS:
        for label in _FIELD_ALIASES.get(field, (COLUMN_HEADERS_HE[field],)):
            matches = [c.column for c in ws[1] if c.value == label
                       and not (c.comment and c.comment.text.startswith(_DISPLAY_MARKER))]
            if len(matches) > 1:
                raise ValueError('Ambiguous original report fields; no overwrite')
            if matches:
                columns[field] = matches[0]
                break
    return columns

def _apply_primary_layout(ws, *, skip_rows=()):
    """13 visible fields on the month itself, without moving original addresses."""
    from openpyxl.comments import Comment
    archive_col = next((c.column for c in ws[1] if c.value == 'Historical duplicate archive v1'),None)
    skip_rows=set(skip_rows) | {r for r in range(2,ws.max_row+1) if archive_col and ws.cell(r,archive_col).value}
    skip_rows |= {r for (title,r) in getattr(ws.parent,'_annual_archive_owners',{}) if title==ws.title}
    columns = _report_field_columns(ws)
    if any(columns.get(field) != i for i, field in enumerate(PRIMARY_COLUMNS[:9], 1)):
        raise ValueError('Existing leading report columns differ; original preserved')
    status_col = columns.get('section46_status')
    # Published blanks can be human corrections. The enrichment writer
    # separately checks the managed baseline before filling a status.
    published_layout = any(c.value == 'Published row position v1' for c in ws[1])
    if status_col and not published_layout:
        for row in range(2, ws.max_row+1):
            cell = ws.cell(row, status_col)
            if row not in skip_rows and cell.value in (None, '') and cell.comment is None:
                cell.value = 'לא ידוע / דורש בירור'
    native = all(columns.get(field) == i for i, field in enumerate(PRIMARY_COLUMNS, 1))
    visible = set(range(1, 14)) if native else set(range(1, 10))
    if not native:
        for field in PRIMARY_COLUMNS[9:]:
            marked = [c for c in ws[1] if c.comment and c.comment.text.splitlines()[0] == _DISPLAY_MARKER + field]
            if len(marked) > 1:
                raise ValueError('Ambiguous primary display field')
            header = marked[0] if marked else ws.cell(1, ws.max_column + 1, COLUMN_HEADERS_HE[field])
            if not marked:
                header.comment = Comment(_DISPLAY_MARKER + field, 'Donation Receipts')
            header.font = Font(bold=True, size=11)
            origin = columns.get(field)
            for row in range(2, ws.max_row + 1):
                target = ws.cell(row, header.column)
                if row in skip_rows:
                    continue
                # Refresh only our exact previous projection, never arbitrary
                # formulas or literal display overrides entered by a person.
                previous_ref = ws.cell(row, origin).coordinate if origin else None
                old_projection = f'=IF({previous_ref}="","",{previous_ref})' if previous_ref else None
                generated = False
                if target.comment and target.comment.text.startswith('donation-receipts:display:v1\n'):
                    try:
                        generated = target.value == json.loads(target.comment.text.split('\n', 1)[1])
                    except (ValueError, TypeError):
                        pass
                    if field == 'section46_status' and not generated:
                        continue
                elif target.comment:
                    continue
                if field in ('section46_status','severity'):
                    old_unknown = f'=IF({previous_ref}="","לא ידוע / דורש בירור",{previous_ref})' if previous_ref else None
                    if target.value not in (None, '', old_projection, old_unknown) and not generated:
                        continue
                    value = ws.cell(row, origin).value if origin else None
                    if value is None or value == '' or (origin and ws.cell(row, origin).data_type == 'f'):
                        # An unevaluated manual source formula is kept intact in
                        # its original technical cell; do not invent its result.
                        value = 'לא ידוע / דורש בירור' if field=='section46_status' else 'דורשת בדיקה'
                    target.value = value; target.data_type = 's'
                    target.comment = Comment('donation-receipts:display:v1\n' + json.dumps(value, ensure_ascii=False), 'Donation Receipts')
                    font = copy(target.font); font.sz = 11; target.font = font
                    continue
                if target.value not in (None, '') and target.value != old_projection:
                    continue
                if origin:
                    original = ws.cell(row, origin)
                    ref = original.coordinate
                    fallback = 'לא ידוע / דורש בירור' if field == 'section46_status' else ''
                    target.value = f'=IF({ref}="","{fallback}",{ref})'
                    target._style = copy(original._style)
                elif field in ('section46_status', 'tax_reporting_status'):
                    target.value = 'לא ידוע'
                font = copy(target.font); font.sz = 11; target.font = font
            visible.add(header.column)
    for col in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].hidden = col not in visible
    for row in range(1, ws.max_row + 1):
        cell = ws.cell(row, columns['file_name'])
        font = copy(cell.font); font.sz = 11; cell.font = font
    for cells in ws.iter_rows():
        for cell in cells:
            font = copy(cell.font); font.sz = 11; cell.font = font
    ws.sheet_view.rightToLeft = True
    ws.auto_filter.ref = ws.dimensions
    return visible

def _has_primary_layout(ws):
    visible = [c.value for c in ws[1] if not ws.column_dimensions[c.column_letter].hidden]
    expected = [COLUMN_HEADERS_HE[field] for field in PRIMARY_COLUMNS]
    aliases = [_FIELD_ALIASES.get(field, (label,)) for field, label in zip(PRIMARY_COLUMNS, expected)]
    return len(visible) == 13 and all(value in allowed for value, allowed in zip(visible, aliases))


PDF_AUDIT = '_PDF source audit'
PDF_REVIEW = 'מקור PDF לא מאומת — נדרשת בדיקה'


def _pdf_audit(wb):
    """Private in-workbook evidence, never a publication checkpoint or user report."""
    if hasattr(wb, '_pdf_source_audit'):
        return wb._pdf_source_audit
    records = {}
    if PDF_AUDIT in wb:
        ws = wb[PDF_AUDIT]
        if [ws.cell(1, c).value for c in (1, 2)] != ['Location', 'PDF source audit v1']:
            from src.sync_state import SyncError
            raise SyncError('pdf_source_audit_invalid', 'Unrecognized private source audit; no publication')
        for row in range(2, ws.max_row+1):
            key, raw = ws.cell(row, 1).value, ws.cell(row, 2).value
            if not key or key in records:
                from src.sync_state import SyncError
                raise SyncError('pdf_source_audit_invalid', 'Duplicate source audit identity; no publication')
            try:
                payload = json.loads(raw)
                valid = (isinstance(payload, dict) and type(payload.get('pending')) is bool
                         and {'original_name','original_uri','decision'} <= payload.keys()
                         and isinstance(payload.get('history', []), list)
                         and (not payload['pending'] or {'display_name','review_comment','previous_comment'} <= payload.keys()))
                if not valid:
                    raise ValueError()
            except (ValueError, TypeError):
                from src.sync_state import SyncError
                raise SyncError('pdf_source_audit_invalid', 'Invalid private source evidence; no publication') from None
            records[key] = (row, payload)
    wb._pdf_source_audit = records
    return records


def source_link_input(wb, cell):
    """Retain the unresolved original target on retries, even after disabling it."""
    key = f'{cell.parent.title}!{cell.coordinate}'
    record = _pdf_audit(wb).get(key, (None, {}))[1]
    target = (cell.hyperlink.target or cell.hyperlink.location) if cell.hyperlink else None
    if not target and record.get('pending'):
        name = record['original_name'] if cell.value == record.get('display_name') else cell.value
        return name, record['original_uri']
    return cell.value, target


def _record_pdf_audit(wb, cell, payload):
    records = _pdf_audit(wb)
    key = f'{cell.parent.title}!{cell.coordinate}'
    previous = records.get(key)
    if PDF_AUDIT not in wb:
        ws = wb.create_sheet(PDF_AUDIT)
        ws.append(['Location', 'PDF source audit v1'])
        ws.sheet_state = 'hidden'
    ws = wb[PDF_AUDIT]
    if previous:
        core = {k:v for k,v in previous[1].items() if k != 'history'}
        if core == payload:
            return
        payload = {**payload, 'history':[*previous[1].get('history', []), core]}
    row = previous[0] if previous else ws.max_row+1
    ws.cell(row, 1, key).data_type = 's'
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    if len(raw) > 32767:
        from src.sync_state import SyncError
        raise SyncError('pdf_source_audit_capacity', 'Source evidence cannot be preserved in the workbook; no publication')
    ws.cell(row, 2, raw).data_type = 's'
    records[key] = (row, payload)


def _pdf_semantic_match(old, paths, state, account, config, owners, owner):
    """Existing Dedup must prove the ENTIRE candidate set, not just a basename."""
    from dataclasses import asdict
    from src import receipt_summary
    from src.sync_workbook import receipt_source_evidence
    from src.sync_state import digest, effective_metadata, donation_date, SyncError
    from src.receipt_deduplication import consolidate_receipts
    from src.private_runtime import safe_path
    try:
        old = safe_path(old)
        before = digest(old.read_bytes())
    except (OSError, ValueError):
        return None
    if owners.get(before, set()) - {owner}:
        return None
    reference_date = donation_date(effective_metadata(state['documents'][digest(paths[0].read_bytes())])) if paths else None
    if reference_date is None:
        return None
    try:
        old_meta = effective_metadata(state['documents'][before]) if before in state['documents'] else effective_metadata({
            'metadata': asdict(receipt_summary._extract_row(old, account, config, reference_date.year))})
        scope = receipt_source_evidence(old, old_meta)
    except (receipt_summary.PdfExtractionError, OSError, ValueError):
        return None
    if len(scope['receipt_numbers']) != 1 or scope['multiple_amounts'] or not donation_date(old_meta):
        return None
    matches = []
    metas = []
    for path in paths:
        key = digest(path.read_bytes())
        if key not in state['documents']:
            raise SyncError('indexed_pdf_changed', 'Indexed source changed during semantic verification; no publication')
        meta = effective_metadata(state['documents'][key])
        evidence = receipt_source_evidence(path, meta)
        if evidence['sha256'] != key:
            raise SyncError('indexed_pdf_changed', 'Indexed source changed during semantic verification; no publication')
        if len(evidence['receipt_numbers']) != 1 or evidence['multiple_amounts']:
            continue
        group = consolidate_receipts([(Path('old'), old_meta), (Path('active'), meta)])
        if len(group) == 1 and len(group[0].sources) == 2 and group[0].suspected_group is None:
            matches.append(path); metas.append(meta)
    if digest(old.read_bytes()) != before:
        raise SyncError('pdf_source_changed', 'Source changed during semantic verification; no publication')
    all_records = [(Path('old'), old_meta)] + [(Path(str(i)), meta) for i, meta in enumerate(metas)]
    if matches and len(consolidate_receipts(all_records)) == 1:
        return matches[0].resolve(strict=True)
    return None


def _resolve_pdf_link(cell, paths, state, receipts_dir, account, workbook_path, config, owners, owner, *, scoped=False):
    from src.workbook_maintenance import resolve_encoding_target
    from src.sync_workbook import _external_document
    from src.sync_state import digest, SyncError
    from types import SimpleNamespace
    name, target = source_link_input(cell.parent.parent, cell)
    # Prefer an existing reversibly recovered original name among cataloged
    # byte-identical aliases. Never rename files or invent a display filename.
    def preferred(candidates):
        for candidate in candidates:
            repaired,why=resolve_encoding_target(candidate.as_uri(),candidate.parent)
            if repaired is not None and repaired.name!=candidate.name and repaired in candidates:
                return repaired.resolve(strict=True)
        return candidates[0].resolve(strict=True)
    try:
        old, reason = resolve_encoding_target(target, workbook_path.parent, display_name=name, permitted_root=(Path(receipts_dir)/account).absolute() if scoped else None) if target and workbook_path else (None, None)
    except (OSError, ValueError):
        return None, 'conflict', 'unreadable_source_reference'
    if reason == 'ambiguous_existing_names':
        return None, 'conflict', 'ambiguous_encoding_candidates'
    if (target and target.startswith('#')) or reason in ('not_local_file', 'uri_query_or_fragment', 'source_outside_account'):
        return None, 'conflict', 'invalid_local_reference'
    if old is not None:
        try:
            old_hash = digest(old.read_bytes())
        except OSError:
            return None, 'conflict', 'unreadable_source_reference'
        same = [p for p in paths if digest(p.read_bytes()) == old_hash]
        if same:
            return (next((p.resolve(strict=True) for p in same if p.resolve()==old.resolve()),None) or preferred(same)), 'verified', 'identical_content', {'original_sha256':old_hash}
        equivalent = _pdf_semantic_match(old, paths, state, account, config, owners, owner) if paths else None
        return ((equivalent, 'verified', 'dedup_semantic_copy', {'original_sha256':old_hash}) if equivalent else
                (None, 'conflict', 'different_unproven_content', {'original_sha256':old_hash}))
    probe = SimpleNamespace(hyperlink=SimpleNamespace(target=target, location=None))
    key = _external_document(probe, workbook_path, Path(receipts_dir)/account, state['documents'], scoped=scoped) if target and workbook_path else None
    candidates = [p for p in paths if digest(p.read_bytes()) == key] if key else [p for p in paths if p.name == name]
    if not candidates and len(paths) == 1:
        candidates = paths
    if candidates and len({digest(p.read_bytes()) for p in candidates}) == 1:
        return preferred(candidates), 'verified', 'proven_catalog_binding'
    return None, 'missing' if old is None else 'conflict', 'no_unique_source_binding'


def _local_pdf_address(pdf):
    """Excel local-file relationships use the verified native Unicode path.

    Do not percent-encode UTF-8 into file:/// here: Excel's local-link decoding
    may interpret those bytes as legacy text. openpyxl writes Unicode XML itself.
    """
    from src.sync_state import SyncError
    path=Path(pdf).resolve(strict=True)
    if path.suffix.lower()!='.pdf' or not path.is_file():
        raise SyncError('pdf_source_changed','Verified PDF is no longer a file; canonical preserved')
    return str(path)


def _write_pdf_link(wb, cell, owner, result, rows, entries, counts):
    from openpyxl.comments import Comment
    from src.logger import metric
    pdf, status, reason = result[:3]
    evidence = result[3] if len(result) > 3 else {}
    original_name, original_uri = source_link_input(wb, cell)
    prior = _pdf_audit(wb).get(f'{cell.parent.title}!{cell.coordinate}', (None, {}))[1]
    address=_local_pdf_address(pdf) if pdf is not None else None
    changed = pdf is not None and (cell.value != pdf.name or not cell.hyperlink or cell.hyperlink.target != address)
    if pdf is not None:
        if changed:
            counts['links_fixed'] += 1; metric('links_fixed')
        if changed or prior.get('pending'):
            payload = {'owner':owner, 'original_name':original_name, 'original_uri':original_uri,
                       'pending':False, 'decision':reason, 'target':address, **evidence}
            from src.sync_state import digest
            payload['target_sha256'] = digest(pdf.read_bytes())
            _record_pdf_audit(wb, cell, payload)
        cell.value = pdf.name; cell.data_type = 's'; cell.hyperlink = address
        if prior.get('pending') and cell.comment and cell.comment.text == prior.get('review_comment'):
            before = prior.get('previous_comment')
            cell.comment = Comment(before[0], before[1]) if before else None
    else:
        counter = 'links_not_found' if status == 'missing' else 'links_conflicts'
        counts[counter] += 1; metric(counter)
        current_comment = [cell.comment.text, cell.comment.author] if cell.comment else None
        unchanged_review = prior.get('pending') and cell.comment and cell.comment.text == prior.get('review_comment')
        comment_before = prior.get('previous_comment') if unchanged_review else current_comment
        text = comment_before[0] if comment_before else ''
        if PDF_REVIEW not in text:
            text = (text+'\n' if text else '') + PDF_REVIEW + ' ['+reason+']'
        caption = str(original_name or 'PDF') + ' — מקור לא מאומת'
        payload = {'owner':owner, 'original_name':original_name, 'original_uri':original_uri,
                   'display_name':caption, 'pending':True, 'decision':reason,
                   'previous_comment':comment_before, 'review_comment':text, **evidence}
        _record_pdf_audit(wb, cell, payload)
        cell.value = caption; cell.data_type = 's'; cell.hyperlink = None
        cell.comment = Comment(text, comment_before[1] if comment_before else 'Donation Receipts')
    font = copy(cell.font); font.sz = 11; cell.font = font
    alignment = copy(cell.alignment); alignment.shrinkToFit = None; cell.alignment = alignment


def _pdf_review_notes(wb, rows, entries, selected):
    from src.sync_workbook import _column
    pending = {record['owner'] for _, record in _pdf_audit(wb).values() if record.get('pending') and record.get('owner')}
    for key in selected:
        ws, row = rows[key]
        cell = ws.cell(row, _column(ws, COLUMN_HEADERS_HE['notes']))
        snapshot = entries[key].get('source_review_note')
        if key in pending and not snapshot and cell.data_type != 'f':
            before = cell.value
            after = '; '.join(filter(None, (str(before or ''), PDF_REVIEW)))
            cell.value = after; cell.data_type = 's'
            entries[key]['source_review_note'] = {'before':before, 'after':after}
        elif key not in pending and snapshot:
            if cell.value == snapshot['after']:
                cell.value = snapshot['before']
            entries[key].pop('source_review_note', None)



def apply_full_report(wb, rows, entries, state, receipts_dir, account, root, *, workbook_path=None, selected_ids=None, finalize=True, config=None, scoped=False, registry=None):
    """Apply existing presentation to the existing sync workbook, before publication."""
    from src.sync_state import document_paths, digest
    from src.sync_workbook import _column, ORIGINAL_NAME, SOURCES, _external_document, _row_fingerprint, set_archive_proof, ARCHIVE, headers
    from src.organization_cache import OrganizationCache
    cache = registry if registry is not None else OrganizationCache(root / 'organizations.json')
    from openpyxl.workbook.properties import CalcProperties
    wb.calculation = CalcProperties(calcMode='auto', fullCalcOnLoad=True)
    from collections import Counter, defaultdict
    from src.sync_state import SyncError
    selected = set(rows) if selected_ids is None else set(selected_ids)
    if not selected <= set(rows):
        raise SyncError('pdf_owner_unknown', 'Monthly source owner is not in publication rows; no publication')
    counts = Counter(links_fixed=0, links_not_found=0, links_conflicts=0)
    owners = defaultdict(set)
    locations = {}
    for key, entry in entries.items():
        for doc in entry['documents']:
            owners[doc].add(key)
        locations[(rows[key][0].title, rows[key][1])] = key
        for alias in entry.get('historical_aliases', []):
            locations[(alias['sheet'], alias['row'])] = key
    proven = {}
    def verified_pdf(cell, paths, owner):
        return _resolve_pdf_link(cell, paths, state, receipts_dir, account, workbook_path, config, owners, owner, scoped=scoped)

    for key, (ws, row) in rows.items():
        if key not in selected:
            continue
        entry = entries[key]
        columns = _report_field_columns(ws)
        paths = document_paths(receipts_dir, account, state, entry['documents'])
        if not paths:
            raise SyncError('indexed_pdf_unavailable', 'No verified indexed source remains for the donation; no publication')
        proven[key] = paths
        proven[(ws.title, row)] = paths
        if any(not p.resolve(strict=True).is_relative_to((Path(receipts_dir)/account).resolve()) for p in paths):
            raise SyncError('pdf_outside_project', 'Indexed source escapes active project; no publication')
        cell = ws.cell(row, columns['file_name'])
        representative = entry.get('representative_document')
        audit = _pdf_audit(wb).get(f'{ws.title}!{cell.coordinate}', (None, {}))[1]
        current = cell.hyperlink.target if cell.hyperlink else None
        previous_target = audit.get('target')
        # An exact native address to an owned, SHA-verified source can be
        # intentional even if the caption still names the former representative.
        manual_choice = (representative and current and previous_target
                         and current != previous_target
                         and any(current == str(path.resolve()) for path in paths))
        if not manual_choice:
            result = verified_pdf(cell, paths, key)
            managed_link = (
                current == previous_target if audit else
                (entry.get('owned_source_fields') and result[0] is not None
                 and current == result[0].resolve().as_uri())
            )
            reviewed = entry.get('reviewed_oc_pair')
            if (reviewed and not audit and current and result[0] is not None
                    and (current == result[0].resolve().as_uri()
                         or (Path(current).is_absolute() and Path(current).resolve() == result[0].resolve()))
                    and digest(result[0].read_bytes()) == reviewed['original_sha']):
                managed_link = True
            product_review_comment = audit.get('pending') and cell.comment and cell.comment.text == audit.get('review_comment')
            allow_recovery = reviewed and current is None and product_review_comment
            if (representative and ((managed_link and result[0] is not None) or allow_recovery)
                    and (cell.comment is None or product_review_comment) and cell.data_type != 'f'):
                preferred = [path for path in document_paths(receipts_dir, account, state, [representative])
                             if path in paths]
                if reviewed:
                    preferred = [path for path in preferred if path.name == reviewed['copy_name']]
                if preferred and (result[0] is None
                                  or (reviewed and preferred[0].resolve() != result[0].resolve())
                                  or (not reviewed and digest(result[0].read_bytes()) != representative)):
                    result = (preferred[0], 'verified', 'dedup_representative')
            _write_pdf_link(wb, cell, key, result, rows, entries, counts)
        col = _column(ws, COLUMN_HEADERS_HE['tax_reporting_status'])
        if ws.cell(row,col).value is None:
            ws.cell(row,col,'לא ידוע')
        original = entry.get('original_extracted_name')
        if original is not None:
            out = ws.cell(row, _column(ws, COLUMN_HEADERS_HE['extracted_organization_name']))
            if out.value is None:
                out.value = original; out.data_type = 's'
        number = str(ws.cell(row, columns['registration_number']).value or '')
        identity = cache.official_identity(number) if number.isascii() and number.isdigit() and len(number)==9 else None
        if identity:
            out = ws.cell(row, _column(ws, COLUMN_HEADERS_HE['organization_identity_provenance']))
            if out.value is None:
                out.value = json.dumps(identity, ensure_ascii=False); out.data_type = 's'
    # Old source-sheet pointers to retained duplicate rows remain valid; the
    # primary publication owns their union of proven PDF sources.
    for key, entry in entries.items():
        if key not in selected:
            continue
        for alias in entry.get('historical_aliases', []):
            paths = proven[(rows[key][0].title, rows[key][1])]
            proven[(alias['sheet'], alias['row'])] = paths
            # Retained audit rows are preserved verbatim, never repaired or
            # re-signed as a prerequisite for the active report.
    for ws in {rows[key][0] for key in selected}:
        archive_col = headers(ws).get(ARCHIVE)
        skip = {r for r in range(2, ws.max_row+1) if archive_col and ws.cell(r, archive_col).value}
        _apply_primary_layout(ws, skip_rows=skip)
    if SOURCE_SHEET in wb:
        for row in wb[SOURCE_SHEET].iter_rows(min_row=2):
            link = row[0].hyperlink
            reference = (link.target or link.location) if link else row[0].value
            owner = None
            if isinstance(reference, str) and '!' in reference:
                title, coordinate = reference.lstrip('#').rsplit('!', 1)
                title = title.strip("'").replace("''", "'")
                number = coordinate.replace('$','').lstrip('A')
                owner = locations.get((title, int(number))) if number.isdigit() else None
            if owner not in selected and not (finalize and owner is None):
                continue
            result = verified_pdf(row[2], proven[owner], owner) if owner else verified_pdf(row[2], [], None)
            _write_pdf_link(wb, row[2], owner, result, rows, entries, counts)
    if SOURCES in wb:
        for row in wb[SOURCES].iter_rows(min_row=2):
            owner = row[0].value
            if owner not in selected and not (finalize and owner not in rows):
                continue
            if owner not in rows:
                result = verified_pdf(row[2], [], None)
            elif row[1].value not in entries[owner]['documents']:
                result = (None, 'conflict', 'ledger_document_not_owned')
            else:
                paths = document_paths(receipts_dir, account, state, [row[1].value])
                if not paths:
                    raise SyncError('indexed_pdf_changed', 'Indexed source disappeared during monthly processing; no publication')
                result = verified_pdf(row[2], paths, owner)
            _write_pdf_link(wb, row[2], owner if owner in rows else None, result, rows, entries, counts)
    _pdf_review_notes(wb, rows, entries, selected if not finalize else set(rows))
    return dict(counts)
