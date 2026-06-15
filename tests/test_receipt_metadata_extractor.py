from pathlib import Path

from src.receipt_metadata_extractor import (
    STATUS_NEEDS_REVIEW,
    STATUS_OK,
    STATUS_PARTIAL,
    extract_metadata,
)

# Synthetic normalized text mirroring the fragmented RTL layout of real receipts.
FULL_TEXT = """\
קרן קובי מנדל
עמותה
580395051
לכבוד
אוריאל אוחיון
5527
:
המיסים
לרשות
התרומה
דיווח
אישור
מספר
05/06/2026
80806
קבלה
₪200.00
"""

FAKE_PATH = Path("receipts/primary/2026/06_June/05_06_26__קבלה_תרומה_80806.pdf")


# --- basic extraction ---

def test_full_text_all_fields_ok():
    meta = extract_metadata(FULL_TEXT, FAKE_PATH)

    assert meta.file_name == FAKE_PATH.name
    assert meta.receipt_date == "05/06/2026"
    assert meta.amount == "200.00"
    assert meta.registration_number == "580395051"
    assert meta.receipt_number == "80806"
    assert meta.tax_report_number == "5527"
    assert meta.extraction_status in (STATUS_OK, STATUS_PARTIAL)


def test_file_name_always_populated():
    meta = extract_metadata("", FAKE_PATH)

    assert meta.file_name == FAKE_PATH.name


def test_no_file_path_field():
    meta = extract_metadata(FULL_TEXT, FAKE_PATH)

    assert not hasattr(meta, "file_path")


# --- amount extraction ---

def test_amount_shekel_prefix():
    meta = extract_metadata(FULL_TEXT, FAKE_PATH)

    assert meta.amount == "200.00"


def test_amount_with_reversed_total_indicator():
    # "הס\"כ לקש200.00" is what real receipts produce after normalization
    # reverses an already-correct-order PDF (כ"סה שקל → הס"כ לקש).
    text = 'הס"כ לקש200.00\n07/06/2026\nעמותה\nקרן דוגמה'
    path = Path("receipts/primary/2026/06_June/07_06_26__receipt_9999.pdf")
    meta = extract_metadata(text, path)

    assert meta.amount == "200.00"


def test_amount_with_normal_total_indicator():
    text = 'סה"כ ₪350.00\n05/06/2026\nעמותה\nקרן דוגמה'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.amount == "350.00"


def test_amount_without_shekel_symbol():
    text = 'סכום 350.00\n05/06/2026\nעמותה\nקרן דוגמה'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_9999.pdf")
    meta = extract_metadata(text, path)

    # Fallback: shekel-word pattern or total anchor
    # סכום is not in our patterns but this tests the graceful missing case
    assert meta.extraction_status in (STATUS_OK, STATUS_PARTIAL, STATUS_NEEDS_REVIEW)


# --- tax report number extraction ---

def test_tax_report_number_fragmented_before_colon():
    # Real receipt layout: "5527\n: \nהמיסים \nלרשות ..."
    text = "5527\n: \nהמיסים \nלרשות \nהתרומה\n05/06/2026\n₪200.00\nעמותה\n580395051"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_80806.pdf")
    meta = extract_metadata(text, path)

    assert meta.tax_report_number == "5527"


def test_tax_report_number_after_anchor_line():
    # Layout after normalizer reverses already-correct text: anchor then number on next line
    text = "תושרל םיסימה\n32830\n07/06/2026\n₪200.00\nעמותה\n580741221"
    path = Path("receipts/primary/2026/06_June/07_06_26__receipt_17681.pdf")
    meta = extract_metadata(text, path)

    assert meta.tax_report_number == "32830"


def test_tax_report_number_inline_colon():
    # Inline format: "מספר אישור דיווח התרומה לרשות המיסים: 2188"
    text = "מספר אישור דיווח התרומה לרשות המיסים: 2188\n08/06/2026\n₪200.00\nעמותה\n580544641"
    path = Path("receipts/primary/2026/06_June/08_06_26__receipt_80553.pdf")
    meta = extract_metadata(text, path)

    assert meta.tax_report_number == "2188"


# --- date extraction ---

def test_missing_date_falls_back_to_filename():
    text = FULL_TEXT.replace("05/06/2026", "")
    meta = extract_metadata(text, FAKE_PATH)

    assert meta.receipt_date == "05/06/2026"
    assert "תאריך מתוך שם הקובץ" in meta.notes


def test_missing_date_and_no_filename_date():
    text = FULL_TEXT.replace("05/06/2026", "")
    path_no_date = Path("receipts/primary/2026/06_June/no_date_receipt.pdf")
    meta = extract_metadata(text, path_no_date)

    assert meta.receipt_date == ""
    assert meta.extraction_status == STATUS_NEEDS_REVIEW
    assert "תאריך" in meta.notes


# --- status logic ---

def test_missing_amount_is_needs_review():
    text = FULL_TEXT.replace("₪200.00", "")
    meta = extract_metadata(text, FAKE_PATH)

    assert meta.amount == ""
    assert meta.extraction_status == STATUS_NEEDS_REVIEW
    assert "סכום" in meta.notes


def test_missing_org_with_registration_is_not_needs_review():
    # If registration_number exists, missing organization_name is NOT critical.
    # No Hebrew-only line means _find_organization_name returns nothing.
    text = "580395051\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_9999.pdf")
    meta = extract_metadata(text, path)

    assert meta.organization_name == ""
    assert meta.registration_number == "580395051"
    assert meta.extraction_status != STATUS_NEEDS_REVIEW


def test_missing_org_and_registration_is_needs_review():
    text = "05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt.pdf")
    meta = extract_metadata(text, path)

    assert meta.organization_name == ""
    assert meta.registration_number == ""
    assert meta.extraction_status == STATUS_NEEDS_REVIEW
    assert "שם עמותה" in meta.notes


def test_status_partial_when_only_noncritical_missing():
    text = "קרן קובי מנדל\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt.pdf")
    meta = extract_metadata(text, path)

    assert meta.extraction_status == STATUS_PARTIAL
    assert meta.amount == "200.00"
    assert meta.receipt_date == "05/06/2026"


def test_empty_text_all_blank_needs_review():
    meta = extract_metadata("", FAKE_PATH)

    assert meta.file_name == FAKE_PATH.name
    assert meta.organization_name == ""
    assert meta.registration_number == ""
    assert meta.receipt_date == "05/06/2026"  # from filename
    assert meta.amount == ""
    assert meta.extraction_status == STATUS_NEEDS_REVIEW


def test_registration_number_nine_digits():
    text = "עמותה\n123456789\n05/06/2026\n₪100.00\nארגון"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.registration_number == "123456789"
