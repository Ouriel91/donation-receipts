from pathlib import Path

import pytest

from src import receipt_summary
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_metadata_extractor import (
    IDENTIFIER_REVIEW_NOTES,
    extract_metadata,
    receipt_numbers_in_text,
)


TAX_LINE = '98765 :מספר אישור דיווח התרומה לרשות המסים'


@pytest.mark.parametrize('header', (
    '- מקור123456 קבלה-תרומה',
    'מקור- 123456 תרומה-קבלה',
    '\t— מקור:\u200f123456   קבלה ־ תרומה:',
    'רוקמ- 123456 המורת-הלבק',
    'מקור 123456 קבלה־תרומה',
))
def test_original_receipt_header_is_separate_from_tax_confirmation(header):
    raw = f'{header}\r\n{TAX_LINE}'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('synthetic.pdf'), raw)
    assert (meta.receipt_number, meta.tax_report_number) == ('123456', '98765')
    assert receipt_numbers_in_text(raw) == {'123456'}


def test_both_pdf_readers_agree_on_receipt_and_tax(monkeypatch, tmp_path):
    for engine, header in (
        ('extract_text_from_pdf', '- מקור123456 קבלה-תרומה'),
        ('extract_text_from_pdf_pymupdf', 'מקור- 123456 תרומה-קבלה'),
    ):
        monkeypatch.setattr(receipt_summary, engine,
                            lambda _, text=f'{header}\n{TAX_LINE}': text)
    meta = receipt_summary._extract_row(tmp_path/'fixture.pdf', 'test-account', None, 2026)
    assert (meta.receipt_number, meta.tax_report_number) == ('123456', '98765')


@pytest.mark.parametrize('header', (
    'עמוד מקור123456 קבלה-תרומה',
    'מקור 123456 קבלה-תרומה עבור בירור',
    'מקור 12/04/2026 קבלה-תרומה',
    'מקור 123456 אסמכתא-תרומה',
    'מקור קבלה-תרומה',
))
def test_incomplete_or_unrelated_header_does_not_supply_a_receipt_number(header):
    raw = f'{header}\n{TAX_LINE}'
    name = Path('- מקור123456 קבלה-תרומה.pdf')
    meta = extract_metadata(normalize_hebrew_text(raw), name, raw)
    assert (meta.receipt_number, meta.tax_report_number) == ('', '98765')
    assert receipt_numbers_in_text(raw) == set()


def test_filename_cannot_supply_the_new_header_evidence():
    raw = f'עמותה רשומה: 580000001\n23/04/2026\n₪ 20.00\n{TAX_LINE}'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('- מקור123456 קבלה-תרומה.pdf'), raw)
    assert (meta.receipt_number, meta.tax_report_number) == ('', '98765')


def test_conflicting_original_headers_require_review():
    raw = f'- מקור123456 קבלה-תרומה\nמקור- 654321 תרומה-קבלה\n{TAX_LINE}'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('synthetic.pdf'), raw)
    assert not meta.receipt_number
    assert meta.tax_report_number == '98765'
    assert IDENTIFIER_REVIEW_NOTES['receipt_number'] in meta.notes
