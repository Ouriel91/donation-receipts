import openpyxl
import pytest

from src.receipt_metadata_extractor import extract_metadata
from src.sync_workbook import ARCHIVE, headers
from tests.test_report2026_core import existing, saved
from tests.test_sync import env, report, rows, total


@pytest.mark.parametrize('marker', [None, 'bad-json', '{"version":2,"snapshot":"invalid"}'])
def test_inactive_proof_damage_preserves_exact_evidence_and_total(env, marker):
    existing(env, ('1001', '9999'))
    saved(env)
    path = report(env[0])
    book = openpyxl.load_workbook(path)
    sheet, _ = rows(book)[0]
    col = headers(sheet)[ARCHIVE]
    alias = next(row for row in range(2, sheet.max_row + 1) if sheet.cell(row, col).value)
    sheet.cell(alias, col).value = marker
    amount_column = headers(sheet)['סכום']
    sheet.cell(alias, amount_column).value = 999
    book.save(path)
    book.close()

    saved(env)
    assert saved(env)['reports_updated'] == 0
    book = openpyxl.load_workbook(path)
    [(sheet, _)] = rows(book)
    assert sheet.row_dimensions[alias].hidden
    assert total(book) == 100
    assert sheet.cell(alias, col).value == marker
    assert sheet.cell(alias, amount_column).value == 999
    book.close()


def test_numeric_filename_does_not_supply_receipt_even_with_corroborating_document():
    from pathlib import Path

    meta = extract_metadata('עמותה רשומה: 580000001\nתאריך 06/03/2026\nסה"כ ₪475',
                            Path('synthetic_001237.pdf'))
    assert not meta.receipt_number


def test_active_row_cannot_be_rebound_under_inactive_archive_exception():
    from openpyxl import Workbook

    from src.sync_state import SyncError
    from src.sync_workbook import ID_HEADER, _carry_archive_owner

    ws = Workbook().active
    ws.append([ID_HEADER])
    ws.append(['active-identity'])
    with pytest.raises(SyncError, match='collides with an active row'):
        _carry_archive_owner(ws, 2, 'old', 'new')
