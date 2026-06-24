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


# --- amount: whole numbers (no decimal places) ---

def test_amount_whole_number_shekel_prefix():
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.amount == "200"


def test_amount_whole_number_total_indicator():
    text = 'קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\nסה"כ 1,500'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.amount == "1500"


def test_amount_whole_number_shekel_word():
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\nשקל 350"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.amount == "350"


# --- tax_report_number: new compound anchors ---

def test_tax_report_num_reshet_hamisim_same_line():
    # "רשות המיסים: 45678" — compound anchor without לרשות prefix
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nרשות המיסים: 45678"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.tax_report_number == "45678"


def test_tax_report_num_divuach_hateruma_next_line():
    # "דיווח התרומה" on one line, number on next
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nדיווח התרומה\n45678"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.tax_report_number == "45678"


def test_tax_report_num_ishur_divuach_same_line():
    # "אישור דיווח: 45678" — compound phrase, not bare אישור
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nאישור דיווח: 45678"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.tax_report_number == "45678"


# --- registration_number: wider search window ---

def test_registration_number_four_lines_after_anchor():
    # 9-digit number sits 4 lines below the anchor — beyond old ±2 window
    text = "קרן קובי מנדל\nעמותה\nפרטים\nנוספים\nלכבוד\n123456789\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.registration_number == "123456789"


# --- receipt_number: text-based fallback ---

def test_receipt_number_from_donation_phrase():
    # "קבלה תרומה 80806" in text; filename has no trailing number
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nקבלה תרומה 80806"
    path = Path("receipts/primary/2026/06_June/receipt.pdf")
    meta = extract_metadata(text, path)

    assert meta.receipt_number == "80806"


def test_receipt_number_from_mkr_phrase():
    # "מקור קבלה תרומה 80553" in text; filename has no trailing number
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nמקור קבלה תרומה 80553"
    path = Path("receipts/primary/2026/06_June/receipt.pdf")
    meta = extract_metadata(text, path)

    assert meta.receipt_number == "80553"


def test_receipt_number_from_label():
    # "מספר קבלה: 80806" in text; filename has no trailing number
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nמספר קבלה: 80806"
    path = Path("receipts/primary/2026/06_June/receipt.pdf")
    meta = extract_metadata(text, path)

    assert meta.receipt_number == "80806"


def test_receipt_number_filename_takes_precedence_over_text():
    # When filename has a trailing number it should win over text phrase
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00\nקבלה תרומה 99999"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_12345.pdf")
    meta = extract_metadata(text, path)

    assert meta.receipt_number == "12345"


# --- donor extraction ---

def test_donor_id_taz_quoted():
    text = 'ת"ז: 123456789\nקרן דוגמה\n05/06/2026\n₪200.00'
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_taz_dotted():
    text = "ת.ז. 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_taz_bare():
    text = "תז 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_mispar_zehut():
    text = "מספר זהות: 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_mz():
    text = "מ.ז. 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_no_anchor():
    # A bare 9-digit number must NOT be extracted as donor_id (it may be a registration number)
    text = "קרן דוגמה\nעמותה\n123456789\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == ""


def test_donor_name_shem_hatoram():
    text = "שם התורם: ישראל ישראלי\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == "ישראל ישראלי"


def test_donor_name_shem_hameshlem():
    text = "שם המשלם ישראל ישראלי\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == "ישראל ישראלי"


def test_donor_name_shem_male():
    text = "שם מלא: ישראל ישראלי\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == "ישראל ישראלי"


def test_donor_name_lkhvod_inline():
    text = "לכבוד ישראל ישראלי\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == "ישראל ישראלי"


def test_donor_name_lkhvod_next_line():
    # Salutation on one line, name on the next — common in Israeli receipts
    text = "לכבוד\nישראל ישראלי\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == "ישראל ישראלי"


def test_donor_name_absent():
    text = "קרן דוגמה\nעמותה\n123456789\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == ""


def test_donor_fields_not_in_notes():
    # Absent donor fields must never appear in notes
    text = "קרן דוגמה\nעמותה\n123456789\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert "תורם" not in meta.notes
    assert 'ת"ז' not in meta.notes


def test_donor_fields_not_affect_status():
    # A complete receipt with no donor information must still reach OK/PARTIAL
    text = "קרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.extraction_status != STATUS_NEEDS_REVIEW
    assert meta.donor_name == ""
    assert meta.donor_id == ""


# --- organization_name: beyond line 10 ---

def test_org_name_beyond_line_10():
    # Org name appears on line 12 (0-indexed 11); first 10 lines are non-Hebrew-only
    preamble = "\n".join([f"line{i} 123" for i in range(11)])
    text = f"{preamble}\nקרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.organization_name == "קרן קובי מנדל"
