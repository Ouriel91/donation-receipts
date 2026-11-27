"""Controlled annual workbook normalization, never historical PDF reconstruction."""
from copy import copy
from datetime import date, datetime
import json
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from src.receipt_summary import COLUMN_HEADERS_HE, REPORT_COLUMNS, SOURCE_SHEET, _MONTH_SHEET_NAMES
from src.attachment_saver import MONTH_NAMES
from src.sync_state import SyncError
from src.sync_workbook import (ID_HEADER, INDEX, LIVE_HEADERS, OFFICIAL, ORIGINAL_NAME,
    SOURCES, donation_sheets, enrich_candidates, headers, metadata, copy_cell_style)

PREVIOUS_NAME = 'שם קודם (baseline)'
REVIEW = 'בדיקת baseline'
AUDIT = 'Annual baseline audit'
FINAL_HEADERS = [COLUMN_HEADERS_HE[k] for k in REPORT_COLUMNS] + [
    ORIGINAL_NAME, PREVIOUS_NAME, OFFICIAL, *LIVE_HEADERS, REVIEW, SOURCES, ID_HEADER]


def check_layout(wb):
    """Validate before discovery/API, rather than discovering unsupported layout late."""
    if '_Sync publications' in wb and wb['_Sync publications']['D1'].value == 'Full report layout v1':
        for ws in donation_sheets(wb):
            headers(ws)
        return  # Full layout preserves original addresses; it does not reorder formulas.
    for ws in donation_sheets(wb):
        headers(ws)
        if ws.merged_cells.ranges:
            raise SyncError('baseline_merged_cells', 'Monthly merged cells require explicit layout review')
        if ws.tables or ws.conditional_formatting or ws.data_validations.count or ws.auto_filter.sortState:
            raise SyncError('baseline_complex_layout', 'Monthly table/validation rules require layout review')
        if any(c.value is not None and ws.cell(1, c.column).value is None
               for row in ws.iter_rows(min_row=2) for c in row):
            raise SyncError('baseline_unlabeled_data', 'Unlabeled monthly data must be preserved explicitly')
    # Reordering does not rewrite formula/named-range dependencies. Fail closed.
    if any(c.data_type == 'f' for ws in wb for row in ws for c in row) or wb.defined_names:
        raise SyncError('baseline_formula_layout', 'Formula or named-range layout requires explicit review')


def linked_sources(wb, path):
    """Only source links bound to actual donation rows prove prior handling.

    A link in an arbitrary audit/review sheet is NOT publication evidence.
    No PDF bytes or old project files are read.
    """
    import os
    result, locations, managed_ids = set(), set(), set()

    def collect(cell):
        target = cell.hyperlink.target if cell.hyperlink else None
        if not target or target.startswith('#'):
            return
        parsed = urlparse(target)
        if parsed.scheme not in ('', 'file') and len(parsed.scheme) != 1:
            return
        value = url2pathname(parsed.path) if parsed.scheme == 'file' else unquote(target)
        if parsed.scheme == 'file' and parsed.netloc:
            value = '//' + parsed.netloc + value
        source = Path(value.replace('\\', '/'))
        if source.suffix.lower() != '.pdf':
            return
        if not source.is_absolute():
            source = path.parent/source
        result.add(stored_source(str(source), Path.cwd()))

    for ws in donation_sheets(wb):
        cols = headers(ws)
        for row in range(2, ws.max_row+1):
            if not any(ws.cell(row,col).value is not None for col in cols.values()):
                continue
            locations.add((ws.title, row))
            if ID_HEADER in cols and ws.cell(row,cols[ID_HEADER]).value:
                managed_ids.add(ws.cell(row,cols[ID_HEADER]).value)
            for name in (COLUMN_HEADERS_HE['file_name'], COLUMN_HEADERS_HE['source_files']):
                if name in cols:
                    collect(ws.cell(row,cols[name]))
    if SOURCE_SHEET in wb.sheetnames:
        for row in wb[SOURCE_SHEET].iter_rows(min_row=2):
            link = row[0].hyperlink
            target = (link.target or link.location) if link else ''
            if target and '!' in target and len(row) >= 3:
                title, ref = target.lstrip('#').rsplit('!',1)
                title = title.strip("'").replace("''", "'")
                ref = ref.replace('$','')
                if ref.startswith('A') and ref[1:].isdigit() and (title,int(ref[1:])) in locations:
                    collect(row[2])
    if SOURCES in wb.sheetnames:
        for row in wb[SOURCES].iter_rows(min_row=2):
            if len(row) >= 3 and row[0].value in managed_ids:
                collect(row[2])
    return result


