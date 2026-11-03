"""Task A regression cases: invented receipt text and identifiers only."""

from pathlib import Path

import openpyxl
import pytest

from src import receipt_summary as summary
from src.account_config import AccountConfig
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_metadata_extractor import extract_metadata


@pytest.mark.parametrize('label', [
    'קבלה # 1234', "קבלה מס' 1234", 'מספר קבלה: 1234',
    'קבלה # AB123', 'קבלה מספר AB123', 'קבלה מספר AB-2026/001',
    'מספר קבלה: A_123', 'קבלה # R.123', 'קבלה מס׳ 1234',
])
@pytest.mark.parametrize('normalized', [False, True])
def test_receipt_labels_and_separators(label, normalized):
    expected = label.split()[-1]
    text = normalize_hebrew_text(label) if normalized else label
    assert extract_metadata(text, Path('synthetic_9999.pdf')).receipt_number == expected


@pytest.mark.parametrize('number', ['313123', '000313123456789012345', 'AB-000313123/007', 'R.000313123'])
@pytest.mark.parametrize('normalized', [False, True])
def test_on_donation_and_hash_labels_identify_same_complete_receipt(number, normalized):
    texts = [f'קבלה # {number}', f'קבלה על תרומה {number}']
    if normalized:
        texts = [normalize_hebrew_text(text) for text in texts]
    assert [extract_metadata(text, Path('synthetic_9999.pdf')).receipt_number
            for text in texts] == [number, number]


@pytest.mark.parametrize('number', ['313123', '000313123456789012345', 'AB-000313123/007', 'R.000313123'])
@pytest.mark.parametrize('template', ['{} # הלבק', '{} : # תלבק', '{}#:קבלה'])
@pytest.mark.parametrize('normalized', [False, True])
def test_number_before_hash_receipt_label_preserves_complete_token(number, template, normalized):
    text = template.format(number)
    if normalized:
        text = normalize_hebrew_text(text)
    assert extract_metadata(text, Path('synthetic_9999.pdf')).receipt_number == number


@pytest.mark.parametrize('number', ['580000001', '123456782', '654321'])
@pytest.mark.parametrize('normalized', [False, True])
def test_reversed_hash_label_excludes_other_known_identifiers(number, normalized):
    raw = ('מספר עמותה: 580000001\nת.ז 123456782\nאישור דיווח: 654321\n'
           f'{number} # הלבק')
    text = normalize_hebrew_text(raw) if normalized else raw
    assert extract_metadata(text, Path('synthetic.pdf'), raw_text=raw).receipt_number == ''


@pytest.mark.parametrize('template', [
    'קבלה  על\tתרומה: {}', 'קבלה\nעל\nתרומה # {}',
    'קבלה\u00a0על\u00a0תרומה:#{}', 'קבלה על תרומה #: {}',
    'קבלה על תרומה:{}', '{} המורת לע הלבק', '{} # : המורת לע תלבק',
    '{}:המורת לע הלבק רוקמ',
])
@pytest.mark.parametrize('normalized', [False, True])
def test_on_donation_whitespace_punctuation_and_pdf_order(template, normalized):
    number = 'AB-000313123456789/007'
    text = template.format(number)
    if normalized:
        text = normalize_hebrew_text(text)
    assert extract_metadata(text, Path('synthetic_9999.pdf')).receipt_number == number


@pytest.mark.parametrize('text', [
    'קבלה על תרומה AB313123עברית', 'AB313123עברית המורת לע הלבק',
    '/AB313123 המורת לע הלבק', '.AB313123 המורת לע הלבק',
    'קבלה על תרומה AB313123/', 'קבלה על תרומה: שם עמותה 580000001',
    'קבלה על תרומה: ת.ז 123456782', 'קבלה על תרומה: אישור דיווח 654321',
    'לאקבלה על תרומה 313123', '313123 המורת לע הלבקה',
    'קבלה על תרומה 02/07/2026', '02.07.2026 המורת לע הלבק',
    '/AB313123 # הלבק', 'AB313123עברית # הלבק', 'AB313123 # הלבקה',
])
@pytest.mark.parametrize('normalized', [False, True])
def test_on_donation_rejects_partial_tokens_and_unrelated_fields(text, normalized):
    if normalized:
        text = normalize_hebrew_text(text)
    assert extract_metadata(text, Path('synthetic.pdf')).receipt_number == ''


