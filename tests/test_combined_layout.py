from contextlib import closing
from dataclasses import replace

from openpyxl.utils import get_column_letter

from src import combined_report
from src.receipt_metadata_extractor import ReceiptMetadata
from src.sync_workbook import ID_HEADER, LIVE_HEADERS, donation_sheets, headers, load
from src.user_scope import annual_report
from tests.test_sync import env, total
from tests.test_user_scope import register


def test_combined_projection_and_internal_audit(env, monkeypatch):
    project, _, _, _ = env
    scope = register(project, 'orchard')
    scope.configure('gift-mail', 'gifts@example.test')
    raw = b'%PDF-1.4 synthetic'
    meta = ReceiptMetadata(receipt_number='001234', tax_report_number='008321',
                           registration_number='580000001', receipt_date='22/03/2026', amount='100',
                           donor_id='987654321')

    def source(account, receipt, contents):
        pdf = scope.receipts / account / f'{receipt.receipt_number}.pdf'
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(contents)
        return combined_report.PublishedDonation(account, account + '-publication', receipt, 'מאושר',
            set(), frozenset({combined_report.digest(contents)}), (pdf,), False)

    primary = source('primary', meta, raw)
    manual = source('manual', meta, raw)
    secondary = source('gift-mail', replace(meta, receipt_number='001235', amount='50'), raw + b' other')
    records = {row.account: row for row in (primary, manual, secondary)}
    review = ('gift-mail', 'review-me', 'unverified_source_or_date', secondary.paths)
    monkeypatch.setattr(combined_report, '_read',
        lambda _, account, year: ([records[account]], [review] if account == 'gift-mail' else []))

    combined_report.run_combined(scope, 2026)
    path = annual_report(scope.reports, 'combined', 2026)
    with closing(load(path)) as workbook:
        assert 'בדיקת מקורות' not in workbook
        audit = workbook[combined_report._PROVENANCE]
        assert audit.sheet_state == 'veryHidden'
        assert audit['H2'].value == 'unverified_source_or_date'
        assert audit['I2'].hyperlink.target.endswith('001235.pdf')
        assert total(workbook) == 150
        monthly = tuple(donation_sheets(workbook))
        assert len(monthly) == 12
        layout = tuple((cell.value, cell.style_id) for cell in workbook['מרץ'][1])
        for ws in monthly:
            assert tuple((cell.value, cell.style_id) for cell in ws[1]) == layout
            columns = headers(ws)
            for removed in ('ת"ז תורם שזוהתה', 'התאמת תורם', 'חומרה', 'סטטוס הקבלה', 'סטטוס', 'מספר קובצי מקור'):
                assert removed not in columns
            assert ws.column_dimensions[get_column_letter(columns[ID_HEADER])].hidden
            assert ws.auto_filter.ref == ws.dimensions
        month = workbook['מרץ']
        columns = headers(month)
        assert month.cell(2, columns['חשבון']).value == 'PRIMARY-ORCHARD, MANUAL-ORCHARD'
        assert month.cell(3, columns['חשבון']).value == 'GIFT-MAIL-ORCHARD'
        assert month.cell(2, columns['מספר קבלה']).value == '001234'
        assert month.cell(2, columns['מספר אישור דיווח']).value == '008321'
        assert month.cell(2, columns['שם קובץ']).hyperlink.target.endswith('001234.pdf')
        assert month.cell(2, columns['כל קובצי המקור']).hyperlink.target.startswith('#')
        formula = workbook['סיכום 2026']['D10'].value.text
        for field in (ID_HEADER, 'סכום', LIVE_HEADERS[0]):
            assert f"'מרץ'!${get_column_letter(columns[field])}$2:" in formula
        # The prior workbook used four provenance columns and a visible review sheet.
        old_key = audit['A2'].value
        audit.delete_cols(5, 5)
        workbook.create_sheet('בדיקת מקורות')
        workbook.save(path)

    combined_report.run_combined(scope, 2026)
    with closing(load(path)) as workbook:
        assert 'בדיקת מקורות' not in workbook
        assert workbook[combined_report._PROVENANCE]['A2'].value == old_key
    stable = path.read_bytes()
    combined_report.run_combined(scope, 2026)
    assert path.read_bytes() == stable
