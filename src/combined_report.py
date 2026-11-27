"""Build a user-wide annual view from the source publications and verified PDFs."""
from collections import defaultdict
from contextlib import closing
from copy import copy
from dataclasses import dataclass, replace
import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from src.annual_excel_summary import _column as display_column, write_summary
from src.attachment_saver import MONTH_NAMES
from src.delta_sync import all_rows, managed_rows
from src.private_runtime import new_bytes, operational_lock, safe_path
from src.receipt_deduplication import Donation, _identity, _possibly_same, consolidate_receipts
from src.receipt_summary import COLUMN_HEADERS_HE, SOURCE_SHEET, _MONTH_SHEET_NAMES, _write_month_sheet, refresh_published_warnings
from src.report_layout import _FIELD_ALIASES, _local_pdf_address, _report_field_columns
from src.sync_state import digest, document_paths, donation_date
from src.receipt_metadata_extractor import ReceiptMetadata
from src.sync_workbook import (ID_HEADER, INDEX, LIVE_HEADERS, _column, load, metadata,
                               replace_verified, signature, write_index, write_totals)
from src.user_scope import annual_report, identifier
from src.workbook_maintenance import resolve_encoding_target

_PROVENANCE = '_Combined provenance'
_OMITTED_FIELDS = ('donor_id', 'donor_match', 'severity', 'extraction_status', 'source_count')
_OMITTED_HEADERS = {label for field in _OMITTED_FIELDS
                    for label in (COLUMN_HEADERS_HE[field], *_FIELD_ALIASES.get(field, ()))}
_FIELDS = ('organization_name', 'registration_number', 'receipt_number', 'receipt_date',
           'amount', 'tax_report_number', 'donor_name', 'donor_id')


@dataclass
class PublishedDonation:
    account: str
    publication: str
    meta: ReceiptMetadata
    status: str
    protected: set
    hashes: frozenset
    paths: tuple[Path, ...]
    distinct: bool


def _catalog(scope, account):
    documents = {}
    exclusions = {}
    for flow in ('delta', 'sync'):
        path = safe_path(scope.runtime / f'{flow}-{account}.json')
        if not path.exists():
            continue
        state = json.loads(path.read_bytes())
        if account == 'manual' and flow == 'sync':
            exclusions = state.get('manual_exclusion_history', {})
        for key, entry in state['documents'].items():
            if key in documents:
                entry = {**documents[key], **entry,
                         'paths': list(dict.fromkeys(documents[key]['paths'] + entry['paths']))}
            documents[key] = entry
    return {'documents': documents, 'manual_exclusion_history': exclusions}


def _read(scope, account, year):
    path = annual_report(scope.reports, account, year)
    if not path.is_file():
        return [], [(account, '', 'source_report_missing', ())]
    catalog = _catalog(scope, account)
    base = safe_path(scope.receipts / account)
    from src.manual_exclusion import excluded_publications
    excluded = excluded_publications(catalog, year) if account == 'manual' else set()
    accepted, review = [], []
    with closing(load(path)) as wb:
        if INDEX in wb and wb[INDEX]['D1'].value == 'Full report layout v1':
            wb._annual_archive_owners = {}
        entries, managed = managed_rows(wb)
        positions = {(ws.title, row): key for key, (ws, row) in managed.items()}
        for coord, (ws, row) in all_rows(wb).items():
            key = positions.get(coord)
            if key in excluded:
                continue
            entry = entries.get(key, {})
            meta = metadata(ws, row)
            cols = _report_field_columns(ws)
            physical = {}
            for sha in entry.get('documents', ()):
                for pdf in document_paths(scope.receipts, account, catalog, [sha]):
                    physical[pdf] = sha
            if not key and 'file_name' in cols:
                link = ws.cell(row, cols['file_name']).hyperlink
                if link and link.target:
                    pdf, _ = resolve_encoding_target(link.target, path.parent, permitted_root=base)
                    if pdf:
                        physical[pdf] = digest(pdf.read_bytes())
            publication = key or f'legacy:{ws.title}:{row}'
            if not physical or not donation_date(meta) or donation_date(meta).year != year:
                review.append((account, publication, 'unverified_source_or_date', tuple(physical)))
                continue
            amount = ws.cell(row, cols['amount']) if 'amount' in cols else None
            if amount is not None and amount.data_type in ('b', 'f'):
                review.append((account, publication, 'manual_amount_review', tuple(physical)))
                continue
            protected = {field for field in _FIELDS if field in cols and (
                ws.cell(row, cols[field]).comment or
                (COLUMN_HEADERS_HE[field] in entry.get('managed', {}) and
                 ws.cell(row, cols[field]).value != entry['managed'][COLUMN_HEADERS_HE[field]])
            )}
            if entry.get('identifier_approvals'):
                protected.update(('receipt_number', 'tax_report_number', 'amount'))
            status_col = display_column(ws, 'section46_status') or cols.get('section46_status')
            status = str(ws.cell(row, status_col).value or '') if status_col else ''
            accepted.append(PublishedDonation(account, publication, meta, status, protected,
                                               frozenset(physical.values()), tuple(sorted(physical, key=lambda p: (physical[p] != entry.get('representative_document'), p))),
                                               any(catalog['documents'].get(sha, {}).get('review_decision', {}).get('action') == 'distinct'
                                                   for sha in physical.values())))
    return accepted, review