@pytest.mark.parametrize('number', ['580000001', '123456782', '654321'])
@pytest.mark.parametrize('normalized', [False, True])
def test_on_donation_excludes_known_registration_donor_and_reporting_ids(number, normalized):
    raw = ('מספר עמותה: 580000001\nת.ז 123456782\nאישור דיווח: 654321\n'
           f'קבלה על תרומה: {number}')
    text = normalize_hebrew_text(raw) if normalized else raw
    # Production supplies raw text alongside normalized text for ID exclusions.
    assert extract_metadata(text, Path('synthetic.pdf'), raw_text=raw).receipt_number == ''


@pytest.mark.parametrize('raw_first', [False, True])
def test_summary_text_receipt_beats_conflicting_filename(raw_first):
    logical = 'מספר קבלה: AB123'
    visual = normalize_hebrew_text(logical)
    raw, normalized = (logical, visual) if raw_first else (visual, logical)
    meta = summary._best_metadata(Path('synthetic_9999.pdf'), raw, normalized)
    assert meta.receipt_number == 'AB123'


def test_raw_receipt_beats_filename_when_normalized_has_no_receipt_label():
    meta = extract_metadata('₪100', Path('synthetic_9999.pdf'), raw_text='קבלה # AB123')
    assert meta.receipt_number == 'AB123'
    merged = summary._best_metadata(Path('synthetic_9999.pdf'), 'קבלה # AB123', '₪100')
    assert merged.receipt_number == 'AB123'


def test_receipt_filename_remains_fallback():
    assert extract_metadata('₪100', Path('synthetic_9876.pdf')).receipt_number == '9876'


LONG_RECEIPT_NUMBERS = ['0012345678', '001234567890123456789', '0000' + '1234567890' * 4]


@pytest.mark.parametrize('number', LONG_RECEIPT_NUMBERS)
@pytest.mark.parametrize('template', [
    'קבלה תרומה {}', 'מקור קבלה תרומה {}', '{} המורת תלבק רוקמ',
    '{} המורת הלבק', 'רוקמ{}הלבק',
])
def test_long_receipt_number_is_complete_in_existing_numeric_layouts(number, template):
    meta = extract_metadata(template.format(number), Path('synthetic_9999.pdf'))
    assert meta.receipt_number == number


@pytest.mark.parametrize('number', [*LONG_RECEIPT_NUMBERS, 'AB-001234567890/007'])
@pytest.mark.parametrize('template', ['קבלה # {}', "קבלה מס' {}", 'מספר קבלה: {}', 'קבלה מספר {}'])
@pytest.mark.parametrize('normalized', [False, True])
def test_long_labelled_receipt_numbers_preserve_leading_zeros(number, template, normalized):
    text = template.format(number)
    if normalized:
        text = normalize_hebrew_text(text)
    assert extract_metadata(text, Path('synthetic_9999.pdf')).receipt_number == number


@pytest.mark.parametrize('number', LONG_RECEIPT_NUMBERS)
@pytest.mark.parametrize('template', ['synthetic_{}.pdf', 'synthetic_{}_attachment.pdf'])
def test_long_filename_receipt_fallback_is_complete(number, template):
    meta = extract_metadata('₪100', Path(template.format(number)))
    assert meta.receipt_number == number


@pytest.mark.parametrize('text', [
    'AB001234567890 המורת תלבק רוקמ', 'AB-001234567890 המורת תלבק',
    '/001234567890 המורת תלבק', '.001234567890 המורת תלבק',
    '_001234567890 המורת תלבק', 'קבלה תרומה 001234567890AB',
    'קבלה תרומה 001234567890/007', 'קבלה תרומה 001234567890-AB',
    'קבלה תרומה 001234567890.007', 'קבלה תרומה 001234567890עברית',
    'רוקמAB001234567890הלבק', 'קבלה # 001234567890עברית',
])
def test_numeric_heuristics_do_not_extract_part_of_larger_receipt_token(text):
    assert extract_metadata(text, Path('synthetic.pdf')).receipt_number == ''
    assert extract_metadata(text + '\nקבלה # AB123', Path('synthetic.pdf')).receipt_number == 'AB123'


