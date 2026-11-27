from pathlib import Path

import pytest

from src.receipt_metadata_extractor import extract_metadata, receipt_amounts_in_text, resolve_identifier_evidence
from src.hebrew_text_normalizer import normalize_hebrew_text


@pytest.mark.parametrize(('raw', 'other', 'expected'), [
    ('01/09/2026 :תאריך\n - מקור67108 קבלה עבור תרומה',
     'מקור- 67108 קבלה עבור תרומה', ('67108', '')),
    ('מספר אישור דיווח התרומה לרשות המיסים\n7452\nמקור\n81673 קבלה על תרומה',
     '', ('81673', '7452')),
    ('84732 תרומה קבלת\n20315 :המסים לרשות התרומה דיווח אישור',
     '84732 קבלת תרומה\n20315 :אישור דיווח התרומה לרשות המסים', ('84732', '20315')),
    (' - מקור906501 קבלה עבור תרומה\n36705 :מספר אישור דיווח התרומה לרשות המסים',
     'מקור- 906501 קבלה עבור תרומה\n36705 :מספר אישור דיווח התרומה לרשות המסים',
     ('906501', '36705')),
    ('קבלה עבור תרומה:\n6720\nאישור דיווח התרומה לרשות המסים: 93061', '', ('6720', '93061')),
    ('67931 קבלת תרומה\n40275 :אישור דיווח התרומה לרשות המסים',
     '68123 קבלת תרומה\n40275 :אישור דיווח התרומה לרשות המסים', ('', '40275')),
])
def test_visual_label_binding(raw, other, expected):
    normalized = normalize_hebrew_text(raw)
    meta = extract_metadata(normalized, Path('neutral.pdf'), raw)
    resolve_identifier_evidence(meta, raw, normalized, other, normalize_hebrew_text(other))
    assert (meta.receipt_number, meta.tax_report_number) == expected


def test_dates_do_not_count_as_currency_but_genuine_conflicts_do():
    raw = '₪ 19/07/2026 תאריך\n₪\n01.06.26\n₪100\n₪ 100.00'
    assert receipt_amounts_in_text(raw) == {'100', '100.00'}
    assert receipt_amounts_in_text(raw + '\n₪ 90') == {'100', '100.00', '90'}


@pytest.mark.parametrize('line', ['סה"כ אחר\nש"ח154.61', '154.61 ח״ש', 'ש״ח 154.61'])
def test_explicit_abbreviation_attests_decimal(line):
    assert receipt_amounts_in_text(line) == {'154.61'}
    assert extract_metadata(line, Path('neutral.pdf')).amount == '154.61'
    assert receipt_amounts_in_text('מספר אסמכתא\n154.61') == set()