def _components(items):
    neighbors = [set() for _ in items]
    keys = [_identity(item.meta) for item in items]
    for i, first in enumerate(items):
        for j in range(i + 1, len(items)):
            second = items[j]
            if first.account == second.account:
                continue
            exact = first.hashes & second.hashes
            possible = not (first.distinct or second.distinct) and _possibly_same(keys[i], keys[j])
            if exact or possible:
                neighbors[i].add(j)
                neighbors[j].add(i)
    seen = set()
    for start in range(len(items)):
        if start in seen:
            continue
        component, pending = [], [start]
        while pending:
            i = pending.pop()
            if i in seen:
                continue
            seen.add(i)
            component.append(i)
            pending.extend(sorted(neighbors[i] - seen, reverse=True))
        yield [items[i] for i in sorted(component)]


def _merge(items):
    primary = items[0]
    if len(items) == 1:
        return replace(primary.meta), primary.status
    if len({item.account for item in items}) != len(items):
        return None
    exact = set.intersection(*(set(item.hashes) for item in items))
    records = [(Path(str(i)), item.meta) for i, item in enumerate(items)]
    copies = consolidate_receipts(records)
    if not exact and len(copies) != 1:
        return None
    meta = replace(copies[0].metadata if len(copies) == 1 else primary.meta)
    for field in _FIELDS:
        values = {getattr(item.meta, field) for item in items if field in item.protected}
        if len(values) > 1:
            return None
        if values:
            setattr(meta, field, values.pop())
        elif exact and field in {'receipt_date', 'amount', 'registration_number', 'receipt_number'}:
            values = {getattr(item.meta, field) for item in items if getattr(item.meta, field)}
            if len(values) > 1 and len(copies) != 1:
                return None
            if values and len(copies) != 1:
                setattr(meta, field, values.pop())
    statuses = {item.status for item in items if item.status}
    decided = {status for status in statuses if status in {'מאושר', 'לא מאושר'}}
    status = next(iter(decided)) if len(decided) == 1 else next(iter(statuses)) if len(statuses) == 1 else 'לא ידוע / דורש בירור'
    return meta, status


def _previous(path):
    if not path.exists():
        return {}, None, None
    raw = path.read_bytes()
    with closing(load(path)) as wb:
        if _PROVENANCE not in wb:
            raise ValueError('Existing combined workbook is not a generated publication')
        previous = {}
        for key, account, source, _ in wb[_PROVENANCE].iter_rows(min_row=2, max_col=4, values_only=True):
            if key is not None:
                previous[(account, source)] = key
        return previous, raw, signature(wb)


def _simplify_month(ws):
    omitted = [cell.column for cell in ws[1] if cell.value in _OMITTED_HEADERS]
    retained_dimensions = [
        copy(ws.column_dimensions.get(cell.column_letter))
        for cell in ws[1] if cell.column not in omitted
    ]
    for column in reversed(omitted):
        ws.delete_cols(column)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.hyperlink:
                cell.hyperlink.ref = cell.coordinate
    ws.column_dimensions.clear()
    for column, dimension in enumerate(retained_dimensions, 1):
        if dimension is not None:
            letter = get_column_letter(column)
            dimension.index = letter
            dimension.min = dimension.max = column
            ws.column_dimensions[letter] = dimension
    # The annual summary uses this internal key to count indexed rows.
    ws.column_dimensions[get_column_letter(_column(ws, ID_HEADER))].hidden = True
    ws.auto_filter.ref = ws.dimensions


def _migrate_combined_name(path, year):
    """Keep the preceding publication and its IDs when changing its name."""
    legacy = safe_path(path.with_name(f'donation_summary_{year}.xlsx'))
    if not legacy.exists():
        return
    _, raw, _ = _previous(legacy)  # Verify generated provenance before moving.
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Both combined report names exist with different contents; no overwrite')
    else:
        new_bytes(path, raw)  # No-clobber publication; safe to resume after interruption.
    if path.read_bytes() != raw or legacy.read_bytes() != raw:
        raise ValueError('Combined report changed during rename; close Excel and retry')
    legacy.unlink()