@pytest.mark.parametrize('receipt_engine', ['pypdf', 'pymupdf'])
def test_long_reversed_receipt_preserved_across_pdf_engines(monkeypatch, receipt_engine):
    expected = '001234567890123456789'
    richer = 'עמותת בדיקה\nמספר עמותה: 580000001\n02/01/2026\n₪100.00'
    receipt_only = f'{expected} המורת תלבק רוקמ'
    pypdf, pymupdf = ((receipt_only, richer) if receipt_engine == 'pypdf'
                      else (richer, receipt_only))
    monkeypatch.setattr(summary, 'extract_text_from_pdf', lambda path: pypdf)
    monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf', lambda path: pymupdf)
    meta = summary._build_row(Path('synthetic_9999.pdf'), 'synthetic', None, 2026)
    assert meta.receipt_number == expected


@pytest.mark.parametrize('receipt_engine', ['pypdf', 'pymupdf'])
def test_receipt_text_beats_filename_across_pdf_engines(monkeypatch, receipt_engine):
    # The richer engine has a filename fallback; the other has only a receipt label.
    richer = 'עמותת בדיקה\nמספר עמותה: 580000001\n02/01/2026\n₪100.00'
    receipt_only = 'מספר קבלה: AB123'
    pypdf, pymupdf = ((receipt_only, richer) if receipt_engine == 'pypdf'
                      else (richer, receipt_only))
    monkeypatch.setattr(summary, 'extract_text_from_pdf', lambda path: pypdf)
    monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf', lambda path: pymupdf)
    meta = summary._build_row(Path('synthetic_9999.pdf'), 'synthetic', None, 2026)
    assert meta.receipt_number == 'AB123'
    assert meta.amount == '100.00'
    assert meta.registration_number == '580000001'


@pytest.mark.parametrize('other_number', ['580000001', '123456782', '654321'])
def test_other_identifiers_do_not_become_receipt_number(other_number):
    text = ('מספר עמותה: 580000001\nת.ז 123456782\nאישור דיווח: 654321\n'
            f'קבלה # {other_number}')
    assert extract_metadata(text, Path('synthetic.pdf')).receipt_number == ''
    assert extract_metadata(text, Path(f'synthetic_{other_number}.pdf')).receipt_number == ''
    assert extract_metadata(text + '\nקבלה # AB123', Path('synthetic.pdf')).receipt_number == 'AB123'


@pytest.mark.parametrize('text', [
    'קבלה מספר עמותה: 580000001', 'קבלה מספר אישור דיווח: 654321',
    'קבלה ת.ז 123456782', 'מספר עמותה: 580000001\nת.ז 123456782\nאישור דיווח: 654321',
    'קבלה # ABC', 'קבלה # AB123עברית',
])
def test_no_unlabelled_or_partial_receipt_matches(text):
    assert extract_metadata(text, Path('synthetic.pdf')).receipt_number == ''


@pytest.mark.parametrize('label', [
    'ת.ז', 'ת.ז.', 'ת"ז:', 'תז', 'ת״ז:', 'ת . ז .',
    'ת\u200f.ז', 'ת\u200e"ז',
])
@pytest.mark.parametrize('normalized', [False, True])
def test_donor_id_labels_with_direction_marks(label, normalized):
    text = label + ' \u2066123456782\u2069'
    if normalized:
        text = normalize_hebrew_text(text)
    assert extract_metadata(text, Path('synthetic.pdf')).donor_id == '123456782'


@pytest.mark.parametrize('text', ['123456782', 'מספר עמותה: 580000001',
                                 'ת.ז 1234567820', 'ת.ז 12345678', 'ת.ז 123456782ABC'])
def test_donor_id_requires_label_and_exactly_nine_digits(text):
    assert extract_metadata(text, Path('synthetic.pdf')).donor_id == ''


LINES = {
    'organization_name': 'עמותת בדיקה',
    'registration_number': 'מספר עמותה: 580000001',
    'receipt_number': 'מספר קבלה: AB123',
    'tax_report_number': 'אישור דיווח: 654321',
    'receipt_date': '02/01/{year}',
    'amount': '₪100.00',
}


@pytest.fixture
def build_synthetic_row(monkeypatch):
    def build(*, missing=(), year=2026, extra='', config=None):
        text = '\n'.join(v.format(year=year) for k, v in LINES.items() if k not in missing)
        text += '\n' + extra
        monkeypatch.setattr(summary, 'extract_text_from_pdf', lambda path: text)
        monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf', lambda path: '')
        return summary._build_row(Path('synthetic.pdf'), 'synthetic', config, year)
    return build


