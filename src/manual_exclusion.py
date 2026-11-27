"""User-scoped MANUAL reporting decisions. The source and publication stay auditable."""
from contextlib import closing
from datetime import datetime, timezone
import json
import re

from src.private_runtime import operational_lock, safe_path
from src.sync_state import _save_review_catalog, document_paths
from src.user_scope import annual_report

MARKER = 'Manual reporting exclusion'


def excluded_publications(state, year):
    return {key for key, events in state.get('manual_exclusion_history', {}).items()
            if events[-1]['year'] == year and events[-1]['excluded']}


def record_exclusion(scope, year, sha, excluded, reason):
    from src.delta_sync import managed_rows
    from src.sync_workbook import load

    if not re.fullmatch('[0-9a-f]{64}', sha) or not reason.strip():
        raise ValueError('Select a receipt SHA-256 and record a review reason')
    scope.require()
    catalog = safe_path(scope.runtime / 'sync-manual.json')
    report = annual_report(scope.reports, 'manual', year)
    with operational_lock(scope.runtime):
        before = catalog.read_bytes()
        state = json.loads(before)
        workbook_before = report.read_bytes()
        with closing(load(report)) as wb:
            entries, rows = managed_rows(wb)
        owners = [key for key, entry in entries.items() if sha in entry['documents'] and key in rows]
        if len(owners) != 1 or not document_paths(scope.receipts, 'manual', state, [sha]):
            raise ValueError('Select a verified PDF with one indexed MANUAL publication')
        key = owners[0]
        history = state.setdefault('manual_exclusion_history', {}).setdefault(key, [])
        if bool(history and history[-1]['excluded']) == excluded:
            return
        history.append({'year': year, 'excluded': excluded, 'document_sha256': sha,
                        'reason': reason.strip(), 'at': datetime.now(timezone.utc).isoformat()})
        if report.read_bytes() != workbook_before:
            raise ValueError('MANUAL workbook changed during the decision')
        _save_review_catalog(catalog, scope.runtime, 'manual', before, state)


def apply_workbook_exclusions(rows, excluded):
    from src.sync_workbook import _column, headers
    from openpyxl.utils import get_column_letter

    if not excluded <= rows.keys():
        raise ValueError('An excluded MANUAL publication is missing from the workbook')
    for key, (ws, row) in rows.items():
        col = headers(ws).get(MARKER)
        if key in excluded:
            col = col or _column(ws, MARKER)
            ws.column_dimensions[get_column_letter(col)].hidden = True
            ws.cell(row, col).value = 'excluded'
            ws.row_dimensions[row].hidden = True
        elif col is not None and ws.cell(row, col).value == 'excluded':
            ws.cell(row, col).value = None
            ws.row_dimensions[row].hidden = False
    return {key: location for key, location in rows.items() if key not in excluded}
