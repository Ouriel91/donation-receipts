from pathlib import Path

from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_metadata_extractor import (
    STATUS_NEEDS_REVIEW,
    STATUS_OK,
    STATUS_PARTIAL,
    _find_registration_number_by_explicit_anchor,
    _is_valid_corporate_registration_number,
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
    text = "עמותה\n581234567\n05/06/2026\n₪100.00\nארגון"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.registration_number == "581234567"


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
    text = "קרן קובי מנדל\nעמותה\nפרטים\nנוספים\nלכבוד\n581234567\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.registration_number == "581234567"


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


# --- date: adjacent to Hebrew chars (real receipt patterns) ---

def test_date_extracted_when_hebrew_precedes_digits():
    # Real pattern from 03_06_26__מרחבים receipt: date immediately follows Hebrew chars (no space)
    text = "62376 המורת תלבק רוקמ03/06/2026\nעמותה\n580805984\n₪200"
    path = Path("receipts/primary/2026/06_June/03_06_26__קבלת_תרומה_62376_מאת.pdf")
    meta = extract_metadata(text, path)
    assert meta.receipt_date == "03/06/2026"
    assert "תאריך מתוך שם הקובץ" not in meta.notes


def test_date_extracted_when_hebrew_follows_digits():
    # Real pattern from 18_05_26__peachgama receipt: date immediately precedes Hebrew chars (no space)
    text = "18/05/2026ךיראת הקפה\nעמותה\n580537942\n₪100"
    path = Path("receipts/primary/2026/05_May/18_05_26__peachgama_1234.pdf")
    meta = extract_metadata(text, path)
    assert meta.receipt_date == "18/05/2026"
    assert "תאריך מתוך שם הקובץ" not in meta.notes


# --- receipt_number: mid-filename and reversed text (real receipt patterns) ---

def test_receipt_number_mid_filename():
    # Real pattern: מרחבים receipts have the number mid-stem before Hebrew words
    text = "עמותה\n580805984\n03/06/2026\n₪200"
    path = Path("receipts/primary/2026/06_June/03_06_26__קבלת_תרומה_62376_מאת_מרחבים.pdf")
    meta = extract_metadata(text, path)
    assert meta.receipt_number == "62376"


def test_receipt_number_from_reversed_spaced_text():
    # Real pattern from מרחבים receipts: "NUM המורת תלבק רוקמ" (reversed "מקור קבלה תרומה NUM")
    text = "עמותה\n580805984\n03/06/2026\n₪200\n62376 המורת תלבק רוקמ"
    path = Path("receipts/primary/2026/06_June/receipt.pdf")
    meta = extract_metadata(text, path)
    assert meta.receipt_number == "62376"


def test_receipt_number_from_adjacent_reversed_text():
    # Real pattern from peachgama receipts: "רוקמNUMהלבק" (reversed "קבלה NUM מקור")
    text = "עמותה\n580537942\n18/05/2026\n₪100\n - רוקמ260344הלבק רובע המורת"
    path = Path("receipts/primary/2026/05_May/receipt.pdf")
    meta = extract_metadata(text, path)
    assert meta.receipt_number == "260344"


# --- tax_report_number: number before reversed label (real receipt patterns) ---

def test_tax_report_number_before_reversed_label():
    # Real pattern from 18_05_26__peachgama receipt:
    # "73982רפסמ רושיא חוויד המורתה תושרל םיסמה" = reversed "המיסים לרשות ... מספר 73982"
    text = "עמותה\n580537942\n18/05/2026\n₪100\n73982רפסמ רושיא חוויד המורתה תושרל םיסמה"
    path = Path("receipts/primary/2026/05_May/18_05_26__peachgama_1234.pdf")
    meta = extract_metadata(text, path)
    assert meta.tax_report_number == "73982"


# --- organization_name: beyond line 10 ---

def test_org_name_beyond_line_10():
    # Org name appears on line 12 (0-indexed 11); first 10 lines are non-Hebrew-only
    preamble = "\n".join([f"line{i} 123" for i in range(11)])
    text = f"{preamble}\nקרן קובי מנדל\nעמותה\n580395051\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)

    assert meta.organization_name == "קרן קובי מנדל"


# --- donor_id / donor_name / registration_number fixes ---

def test_donor_id_taz_no_trailing_dot():
    text = "ת.ז 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_mas_zehut_apostrophe():
    text = "מס' זהות: 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_mas_zehut_no_apostrophe():
    text = "מס זהות 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_zehut_bare():
    text = "זהות: 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_donor_id_mz_no_trailing_dot():
    text = "מ.ז 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_id == "123456789"


def test_registration_number_malkhur_not_anchor():
    # Both מלכ"ר and עמותה are anchors; NGO context (מלכ"ר) causes the 58-prefix number to win.
    text = 'מלכ"ר\nעמותה\n580123456\nת"ז: 987654321\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580123456"
    assert meta.donor_id == "987654321"


def test_collision_prefers_donor_id():
    # When the only 9-digit number in the text is the donor ID (anchored by ת"ז),
    # the fallback should not set it as registration_number.
    text = 'ת"ז: 123456789\nקרן דוגמה\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.donor_id == "123456789"
    assert meta.registration_number == ""


def test_donor_name_email_rejected():
    text = "שם התורם: donor@example.com\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == ""


def test_donor_name_pure_digits_rejected():
    text = "לכבוד: 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == ""


def test_donor_name_id_trimmed():
    text = "שם התורם: ישראל ישראלי 123456789\nקרן דוגמה\n05/06/2026\n₪200.00"
    meta = extract_metadata(text, FAKE_PATH)
    assert meta.donor_name == "ישראל ישראלי"


# --- registration_number: prefix-based validation ---

def test_registration_number_prefix_30_rejected():
    # Donor ID starting with 30 must not become registration_number, even near an org anchor
    text = "עמותה\n300123456\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == ""


def test_registration_number_prefix_58_accepted():
    # NGO (amuta) registration numbers start with 58
    text = "עמותה\n580123456\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580123456"


def test_registration_number_prefix_51_accepted():
    # Company registration numbers start with 51
    text = 'ח"פ\n512345678\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "512345678"


def test_registration_number_prefix_55_accepted():
    # Cooperative registration numbers start with 55
    text = "ח.פ\n552345678\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "552345678"


# --- dual-text search: raw + normalized ---

def test_dual_text_already_correct_pdf():
    # Simulates a PDF (e.g. Tranzila/Ahavat HaGilad) where Hebrew is already in
    # logical order. normalize_hebrew_text reverses it and breaks Hebrew anchors.
    # Passing raw_text as fallback must recover all fields.
    raw = (
        "לכבוד\n"
        "אוריאל אוחיון\n"
        'ת"ז 123456789\n'
        'מלכ"ר : 580537942\n'
        "קבלה תרומה 302678\n"
        "אישור דיווח: 88287\n"
        "24/06/2026\n"
        "₪100"
    )
    normalized = normalize_hebrew_text(raw)
    path = Path("receipts/primary/2026/06_June/24_06_26__receipt_302678.pdf")
    meta = extract_metadata(normalized, path, raw_text=raw)

    assert meta.donor_name == "אוריאל אוחיון"
    assert meta.donor_id == "123456789"
    assert meta.registration_number == "580537942"
    assert meta.receipt_number == "302678"
    assert meta.tax_report_number == "88287"
    assert meta.receipt_date == "24/06/2026"
    assert meta.amount == "100"


def test_tranzila_logical_order_raw_extracts_all_fields():
    # Simulates pypdf output from a Tranzila/Ahavat HaGilad receipt where
    # Hebrew is already in logical order. normalize_hebrew_text would reverse
    # the anchors and break most patterns — but raw alone must succeed.
    raw = (
        "לכבוד\n"
        "אוריאל אוחיון\n"
        'ת"ז 111111118\n'
        'מלכ"ר : 580537942\n'
        "קבלה תרומה 302678\n"
        "אישור דיווח: 88287\n"
        "24/06/2026\n"
        "₪100"
    )
    path = Path("receipts/primary/2026/06_June/24_06_26__test_302678.pdf")
    meta = extract_metadata(raw, path)

    assert meta.registration_number == "580537942"
    assert meta.receipt_number == "302678"
    assert meta.tax_report_number == "88287"
    assert meta.receipt_date == "24/06/2026"
    assert meta.amount == "100"
    assert meta.donor_name == "אוריאל אוחיון"
    assert meta.donor_id == "111111118"


# --- _is_valid_corporate_registration_number ---


def test_is_valid_corporate_reg_num_58():
    assert _is_valid_corporate_registration_number("580537942") is True


def test_is_valid_corporate_reg_num_51():
    assert _is_valid_corporate_registration_number("512345678") is True


def test_is_valid_corporate_reg_num_52():
    assert _is_valid_corporate_registration_number("522345678") is True


def test_is_valid_corporate_reg_num_55():
    assert _is_valid_corporate_registration_number("552345678") is True


def test_is_valid_corporate_reg_num_rejects_30_prefix():
    assert _is_valid_corporate_registration_number("305678901") is False


def test_donor_id_same_as_registration_clears_registration():
    # 580123450 has valid org prefix (58) so it matches as registration_number
    # near an עמותה anchor; ת"ז anchor also makes it donor_id → collision clears
    # registration_number, keeping donor_id.
    text = (
        'ת"ז 580123450\n'
        "עמותה\n"
        "580123450\n"
        "05/06/2026\n₪200.00"
    )
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.donor_id == "580123450"
    assert meta.registration_number == ""


# --- registration_number: מלכ"ר / מלכ״ר anchor patterns ---


def test_registration_number_amuta_reshumat_colon():
    # "עמותה רשומה: 580123456" — "עמותה" is in _ASSOC_ANCHOR so this already works
    text = "עמותה רשומה: 580123456\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580123456"


def test_registration_number_malkhur_ascii_quote_colon():
    # "מלכ"ר: 580123456" — מלכ"ר (ASCII double-quote) is now in _ASSOC_ANCHOR
    text = 'מלכ"ר: 580123456\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580123456"


def test_registration_number_malkhur_gershayim():
    # "מלכ״ר 580123456" — מלכ״ר (Unicode gershayim ״) is now in _ASSOC_ANCHOR
    text = "מלכ״ר 580123456\n05/06/2026\n₪200.00"
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580123456"


def test_registration_number_malkhur_rejects_30_prefix():
    # "מלכ"ר: 305123456" — 30 prefix is not a valid corporate prefix → rejected
    text = 'מלכ"ר: 305123456\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == ""


# --- registration_number: inline visual layout and adjacent (number before anchor) ---


def test_registration_number_malkhur_space_colon_number():
    # Visual PDF layout: "מלכ"ר : 580537942" (anchor then colon then number, same line)
    text = 'מלכ"ר : 580537942\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580537942"


def test_registration_number_number_adjacent_malkhur():
    # PDF extraction concatenates digits directly with anchor: "580537942מלכ"ר"
    # \b boundary fails between digit and Hebrew — inline _REG_ANCHOR_AFTER handles it.
    text = '580537942מלכ"ר\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580537942"


def test_registration_number_number_space_malkhur():
    # Number before anchor with a space: "580537942 מלכ"ר"
    text = '580537942 מלכ"ר\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == "580537942"


def test_registration_number_malkhur_colon_30_prefix_rejected():
    # "מלכ"ר : 305123456" — 30 prefix is not a valid corporate registration prefix
    text = 'מלכ"ר : 305123456\n05/06/2026\n₪200.00'
    path = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
    meta = extract_metadata(text, path)
    assert meta.registration_number == ""


# --- _find_registration_number_by_explicit_anchor: real-miss regression tests ---


def test_explicit_anchor_malkhur_ascii_space_colon():
    # Real receipt format: "מלכ"ר : 580537943"
    assert _find_registration_number_by_explicit_anchor('מלכ"ר : 580537943') == "580537943"


def test_explicit_anchor_malkhur_gershayim_space_colon():
    # Real receipt format with Unicode gershayim: "מלכ״ר : 580537943"
    assert _find_registration_number_by_explicit_anchor('מלכ״ר : 580537943') == "580537943"


def test_explicit_anchor_malkhur_no_quotes():
    # Variant without any quotation marks: "מלכר : 580537943"
    assert _find_registration_number_by_explicit_anchor('מלכר : 580537943') == "580537943"


def test_explicit_anchor_number_before_malkhur_ascii():
    # Number concatenated before anchor: "580537943מלכ"ר"
    assert _find_registration_number_by_explicit_anchor('580537943מלכ"ר') == "580537943"


def test_explicit_anchor_number_before_malkhur_gershayim():
    # Number concatenated before gershayim variant: "580537943מלכ״ר"
    assert _find_registration_number_by_explicit_anchor('580537943מלכ״ר') == "580537943"


def test_explicit_anchor_amuta_reshumat_colon():
    # Real receipt format: "עמותה רשומה: 580712348"
    assert _find_registration_number_by_explicit_anchor('עמותה רשומה: 580712348') == "580712348"


def test_explicit_anchor_number_before_amuta_reshumat():
    # Number directly before anchor (no space): "580712348עמותה רשומה"
    assert _find_registration_number_by_explicit_anchor('580712348עמותה רשומה') == "580712348"


def test_explicit_anchor_malkhur_rejects_non_ngo_prefix():
    # מלכ"ר is an NGO anchor — must reject non-58 prefix (30 is invalid entirely)
    assert _find_registration_number_by_explicit_anchor('מלכ"ר : 305123456') is None


def test_explicit_anchor_amuta_reshumat_rejects_non_ngo_prefix():
    # עמותה רשומה is an NGO anchor — must reject non-58 prefix
    assert _find_registration_number_by_explicit_anchor('עמותה רשומה: 305123456') is None


# --- registration extraction: real-receipt layout variants (bug diagnosis) ---
# These four shapes come from the problematic receipt. Exactly one is novel:
# variant 3 ("number : anchor") is not matched by _EXPLICIT_REG_AFTER (which
# only allows horizontal whitespace between number and anchor, not a colon) and
# must fall through to the window-based search.

_BUG_PATH = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")
_BUG_SUFFIX = "\n05/06/2026\n₪100.00"


def test_registration_bug_variant_anchor_colon_number():
    # "מלכ"ר : 580537942" — anchor then colon then number (standard visual layout)
    meta = extract_metadata('מלכ"ר : 580537942' + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580537942"


def test_registration_bug_variant_number_adjacent_anchor():
    # "580537942מלכ"ר" — number directly concatenated before anchor (PDF concat artefact)
    meta = extract_metadata('580537942מלכ"ר' + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580537942"


def test_registration_bug_variant_number_colon_anchor():
    # "580537942 : מלכ"ר" — number then colon then anchor (reversed label layout).
    # _EXPLICIT_REG_AFTER only allows whitespace between digits and anchor, not ":".
    # Must fall through to the window-based search in _find_registration_number.
    meta = extract_metadata('580537942 : מלכ"ר' + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580537942"


def test_registration_bug_variant_amuta_reshumat():
    # "עמותה רשומה: 580712348" — anchor then number, direct (non-normalized) form
    meta = extract_metadata("עמותה רשומה: 580712348" + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580712348"


def test_registration_bug_variant_amuta_reshumat_normalized():
    # normalize_hebrew_text reverses each Hebrew word in place:
    # "עמותה רשומה: 580712348" → "התומע המושר: 580712348"
    # The reversed-anchor forms in _EXPLICIT_REG_BEFORE must match this.
    meta = extract_metadata("התומע המושר: 580712348" + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580712348"


# --- עמותה רשומה layout variants (anchor + number, all four orientations) ---


def test_registration_amuta_reshumat_no_colon():
    # "עמותה רשומה 580712348" — anchor before number, space only (no colon)
    meta = extract_metadata("עמותה רשומה 580712348" + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580712348"


def test_registration_amuta_reshumat_number_adjacent_before():
    # "580712348עמותה רשומה" — number immediately before anchor, no space
    meta = extract_metadata("580712348עמותה רשומה" + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580712348"


def test_registration_amuta_reshumat_number_space_before():
    # "580712348 עמותה רשומה" — number before anchor with a space
    meta = extract_metadata("580712348 עמותה רשומה" + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == "580712348"


def test_registration_amuta_reshumat_rejects_non_58_prefix():
    # "עמותה רשומה" is an NGO anchor — must reject a non-58 prefix
    meta = extract_metadata("עמותה רשומה: 512345678" + _BUG_SUFFIX, _BUG_PATH)
    assert meta.registration_number == ""


# --- _find_registration_number_by_explicit_anchor: עמותה רשומה across a line break ---


def test_explicit_anchor_amuta_reshumat_space_before_colon():
    # "עמותה רשומה : 580712348" — space before the colon as well as after
    assert _find_registration_number_by_explicit_anchor('עמותה רשומה : 580712348') == "580712348"


def test_explicit_anchor_amuta_reshumat_colon_newline_number():
    # "עמותה רשומה:\n580712348" — number on the line following the colon
    assert _find_registration_number_by_explicit_anchor('עמותה רשומה:\n580712348') == "580712348"


def test_explicit_anchor_amuta_reshumat_preceded_by_other_line():
    # Anchor line preceded by an unrelated line — must still find the number
    text = 'חרות ישראל בארצנו\nעמותה רשומה: 580712348'
    assert _find_registration_number_by_explicit_anchor(text) == "580712348"


def test_explicit_anchor_amuta_reshumat_colon_newline_rejects_non_58_prefix():
    # Same line-break shape, but a non-58 prefix must still be rejected
    assert _find_registration_number_by_explicit_anchor('עמותה רשומה:\n305123456') is None