def test_missing_report_number_only_is_warning_and_partial(build_synthetic_row):
    meta = build_synthetic_row(missing=('tax_report_number',))
    assert meta.tax_report_number == ''
    assert (meta.severity, meta.extraction_status) == ('warning', 'partial')
    assert meta.notes == 'מספר אישור דיווח חסר — דרוש אימות'


@pytest.mark.parametrize('field', ['receipt_number', 'amount', 'registration_number', 'receipt_date'])
@pytest.mark.parametrize('year', range(2020, 2027))
def test_missing_core_field_stays_critical(build_synthetic_row, field, year):
    meta = build_synthetic_row(missing=(field, 'tax_report_number'), year=year)
    assert (meta.severity, meta.extraction_status) == ('critical', 'needs_review')
    assert 'שדה חובה חסר:' in meta.notes


@pytest.mark.parametrize('year', range(2020, 2026))
def test_report_number_not_required_before_2026(build_synthetic_row, year):
    meta = build_synthetic_row(missing=('tax_report_number',), year=year)
    assert (meta.severity, meta.extraction_status) == ('ready', 'ok')
    assert meta.tax_report_number == ''
    assert meta.notes == 'שדה לא נדרש לשנת מס זו'


def test_donor_mismatch_is_critical_even_without_report_number(build_synthetic_row):
    config = AccountConfig('Synthetic', expected_donor_ids=['222222226'])
    meta = build_synthetic_row(missing=('tax_report_number',), extra='ת.ז 123456782', config=config)
    assert meta.donor_match == 'mismatch'
    assert (meta.severity, meta.extraction_status) == ('critical', 'needs_review')
    assert 'תעודת זהות תורם לא תואמת' in meta.notes


def test_workbook_warning_status_and_note_agree(tmp_path, monkeypatch):
    receipts = tmp_path / 'receipts'
    month = receipts / 'synthetic' / '2026' / '01_January'
    month.mkdir(parents=True)
    (month / 'synthetic.pdf').write_bytes(b'synthetic input; extraction is mocked')
    text = '\n'.join(v.format(year=2026) for k, v in LINES.items() if k != 'tax_report_number')
    monkeypatch.setattr(summary, 'extract_text_from_pdf', lambda path: text)
    monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf', lambda path: '')
    path, counts = summary.generate_summary_workbook(receipts, tmp_path / 'reports', 'synthetic', 2026)
    assert counts == {'warning': 1}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        row = dict(zip(summary.COLUMNS, next(wb['ינואר'].iter_rows(min_row=2, values_only=True))))
        assert row['severity'] == 'דורש תשומת לב'
        assert row['extraction_status'] == 'חלקי'
        assert row['notes'] == 'מספר אישור דיווח חסר — דרוש אימות'
        assert row['tax_report_number'] is None
    finally:
        wb.close()


@pytest.mark.parametrize('number', [*LONG_RECEIPT_NUMBERS, 'AB-001234567890/007'])
def test_workbook_round_trip_preserves_full_receipt_as_text(tmp_path, monkeypatch, number):
    receipts = tmp_path / 'receipts'
    month = receipts / 'synthetic' / '2026' / '01_January'
    month.mkdir(parents=True)
    (month / 'synthetic_9999.pdf').write_bytes(b'synthetic input; extraction is mocked')
    text = '\n'.join(v.format(year=2026) for k, v in LINES.items() if k != 'receipt_number')
    text += f'\nמספר קבלה: {number}'
    monkeypatch.setattr(summary, 'extract_text_from_pdf', lambda path: text)
    monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf', lambda path: '')
    path, _ = summary.generate_summary_workbook(receipts, tmp_path / 'reports', 'synthetic', 2026)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        cell = wb['ינואר'].cell(row=2, column=summary.COLUMNS.index('receipt_number') + 1)
        assert cell.value == number
        assert isinstance(cell.value, str)
        assert cell.data_type == 's'
        assert cell.number_format == '@'
        amount = wb['ינואר'].cell(row=2, column=summary.COLUMNS.index('amount') + 1)
        assert amount.value == 100
        assert amount.data_type == 'n'
    finally:
        wb.close()