def stored_source(value, repo):
    import os
    path = Path(os.path.abspath(Path(value.replace('\\', '/')) if Path(value.replace('\\', '/')).is_absolute() else repo/Path(value.replace('\\', '/'))))
    previous = os.environ.get('DONATION_RECEIPTS_PREVIOUS_ROOT')
    if previous:
        old = Path(previous)
        if path.is_relative_to(old) and not path.is_relative_to(repo):
            path = repo/path.relative_to(old)
    return os.path.normcase(os.path.abspath(path))


def enrich_existing(wb, rows, root, live, config, stage, original_sha):
    """Render only workbook values. Temporary keys are not publication claims."""
    rows = {f'baseline-render-{i}': pair for i, pair in enumerate(rows.values())}
    previous_audits = {}
    if 'Registry identity audit' in wb.sheetnames:
        for values in wb['Registry identity audit'].iter_rows(min_row=2, values_only=True):
            if len(values) >= 10:
                previous_audits[(values[0], values[1], str(values[2]))] = values
    audit = wb[AUDIT] if AUDIT in wb.sheetnames else wb.create_sheet(AUDIT)
    if audit['A1'].value is None:
        for col, label in enumerate(['Sheet', 'Row at baseline', 'Registration ID', 'Previous workbook values',
                                     'Original name provenance', 'Source workbook SHA256', 'Disposition'], 1):
            audit.cell(1, col, label)
    entries, before, originals = {}, {}, {}
    audit_start = wb['Registry identity audit'].max_row if 'Registry identity audit' in wb.sheetnames else 1
    for key, (ws, row) in rows.items():
        cols = headers(ws)
        values = {name: ws.cell(row, col).value for name, col in cols.items()}
        before[key] = values
        original = values.get(ORIGINAL_NAME)
        proof = previous_audits.get((ws.title, row, str(values.get(COLUMN_HEADERS_HE['registration_number']) or '')))
        if not original and proof and values.get(COLUMN_HEADERS_HE['organization_name']) in (proof[3], proof[4]):
            original = proof[3]
        originals[key] = original
        # Baseline explicitly owns authoritative name/status normalization;
        # every replaced old claim is retained in baseline audit, never dropped.
        managed = {name: values.get(name) for name in (COLUMN_HEADERS_HE['organization_name'], OFFICIAL, *LIVE_HEADERS)}
        entries[key] = {'managed': managed, 'documents': [],
                        'original_extracted_name': original or values.get(COLUMN_HEADERS_HE['organization_name'], '')}
        if proof:
            entries[key]['identity_audit'] = [proof[2], proof[4], *proof[6:9]]
    enrich_candidates(wb, entries, rows, {}, root, live, config, stage, original_sha)
    # Existing renderer's audit label must not assert an extracted original when
    # only a human-edited workbook name is available.
    if 'Registry identity audit' in wb.sheetnames:
        ws_audit = wb['Registry identity audit']
        for r in range(audit_start + 1, ws_audit.max_row + 1):
            coord = (ws_audit.cell(r, 1).value, ws_audit.cell(r, 2).value)
            key = next((k for k, (ws, row) in rows.items() if (ws.title, row) == coord), None)
            if key is not None:
                ws_audit.cell(r, 4).value = originals[key]
    for key, (ws, row) in rows.items():
        cols = headers(ws)
        old = before[key]
        def put(name, value):
            col = cols.setdefault(name, ws.max_column+1)
            ws.cell(1, col, name)
            cell = ws.cell(row, col)
            cell.value = value
            if isinstance(value, str):
                cell.data_type = 's'
        put(ORIGINAL_NAME, originals[key])
        put(PREVIOUS_NAME, old.get(PREVIOUS_NAME) or old.get(COLUMN_HEADERS_HE['organization_name']))
        changed_claim = any(old.get(name) not in (None, '') and old.get(name) != ws.cell(row, cols[name]).value
                            for name in LIVE_HEADERS)
        note = 'ערכים קודמים נשמרו בביקורת; שינוי באימות מחייב עיון' if changed_claim else old.get(REVIEW)
        if not originals[key]:
            warning = 'שם מקורי מהקבלה אינו ידוע; שם קודם מהדוח נשמר בנפרד'
            note = '; '.join(dict.fromkeys(filter(None, (note, warning))))
        actual = metadata(ws, row).receipt_date
        try:
            donated = datetime.strptime(actual, '%d/%m/%Y').date()
        except ValueError:
            donated = None
        if donated and ws.title != _MONTH_SHEET_NAMES[MONTH_NAMES[donated.month]]:
            warning = 'תאריך התרומה אינו תואם לגיליון ההיסטורי; נדרש עיון ללא הזזת שורה אוטומטית'
            note = '; '.join(dict.fromkeys(filter(None, (note, warning))))
        put(REVIEW, note)
        changed = any(old.get(name) != ws.cell(row, cols[name]).value for name in
                      (COLUMN_HEADERS_HE['organization_name'], OFFICIAL, *LIVE_HEADERS, ORIGINAL_NAME, PREVIOUS_NAME, REVIEW))
        if changed:
            audit.append([ws.title, row, metadata(ws, row).registration_number,
                          json.dumps(old, ensure_ascii=True, default=lambda v: v.isoformat() if isinstance(v, (date, datetime)) else str(v)),
                          'existing original-name field/audit' if originals[key] else 'unknown; workbook value is not extracted evidence',
                          original_sha, 'prior claims retained; authoritative normalization'])
            for cell in audit[audit.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'

    return {(ws.title, row): entries[key] for key, (ws, row) in rows.items()}


def normalize_months(wb):
    """Canonical columns first, extra user columns retained; no row movement."""
    if '_Sync publications' in wb and wb['_Sync publications']['D1'].value == 'Full report layout v1':
        from src.report_layout import _apply_primary_layout
        for ws in donation_sheets(wb):
            _apply_primary_layout(ws)
        return
    sheets = list(donation_sheets(wb))
    extras = list(dict.fromkeys(name for ws in sheets for name in headers(ws) if name not in FINAL_HEADERS))
    for ws in sheets:
        old = headers(ws)
        names = FINAL_HEADERS + extras
        for item in ws.auto_filter.filterColumn:
            previous_name = next((name for name, col in old.items() if col == item.colId + 1), None)
            if previous_name is None:
                raise SyncError('baseline_filter_layout', 'Unresolvable existing filter column')
            item.colId = names.index(previous_name)
        snapshots = {(r, name): copy(ws.cell(r, col)) for name, col in old.items()
                     for r in range(1, ws.max_row+1)}
        for cell in snapshots.values():
            cell._style = copy(cell._style)
        for row in ws:
            for cell in row:
                cell.value = None
                cell.hyperlink = None
                cell.comment = None
                cell._style = None
        for col, name in enumerate(names, 1):
            header = ws.cell(1, col, name)
            source = snapshots.get((1,name))
            if source:
                copy_cell_style(source,header)
                header.comment = copy(source.comment) if source.comment else None
            font = copy(header.font)
            font.bold = True
            header.font = font
            ws.column_dimensions[get_column_letter(col)].hidden = name == ID_HEADER
            ws.column_dimensions[get_column_letter(col)].width = 24 if name not in (REVIEW, LIVE_HEADERS[-1]) else 65
            for r in range(2, ws.max_row+1):
                source = snapshots.get((r, name))
                if source is None:
                    continue
                target = ws.cell(r, col, source.value)
                target.data_type = source.data_type
                copy_cell_style(source, target)
                if source.hyperlink:
                    target.hyperlink = copy(source.hyperlink)
                    target.hyperlink.ref = target.coordinate
                if source.comment:
                    target.comment = copy(source.comment)
                if name == COLUMN_HEADERS_HE['amount']:
                    target.number_format = '#,##0.##'
                if name in (COLUMN_HEADERS_HE['registration_number'], COLUMN_HEADERS_HE['receipt_number']):
                    target.number_format = '@'
        ws.sheet_view.rightToLeft = True
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = f'A1:{get_column_letter(len(names))}{ws.max_row}'