def run_combined(scope, year):
    if not 1900 <= year <= 9998:
        raise ValueError('Invalid combined report year')
    scope.require()
    with operational_lock(scope.runtime), closing(Workbook()) as wb:
        path = annual_report(scope.reports, 'combined', year)
        _migrate_combined_name(path, year)
        previous, before, original = _previous(path)
        accounts = []
        for folder in scope.accounts.iterdir():
            if folder.name.startswith('.') or not folder.is_dir():
                continue
            account = identifier(folder.name)
            scope.require_account(account)
            accounts.append(account)
        items, queue = [], []
        for account in sorted(accounts, key=lambda value: (value != 'primary', value)) + ['manual']:
            rows, review = _read(scope, account, year)
            items.extend(rows)
            queue.extend(review)
        wb.remove(wb.active)
        source_sheet = wb.create_sheet(SOURCE_SHEET)
        source_sheet.append(['תרומה בדוח', 'חודש שמירת מקור', 'פתיחת קובץ מקור'])
        source_sheet.sheet_view.rightToLeft = True
        provenance = wb.create_sheet(_PROVENANCE)
        provenance.append(['Donation ID', 'Source account', 'Source publication', 'Source SHA-256'])
        provenance.sheet_state = 'veryHidden'
        months = defaultdict(list)
        used = set()
        for component in _components(items):
            merged = _merge(component)
            if merged is None:
                queue.extend((row.account, row.publication, 'ambiguous_cross_source_match', row.paths)
                             for row in component)
                # The earlier canonical source remains counted. Extra source
                # publications need a correction or stronger evidence first.
                groups = [([row], (replace(row.meta), row.status)) for row in component
                          if row.account == component[0].account]
            else:
                groups = [(component, merged)]
            for group, (meta, status) in groups:
                ids = {(row.account, row.publication) for row in group}
                known = {previous[source] for source in ids if source in previous}
                available = known - used
                payload = [scope.user, sorted(ids)]
                key = min(available) if available else 'd-' + digest(json.dumps(payload).encode())
                if key in used:
                    key = 'd-' + digest(json.dumps([*payload, 'split']).encode())
                used.add(key)
                for row in group:
                    provenance.append([key, row.account, row.publication, ','.join(sorted(row.hashes))])
                paths = tuple(dict.fromkeys(pdf for row in group for pdf in row.paths))
                if len(group) > 1 and len(paths) == 2 and not any(row.distinct or row.protected for row in group):
                    from src.sync_workbook import verified_copy_source
                    candidates = []
                    for pdf in paths:
                        row = next(row for row in group if pdf in row.paths)
                        sha = digest(pdf.read_bytes())
                        catalog = _catalog(scope, row.account)
                        candidates.append((sha, pdf, row.account, catalog['documents'].get(sha, {})))
                    preferred = verified_copy_source(candidates, _identity(meta))
                    if preferred is not None:
                        paths = (preferred,) + tuple(pdf for pdf in paths if pdf != preferred)
                meta.account = ', '.join(f'{account.upper()}-{scope.user.upper()}'
                                         for account in dict.fromkeys(row.account for row in group))
                refresh_published_warnings(meta, None, year, preserve_donor_notes=True,
                                           duplicate_resolved=all(row.distinct for row in group))
                meta.file_name = paths[0].name
                months[donation_date(meta).month].append((key, Donation(meta, paths), status))
        entries, rows = {}, {}
        for month, folder in sorted(MONTH_NAMES.items()):
            entries_for_month = months[month]
            _write_month_sheet(wb, folder, [item[1] for item in entries_for_month], source_sheet)
            ws = wb[_MONTH_SHEET_NAMES[folder]]
            id_col, section_col = _column(ws, ID_HEADER), _column(ws, LIVE_HEADERS[0])
            file_col = _report_field_columns(ws)['file_name']
            for index, (key, donation, status) in enumerate(entries_for_month, 2):
                ws.cell(index, id_col, key)
                ws.cell(index, section_col, status)
                ws.cell(index, file_col).hyperlink.target = _local_pdf_address(donation.sources[0])
                entries[key] = {'documents': sorted({digest(pdf.read_bytes()) for pdf in donation.sources})}
                rows[key] = (ws, index)
            _simplify_month(ws)
        for cell, pdf in zip((row[2] for row in source_sheet.iter_rows(min_row=2)),
                             (pdf for _, month in sorted(months.items())
                              for _, donation, _ in month for pdf in donation.sources)):
            cell.hyperlink.target = _local_pdf_address(pdf)
        # Preserve the review queue alongside provenance, outside the visible report.
        for column, header in enumerate(('Review account', 'Review publication', 'Review reason', 'Review PDF'), 6):
            provenance.cell(1, column, header)
        audit_rows = ((account, key, reason, pdf) for account, key, reason, paths in queue
                      for pdf in paths or (None,))
        for index, (account, key, reason, pdf) in enumerate(audit_rows, 2):
            for column, value in enumerate((account, key, reason), 6):
                provenance.cell(index, column, value)
            if pdf:
                cell = provenance.cell(index, 9, pdf.name)
                cell.data_type = 's'
                cell.hyperlink = _local_pdf_address(pdf)
        write_totals(wb, rows, False)
        write_index(wb, entries)
        write_summary(wb, year, rows)
        if signature(wb) != original:
            replace_verified(path, before, wb, backup_dir=scope.runtime / 'report-backups' / 'combined' / str(year))
        return {'combined_donations': len(rows), 'combined_review': len(queue)}
