"""Anonymized field values with observed reader line structure retained."""
from pathlib import Path

import pytest

from src import receipt_summary
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.manual_receipts import _inspect
from src.receipt_metadata_extractor import (
    extract_metadata, receipt_numbers_in_text, IDENTIFIER_REVIEW_NOTES,
)

A_PYPDF = (
    '580871803 מספר מוסד\n'
    'קבלת תרומה15/007314 :מספרמקור\n'
    '15/007314 :פרטי תשלום: קבלת תרומה                    מספר\n'
    '27 פרטי התרומה דווחו לרשות המיסים – אישור מספר\n'
    '24/01/2026\n₪1.00\n1,000.00'
)
A_MUPDF = A_PYPDF.replace(':מספרמקור', ':מספר\nמקור')
B_PYPDF = 'עותק 2225689 - קבלה מספרקבלה על תרומה\n311415 :מספר אישור דיווח התרומה לרשות המסים'
B_MUPDF = B_PYPDF.replace('עותק 2225689', 'עותק2225689').replace('מספרקבלה', 'מספר\nקבלה')
C = '853986419 קבלה על תרומה מספר\nמספר אישור דיווח התרומה לרשות המסים\n7310 :הקצאה מספר'
B_END = '\nעמותה רשומה: 580386746\n09/07/2026\n₪200.00'
C_END = '\nעמותה רשומה: 580472638\n28/03/2026\n₪250.00'


@pytest.mark.parametrize(('raw', 'number', 'tax', 'organization', 'date', 'amount', 'reason'), [
    (A_PYPDF, '007314', '', '580871803', '24/01/2026', '', 'ambiguous_donation_amount'),
    (A_MUPDF, '007314', '', '580871803', '24/01/2026', '', 'ambiguous_donation_amount'),
    (B_PYPDF + B_END, '2225689', '311415', '580386746', '09/07/2026', '200.00', None),
    (B_MUPDF + B_END, '2225689', '311415', '580386746', '09/07/2026', '200.00', None),
    (C + C_END, '853986419', '7310', '580472638', '28/03/2026', '250.00', None),
])
def test_observed_structures_independently(monkeypatch, tmp_path, raw, number, tax, organization, date, amount, reason):
    filename = tmp_path / 'anonymous.pdf'
    content = b'%PDF-1.4 synthetic'
    filename.write_bytes(content)
    for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr(receipt_summary, reader, lambda _: raw)
    meta, rejection = _inspect(filename, content, 2026)
    assert meta.receipt_number == number
    assert meta.tax_report_number == tax
    assert meta.registration_number == organization
    assert meta.receipt_date == date
    assert meta.amount == amount
    assert rejection == reason
    if reason:
        assert meta.full_source_serial == '15/007314'
        assert meta.tax_confirmation_candidate == '27'
        assert receipt_numbers_in_text(raw) == {'007314'}


@pytest.mark.parametrize('raw', [B_PYPDF + B_END, B_MUPDF + B_END, C + C_END])
def test_page_counters_do_not_bind_to_tax_header(raw):
    label = 'מספר אישור דיווח התרומה לרשות המסים'
    raw += '\n' + label + '\n1\nפרטי תשלום'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw_text=raw)
    assert meta.tax_report_number in {'311415', '7310'}
    assert IDENTIFIER_REVIEW_NOTES['tax_report_number'] not in meta.notes


def test_observed_copy_still_reports_conflicting_labelled_receipt_and_tax():
    raw = (B_PYPDF + B_END + '\nקבלה מספר - 39231\n'
           '683714 :מספר אישור דיווח התרומה לרשות המסים')
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw_text=raw)
    assert not meta.receipt_number and not meta.tax_report_number
    assert all(note in meta.notes for note in IDENTIFIER_REVIEW_NOTES.values())



def test_institution_candidate_requires_published_review_to_propose():
    from dataclasses import asdict
    from src.sync_state import _refresh_identifiers

    meta = extract_metadata(A_PYPDF, Path('anonymous.pdf'))
    entry = {'metadata': asdict(meta)}
    _refresh_identifiers(entry, meta, 2026)
    assert not entry['identifier_refresh']['proposals']
    _refresh_identifiers(entry, meta, 2026, published={'tax_report_number': ''})
    assert entry['identifier_refresh']['proposals']['tax_report_number']['value'] == '27'
    _refresh_identifiers(entry, meta, 2026, published={'tax_report_number': '6292'})
    assert not entry['identifier_refresh']['proposals']


def test_distinct_institution_header_and_payment_serials_still_require_review():
    raw = A_PYPDF.replace('15/007314 :פרטי', '16/007314 :פרטי')
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw_text=raw)
    assert not meta.receipt_number and not meta.full_source_serial
    assert IDENTIFIER_REVIEW_NOTES['receipt_number'] in meta.notes


def test_observed_copy_legacy_reported_field_on_same_line_still_conflicts():
    raw = B_PYPDF + B_END + '\nאישור דיווח: 449707'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw_text=raw)
    assert not meta.tax_report_number
    assert IDENTIFIER_REVIEW_NOTES['tax_report_number'] in meta.notes



def test_copy_does_not_publish_short_unverified_tax_token():
    raw = B_PYPDF.replace('311415', '1') + B_END
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw_text=raw)
    assert meta.receipt_number == '2225689'
    assert not meta.tax_report_number
    assert meta.tax_confirmation_candidate == '1'


def test_copy_does_not_treat_institution_leading_report_pattern_as_tax():
    raw = B_PYPDF.splitlines()[0] + B_END + '\n1 פרטי התרומה דווחו לרשות המיסים – אישור מספר'
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw_text=raw)
    assert not meta.tax_report_number and not meta.tax_confirmation_candidate
