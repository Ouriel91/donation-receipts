"""Synthetic tests against the Task A Windows annual publisher."""
from contextlib import closing
from copy import copy
from pathlib import Path

import openpyxl

from src.annual_excel_summary import ALL_YEAR, summary_name, write_summary
from src.report_layout import _has_primary_layout, _report_field_columns
from src.sync_workbook import ARCHIVE, ID_HEADER, SOURCES, headers
from tests.test_report2026_core import existing, saved
from tests.test_sync import env, report, rows


def independent_totals(wb, published_ids):
    count = amount = approved = missing = 0
    for ws in wb:
        cols = _report_field_columns(ws)
        ids = headers(ws).get(ID_HEADER)
        if ids is None:
            continue
        status = next((c.column for c in ws[1] if c.value == 'סעיף 46 בתאריך התרומה'
                       and not ws.column_dimensions[c.column_letter].hidden), None)
        for row in range(2, ws.max_row + 1):
            if ws.cell(row, ids).value not in published_ids:
                continue
            count += 1
            value = ws.cell(row, cols['amount']).value
            if type(value) not in (int, float):
                missing += 1
                continue
            amount += value
            if status and ws.cell(row, status).value == 'מאושר':
                approved += value
    return count, amount, approved, missing


def swap_rows(ws, first, second):
    snapshots = [[copy(cell) for cell in ws[row]] for row in (first, second)]
    for row, source in zip((second, first), snapshots):
        for original in source:
            cell = ws.cell(row, original.column)
            cell.value = original.value
            cell._style = copy(original._style)
            cell.hyperlink = copy(original.hyperlink)
            cell.comment = copy(original.comment)


def test_full_summary_preserves_corrections_and_sorted_archive(env):
    existing(env, ('1001', '9999'))
    saved(env)
    path = report(env[0])
    wb = openpyxl.load_workbook(path)
    ws, active = rows(wb)[0]
    cols = headers(ws)
    publication = ws.cell(active, cols[ID_HEADER]).value
    archive = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, cols[ARCHIVE]).value)
    summary = wb[summary_name(2026)]
    assert summary['B2'].value == ALL_YEAR
    assert 'B2' in summary.data_validations.dataValidation[0]
    assert wb.calculation.calcMode == 'auto'
    assert wb[SOURCES].sheet_state == 'hidden'
    assert all(_has_primary_layout(wb[summary.cell(r, 1).value]) for r in range(8, 20))
    status = next(c.column for c in ws[1] if c.value == 'סעיף 46 בתאריך התרומה'
                  and not ws.column_dimensions[c.column_letter].hidden)
    amount = cols['סכום']
    ws.cell(active, amount).value = None
    ws.cell(active, status).value = 'מאושר'
    assert independent_totals(wb, {publication}) == (1, 0, 0, 1)
    ws.cell(active, amount).value = 250
    assert independent_totals(wb, {publication}) == (1, 250, 250, 0)
    ws.cell(active, status).value = 'אין אישור מוכח לתאריך'
    assert independent_totals(wb, {publication}) == (1, 250, 0, 0)
    ws.cell(active, status).value = 'מאושר'
    ws.cell(active, amount).value = 300
    ws.cell(archive, amount).value = 800
    ws.row_dimensions[active].hidden = True
    assert independent_totals(wb, {publication}) == (1, 300, 300, 0)
    summary['B2'] = ws.title
    formula = summary['C8'].value.text
    assert formula.startswith('=SUMPRODUCT(')
    assert 'SUBTOTAL' not in formula and ':$' in formula
    assert '"מאושר"' in summary['D8'].value.text
    swap_rows(ws, active, archive)
    wb.save(path)
    wb.close()

    saved(env)
    with closing(openpyxl.load_workbook(path)) as wb:
        ws, active = rows(wb)[0]
        assert ws.cell(active, headers(ws)[ID_HEADER]).value == publication
        pdf = ws.cell(active, headers(ws)['שם קובץ'])
        assert Path(pdf.hyperlink.target).is_absolute()
        assert Path(pdf.hyperlink.target).is_file()
        assert Path(pdf.hyperlink.target).name == pdf.value
        assert independent_totals(wb, {publication}) == (1, 300, 300, 0)
        assert wb[summary_name(2026)]['B2'].value == ws.title
    assert saved(env)['reports_updated'] == 0

    with closing(openpyxl.load_workbook(path)) as wb:
        ws, active = rows(wb)[0]
        ws.cell(active, status).value = None
        wb.save(path)
    saved(env)
    with closing(openpyxl.load_workbook(path)) as wb:
        assert independent_totals(wb, {publication}) == (1, 300, 0, 0)


def test_empty_year_and_case_insensitive_audit_visibility():
    wb = openpyxl.Workbook()
    audit = wb.active
    audit.title = 'REGISTRY IDENTITY AUDIT'
    audit['A1'] = '=1+2'
    write_summary(wb, 2031, {})
    summary = wb[summary_name(2031)]
    assert audit.sheet_state == 'hidden' and audit['A1'].value == '=1+2'
    assert summary['C20'].value == '=SUM(C8:C19)'
    assert len([ws for ws in wb if ws.sheet_state == 'visible']) == 13
    wb.close()
