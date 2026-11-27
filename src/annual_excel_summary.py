"""Excel-calculated view of the canonical publication's monthly receipt rows."""

from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.formula import ArrayFormula

from src.attachment_saver import MONTH_NAMES
from src.report_layout import COLUMN_HEADERS_HE, PRIMARY_COLUMNS, _DISPLAY_MARKER, _FIELD_ALIASES
from src.sync_state import SyncError

ALL_YEAR = 'כל השנה'
APPROVED = 'מאושר'
HEADERS = ('חודש', 'קבלות פעילות', 'סה"כ תרומות', 'מתוכן מאושר לפי סעיף 46', 'סכומים לבדיקה')
MONEY = '#,##0.00 "₪"'
MARKER = 'Donation Receipts annual summary v1'


def summary_name(year):
    return f'סיכום {year}'


def _column(ws, field):
    """Find the displayed semantic field, including legacy projection headers."""
    aliases = _FIELD_ALIASES.get(field, (COLUMN_HEADERS_HE[field],))
    return next((cell.column for cell in ws[1]
                 if not ws.column_dimensions[cell.column_letter].hidden
                 and (cell.value in aliases or
                      (cell.comment and cell.comment.text == _DISPLAY_MARKER + field))), None)


def write_summary(wb, year, rows):
    from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, SUMMARY, SOURCES, headers
    from src.receipt_summary import _MONTH_SHEET_NAMES
    from src.manual_exclusion import MARKER as EXCLUSION_MARKER

    months = [_MONTH_SHEET_NAMES[MONTH_NAMES[number]] for number in range(1, 13)]
    others = sorted({ws.title for ws, _ in rows.values()} - set(months))
    for title in months:
        ws = wb[title] if title in wb else wb.create_sheet(title)
        if ws.max_row == 1 and ws['A1'].value is None:
            labels = [COLUMN_HEADERS_HE[field] for field in PRIMARY_COLUMNS] + [ID_HEADER]
            for col, label in enumerate(labels, 1):
                ws.cell(1, col, label).font = Font(size=11, bold=True)
            ws.column_dimensions[get_column_letter(len(PRIMARY_COLUMNS) + 1)].hidden = True
            ws.sheet_view.rightToLeft = True
            ws.freeze_panes = 'A2'
            ws.auto_filter.ref = ws.dimensions
        ws.sheet_state = 'visible'

    title = summary_name(year)
    summary = wb[title] if title in wb else wb.create_sheet(title, 0)
    if summary['A1'].value != MARKER and any(
            cell.value is not None or cell.comment or cell.hyperlink for cells in summary for cell in cells):
        raise SyncError('annual_summary_conflict', 'Existing annual summary must be reviewed before publication')
    old_other_count = summary['B1'].value or 0
    summary['A1'] = MARKER
    summary['B1'] = len(others)
    summary.row_dimensions[1].hidden = True
    selection = summary['B2'].value
    summary['A2'] = 'בחירת תקופה'
    summary['B2'] = selection if selection in (ALL_YEAR, *months) else ALL_YEAR
    if not summary.data_validations.dataValidation:
        validation = DataValidation(type='list', formula1='"' + ','.join((ALL_YEAR, *months)) + '"',
                                    showErrorMessage=True, errorStyle='stop')
        summary.add_data_validation(validation)
        validation.add(summary['B2'])

    for row in (4, 7):
        for col, label in enumerate(HEADERS, 1):
            summary.cell(row, col, label)
            summary.cell(row, col).fill = PatternFill('solid', fgColor='DAE8F4')
    summary['A5'] = '=B2'
    annual_row = 20
    active_sheets = {ws.title for ws, _ in rows.values()}
    if rows:
        index_end = wb[INDEX].max_row
        index = f"{quote_sheetname(INDEX)}!$A$2:$A${index_end}"
    for row, name in enumerate(months + others, 8):
        ws = wb[name]
        columns = headers(ws)
        amount = _column(ws, 'amount')
        status = _column(ws, 'section46_status')
        if not amount or ID_HEADER not in columns:
            raise SyncError('annual_summary_layout', 'Monthly amount or publication column is missing')

        def reference(column):
            letter = get_column_letter(column)
            return f'{quote_sheetname(name)}!${letter}$2:${letter}${max(2, ws.max_row)}'

        result_row = row if row < annual_row else row + 1
        summary.cell(result_row, 1, name)
        if name not in active_sheets:
            for col in range(2, 6):
                summary.cell(result_row, col, 0)
            continue
        ids, amounts = reference(columns[ID_HEADER]), reference(amount)
        literal_ids = f'SUBSTITUTE(SUBSTITUTE(SUBSTITUTE({ids},"~","~~"),"*","~*"),"?","~?")'
        active = f'--IFERROR({ids}<>"",FALSE),--ISNUMBER(MATCH({literal_ids},{index},0))'
        if ARCHIVE in columns:
            active += f',--IFERROR({reference(columns[ARCHIVE])}="",FALSE)'
        if EXCLUSION_MARKER in columns:
            active += f',--IFERROR({reference(columns[EXCLUSION_MARKER])}="",FALSE)'
        numeric_amounts = f'IF(ISNUMBER({amounts}),{amounts},0)'
        formulas = (
            f'=SUMPRODUCT({active})',
            f'=SUMPRODUCT({active},{numeric_amounts})',
            f'=SUMPRODUCT({active},{numeric_amounts},--IFERROR({reference(status)}="{APPROVED}",FALSE))'
            if status else 0,
            f'=SUMPRODUCT({active},--(ISNUMBER({amounts})=FALSE))',
        )
        for col, formula in enumerate(formulas, 2):
            cell = summary.cell(result_row, col)
            # Explicit single-cell array evaluation on Excel versions before 365.
            cell.value = ArrayFormula(cell.coordinate, formula) if formula else 0
    for row in range(21 + len(others), 21 + old_other_count):
        for col in range(1, 6):
            summary.cell(row, col).value = None
    summary.cell(annual_row, 1, 'סה"כ שנתי')
    for col in range(2, 6):
        letter = get_column_letter(col)
        ranges = f'{letter}8:{letter}19' + (f',{letter}21:{letter}{20 + len(others)}' if others else '')
        summary.cell(annual_row, col, f'=SUM({ranges})')
        summary.cell(5, col, f'=IF(B2="{ALL_YEAR}",{letter}{annual_row},'
                             f'INDEX({letter}8:{letter}19,MATCH(B2,A8:A19,0)))')
    summary['A3'] = 'סכומים לבדיקה כוללים תאים ריקים וטקסט. לחישוב יש להזין סכום מספרי.'
    for row in summary.iter_rows():
        for cell in row:
            cell.font = Font(size=11, bold=cell.row in (2, 4, 5, 7, annual_row))
            if (cell.row == 5 or cell.row >= 8) and cell.column in (3, 4):
                cell.number_format = MONEY
    for letter, width in zip('ABCDE', (24, 18, 20, 33, 20)):
        summary.column_dimensions[letter].width = width
    summary.sheet_view.rightToLeft = True
    summary.sheet_state = 'visible'
    wb.active = summary
    for ws in wb:
        if ws.title in (SOURCES, SUMMARY) or ws.title.casefold() == 'registry identity audit':
            ws.sheet_state = 'hidden'
    wb.calculation = CalcProperties(calcMode='auto', fullCalcOnLoad=True)
