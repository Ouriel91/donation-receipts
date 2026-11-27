from pathlib import Path

import pytest

from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_metadata_extractor import (
    IDENTIFIER_REVIEW_NOTES, extract_metadata, receipt_numbers_in_text,
)


@pytest.mark.parametrize('text,receipt,approval', [
    ('קבלה על תרומה 14519', '14519', ''),
    ('קבלה עבור תרומה 15523', '15523', ''),
    ('אישור דיווח התרומה לרשות המסים: 18828', '', '18828'),
    ('מספר אישור דיווח התרומה לרשות המסים: 46664', '', '46664'),
    ('קבלה עבור\nתרומה —\u200f 00671\nמספר אישור דיווח ：\n91284', '00671', '91284'),
    ('קבלה על תרומה 80019\nאישור דיווח: 519\nאישור דיווח: 520', '80019', ''),
])
def test_semantic_fields(text, receipt, approval):
    meta = extract_metadata(normalize_hebrew_text(text), Path('sample.pdf'), text)
    assert (meta.receipt_number, meta.tax_report_number) == (receipt, approval)
    if '520' in text:
        assert IDENTIFIER_REVIEW_NOTES['tax_report_number'] in meta.notes
    assert '519' not in receipt_numbers_in_text(text)


def test_unlabelled_numeric_filename_is_not_evidence():
    text = 'מספר עמותה: 580000001\n02/12/2025\nסה"כ ₪100'
    for name in ('provider_00671.pdf', 'provider_00671o.pdf'):
        assert extract_metadata(text, Path(name)).receipt_number == ''
    assert extract_metadata('', Path('קבלת_תרומה_00671.pdf')).receipt_number == ''
    assert extract_metadata(text, Path('קבלת_תרומה_00671.pdf')).receipt_number == '00671'


def test_real_pdf_container_with_held_out_identifiers(tmp_path):
    import pymupdf
    from src.receipt_summary import _extract_row

    path = tmp_path / 'synthetic.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_htmlbox(page.rect, 'קבלה עבור תרומה 63819<br>'
                            'מספר אישור דיווח התרומה לרשות המסים: 71826<br>'
                            'מספר עמותה: 580000001<br>03/11/2025<br>₪100')
        pdf.save(path)
    meta = _extract_row(path, 'primary', None, 2025)
    assert (meta.receipt_number, meta.tax_report_number) == ('63819', '71826')



def test_distinct_legacy_and_new_donation_headings_require_review():
    text = 'קבלה תרומה 88213\nקבלה עבור תרומה 81225'
    meta = extract_metadata(text, Path('synthetic.pdf'))
    assert not meta.receipt_number
    assert IDENTIFIER_REVIEW_NOTES['receipt_number'] in meta.notes
