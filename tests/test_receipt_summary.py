from pathlib import Path

import openpyxl
import pytest
from fpdf import FPDF

from src.account_config import AccountConfig
from src.receipt_summary import (
    COLUMN_HEADERS_HE,
    COLUMNS,
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
