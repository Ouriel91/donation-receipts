from pathlib import Path

import openpyxl
import pytest
from fpdf import FPDF

from src.account_config import AccountConfig
from src.receipt_summary import (
    COLUMN_HEADERS_HE,
    COLUMNS,
    compute_donor_match,
    generate_summary_workbook,
)


def _make_text_pdf(path: Path, text: str) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text=text)
    pdf.output(str(path))


def _make_empty_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.output(str(path))


def _make_corrupt_pdf(path: Path) -> None:
    path.write_bytes(b"not a pdf")


def _setup_receipts(tmp_path: Path, account: str, year: int) -> Path:
    receipts_dir = tmp_path / "receipts"

    may_dir = receipts_dir / account / str(year) / "05_May"
    may_dir.mkdir(parents=True)
    _make_text_pdf(may_dir / "10_05_26__receipt_1001.pdf", "Date: 10/05/2026 Amount $150")
    _make_text_pdf(may_dir / "20_05_26__receipt_1002.pdf", "Date: 20/05/2026 Amount $250")

    jun_dir = receipts_dir / account / str(year) / "06_June"
    jun_dir.mkdir(parents=True)
    _make_text_pdf(jun_dir / "05_06_26__receipt_2001.pdf", "Date: 05/06/2026")
    _make_empty_pdf(jun_dir / "08_06_26__receipt_2002.pdf")
    _make_corrupt_pdf(jun_dir / "10_06_26__receipt_2003.pdf")

    return receipts_dir


