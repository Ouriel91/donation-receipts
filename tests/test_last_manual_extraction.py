from pathlib import Path

import pytest

from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_metadata_extractor import IDENTIFIER_REVIEW_NOTES, extract_metadata
from src.manual_receipts import _inspect
from tests.test_manual_review_quality import readers
from tests.test_observed_receipt_layouts import B_PYPDF, B_END


@pytest.mark.parametrize('tax_line', [
    '2032 :מספר אישור דיווח התרומה לרשות המסים',
    'מספר אישור דיווח התרומה לרשות המסים: 2032',
])
def test_explicit_tax_confirmation_can_look_like_a_year(tax_line):
    raw = B_PYPDF.splitlines()[0] + '\n' + tax_line + B_END
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw)
    assert (meta.receipt_number, meta.tax_report_number) == ('2225689', '2032')
    raw += '\n83119 :מספר אישור דיווח התרומה לרשות המסים'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw)
    assert not meta.tax_report_number
    assert IDENTIFIER_REVIEW_NOTES['tax_report_number'] in meta.notes


def test_year_from_weak_tax_label_is_still_rejected():
    raw = B_PYPDF.splitlines()[0] + B_END + '\nאישור דיווח: 2032'
    assert not extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw).tax_report_number


PAYMENT = (
    '580871803 מספר מוסד\n'
    'קבלת תרומה15/007314 :מספרמקור\n'
    '24/01/2026\n'
    'תיאור פריט כמות ש"ח ליחידה סה"כ ש"ח\n'
    '₪1.00\n'
    '15/007314 :פרטי תשלום: קבלת תרומה                    מספר'
)


@pytest.mark.parametrize('payment_fields', [
    ' שקל חדש    סה"כ 475.00',
    '\n475.00 שקל חדש סה"כ',
    '\nשקל חדש\nסה"כ\n475.00',
])
def test_payment_context_survives_flattened_cells_and_adjacent_reporting(tmp_path, monkeypatch, payment_fields):
    raw = PAYMENT + payment_fields + '    27 פרטי התרומה דווחו לרשות המיסים – אישור מספר'
    readers(monkeypatch, raw)
    path = tmp_path / 'test.pdf'
    content = b'%PDF-1.4 synthetic'
    path.write_bytes(content)
    meta, reason = _inspect(path, content, 2026)
    assert reason is None
    assert (meta.receipt_number, meta.amount, meta.payment_total_verified) == ('007314', '475.00', True)


@pytest.mark.parametrize('payment_fields', [
    '\nשקל חדש\nסה"כ 475.00\nסה"כ 375.00',
    '\nשקל חדש\nסה"כ',
    '\nשקל חדש\nסה"כ 475.00/3',
    '\nשקל חדש\nסה"כ 2    475.00',
    '\nשקל חדש\nסה"כ\n2\n475.00',
    '\nשקל חדש\nסה"כ 2.00    475.00',
    '\nשקל חדש\nסה"כ\n2.00\n475.00',
])
def test_unbound_or_conflicting_totals_do_not_become_verified(payment_fields):
    raw = PAYMENT + payment_fields + '\n27 פרטי התרומה דווחו לרשות המיסים – אישור מספר'
    assert not extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw).payment_total_verified