def test_workbook_created(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    assert output_path.exists()
    assert output_path.name == "donation_summary_2026.xlsx"


def test_one_sheet_per_populated_month_in_hebrew(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    assert "מאי" in wb.sheetnames
    assert "יוני" in wb.sheetnames
    # English folder names must not appear as sheet names
    assert "05_May" not in wb.sheetnames
    assert "06_June" not in wb.sheetnames


def test_header_row_is_hebrew(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    headers = [ws.cell(row=1, column=i).value for i in range(1, len(COLUMNS) + 1)]
    assert headers == [COLUMN_HEADERS_HE[col] for col in COLUMNS]


def test_no_file_path_column(tmp_path):
    assert "file_path" not in COLUMNS


def test_file_name_column_present(tmp_path):
    assert "file_name" in COLUMNS
    assert COLUMN_HEADERS_HE["file_name"] == "שם קובץ"


def test_one_row_per_pdf(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    assert wb["מאי"].max_row == 3   # 1 header + 2 PDFs
    assert wb["יוני"].max_row == 4  # 1 header + 3 PDFs


def test_file_name_always_populated(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["יוני"]
    file_name_col = COLUMNS.index("file_name") + 1
    for row_idx in range(2, ws.max_row + 1):
        cell_value = ws.cell(row=row_idx, column=file_name_col).value
        assert cell_value, f"file_name empty on row {row_idx}"


def test_bad_pdfs_become_לבדיקה_and_do_not_abort(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, counts = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    assert output_path.exists()
    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["יוני"]
    status_col = COLUMNS.index("extraction_status") + 1
    statuses = [ws.cell(row=r, column=status_col).value for r in range(2, ws.max_row + 1)]
    assert "לבדיקה" in statuses


def test_status_values_are_hebrew(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    status_col = COLUMNS.index("extraction_status") + 1
    all_statuses = set()
    for sheetname in wb.sheetnames:
        ws = wb[sheetname]
        for r in range(2, ws.max_row + 1):
            v = ws.cell(row=r, column=status_col).value
            if v:
                all_statuses.add(v)
    # All status values in the workbook must be Hebrew, not English
    for s in all_statuses:
        assert s in ("תקין", "חלקי", "לבדיקה"), f"unexpected status: {s}"


def test_counts_use_english_keys(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    _, counts = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    total = sum(counts.values())
    assert total == 5  # 2 May + 3 June
    # Keys are English (used by main.py for printing)
    for key in counts:
        assert key in ("ok", "partial", "needs_review")


def test_empty_year_creates_סיכום_sheet(tmp_path):
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir(parents=True)
    reports_dir = tmp_path / "reports"

    output_path, counts = generate_summary_workbook(receipts_dir, reports_dir, "emptyaccount", 2026)

    assert output_path.exists()
    assert counts == {}
    wb = openpyxl.load_workbook(str(output_path))
    assert "סיכום" in wb.sheetnames


def test_overwrite_existing_workbook(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)
    output_path2, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    assert output_path2 == output_path
    assert output_path2.exists()


# ---------------------------------------------------------------------------
# account column
# ---------------------------------------------------------------------------


def test_account_column_in_columns():
    assert "account" in COLUMNS


def test_account_column_has_hebrew_header():
    assert COLUMN_HEADERS_HE["account"] == "חשבון"


def test_account_column_value_when_no_config(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    account_col = COLUMNS.index("account") + 1
    assert ws.cell(row=2, column=account_col).value == "testaccount"


def test_account_column_uses_display_name_from_config(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"
    config = AccountConfig(display_name="My Primary Account")

    output_path, _ = generate_summary_workbook(
        receipts_dir, reports_dir, "testaccount", 2026, config=config
    )

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    account_col = COLUMNS.index("account") + 1
    for row_idx in range(2, ws.max_row + 1):
        assert ws.cell(row=row_idx, column=account_col).value == "My Primary Account"


# ---------------------------------------------------------------------------
# donor columns
# ---------------------------------------------------------------------------


def test_donor_name_column_in_columns():
    assert "donor_name" in COLUMNS


def test_donor_id_column_in_columns():
    assert "donor_id" in COLUMNS


def test_donor_name_column_has_hebrew_header():
    assert COLUMN_HEADERS_HE["donor_name"] == 'שם תורם שזוהה'


def test_donor_id_column_has_hebrew_header():
    assert COLUMN_HEADERS_HE["donor_id"] == 'ת"ז תורם שזוהתה'


def test_donor_columns_appear_in_workbook_header(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    headers = [ws.cell(row=1, column=i).value for i in range(1, len(COLUMNS) + 1)]
    assert 'שם תורם שזוהה' in headers
    assert 'ת"ז תורם שזוהתה' in headers


def test_account_column_falls_back_to_account_arg_when_config_none(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "myaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(
        receipts_dir, reports_dir, "myaccount", 2026, config=None
    )

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    account_col = COLUMNS.index("account") + 1
    assert ws.cell(row=2, column=account_col).value == "myaccount"


# ---------------------------------------------------------------------------
# compute_donor_match — unit tests (pure function, no PDF needed)
# ---------------------------------------------------------------------------


def _cfg(*, ids=None, names=None) -> AccountConfig:
    return AccountConfig(
        display_name="Test",
        expected_donor_ids=ids or [],
        expected_donor_names=names or [],
    )


def test_donor_match_matched_by_id():
    assert compute_donor_match("123456789", "", _cfg(ids=["123456789"])) == "matched"


def test_donor_match_mismatch_by_id():
    assert compute_donor_match("999999999", "", _cfg(ids=["123456789"])) == "mismatch"


def test_donor_match_id_falls_through_to_name_when_no_id_extracted():
    # expected_donor_ids configured but no donor_id extracted → name check
    assert (
        compute_donor_match(
            "", "ישראל ישראלי", _cfg(ids=["123456789"], names=["ישראל ישראלי"])
        )
        == "matched"
    )


def test_donor_match_matched_by_name_exact():
    assert compute_donor_match("", "ישראל ישראלי", _cfg(names=["ישראל ישראלי"])) == "matched"


def test_donor_match_matched_by_name_substring_detected_in_expected():
    # detected name is substring of expected name
    assert compute_donor_match("", "ישראל", _cfg(names=["ישראל ישראלי"])) == "matched"


def test_donor_match_matched_by_name_expected_in_detected():
    # expected name is substring of detected name
    assert compute_donor_match("", "ישראל ישראלי הגדול", _cfg(names=["ישראל ישראלי"])) == "matched"


def test_donor_match_matched_by_name_case_insensitive():
    assert compute_donor_match("", "israel israely", _cfg(names=["Israel Israely"])) == "matched"


def test_donor_match_mismatch_by_name():
    assert compute_donor_match("", "שם אחר", _cfg(names=["ישראל ישראלי"])) == "mismatch"


def test_donor_match_not_detected_no_config():
    assert compute_donor_match("123456789", "Some Name", None) == "not_detected"


def test_donor_match_not_detected_empty_expectation_lists():
    assert compute_donor_match("123456789", "Some Name", _cfg()) == "not_detected"


def test_donor_match_not_detected_no_donor_data_extracted():
    assert compute_donor_match("", "", _cfg(names=["ישראל ישראלי"])) == "not_detected"


# ---------------------------------------------------------------------------
# donor_match column — structure tests
# ---------------------------------------------------------------------------


def test_donor_match_column_in_columns():
    assert "donor_match" in COLUMNS


def test_donor_match_column_has_hebrew_header():
    assert COLUMN_HEADERS_HE["donor_match"] == "התאמת תורם"


def test_donor_match_column_after_donor_id():
    assert COLUMNS.index("donor_match") == COLUMNS.index("donor_id") + 1


# ---------------------------------------------------------------------------
# donor_match column — workbook integration tests
# ---------------------------------------------------------------------------


def test_donor_match_header_appears_in_workbook(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    headers = [ws.cell(row=1, column=i).value for i in range(1, len(COLUMNS) + 1)]
    assert "התאמת תורם" in headers


def test_donor_match_shows_לא_זוהה_when_no_config(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    col = COLUMNS.index("donor_match") + 1
    for row_idx in range(2, ws.max_row + 1):
        assert ws.cell(row=row_idx, column=col).value == "לא זוהה"


def test_donor_match_shows_לא_זוהה_when_config_has_expectations_but_no_donor_in_pdf(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"
    config = AccountConfig(display_name="Test", expected_donor_names=["ישראל ישראלי"])

    output_path, _ = generate_summary_workbook(
        receipts_dir, reports_dir, "testaccount", 2026, config=config
    )

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    col = COLUMNS.index("donor_match") + 1
    for row_idx in range(2, ws.max_row + 1):
        assert ws.cell(row=row_idx, column=col).value == "לא זוהה"


def test_donor_match_note_appended_when_expectations_configured_but_no_donor_detected(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"
    config = AccountConfig(display_name="Test", expected_donor_names=["ישראל ישראלי"])

    output_path, _ = generate_summary_workbook(
        receipts_dir, reports_dir, "testaccount", 2026, config=config
    )

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    notes_col = COLUMNS.index("notes") + 1
    notes_values = [ws.cell(row=r, column=notes_col).value or "" for r in range(2, ws.max_row + 1)]
    assert all("לא זוהה תורם" in v for v in notes_values)


def test_donor_match_no_note_when_no_config(tmp_path):
    receipts_dir = _setup_receipts(tmp_path, "testaccount", 2026)
    reports_dir = tmp_path / "reports"

    output_path, _ = generate_summary_workbook(receipts_dir, reports_dir, "testaccount", 2026)

    wb = openpyxl.load_workbook(str(output_path))
    ws = wb["מאי"]
    notes_col = COLUMNS.index("notes") + 1
    notes_values = [ws.cell(row=r, column=notes_col).value or "" for r in range(2, ws.max_row + 1)]
    assert not any("לא זוהה תורם" in v for v in notes_values)
