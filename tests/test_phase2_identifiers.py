from pathlib import Path

import pytest

from src import receipt_summary
from tests.test_qa_fixes_2026 import document, env, put, report, rows, saved, synthetic_text
from src.receipt_metadata_extractor import (
    IDENTIFIER_REVIEW_NOTES,
    extract_metadata,
    receipt_numbers_in_text,
)


@pytest.mark.parametrize('heading', (
    'קבלה על תרומה 38416',
    'קבלה-תרומה 38416',
    'קבלה מספר - 38416',
    'קבלה מספר\n –\n 38416',
    'קבלה על תרומה מספר 38416',
    '38416 ־ רפסמ הלבק',
    '38416 מספר תרומה על קבלה',
    '38416 המורת־הלבק',
))
def test_receipt_heading_is_available_to_extraction_and_identity_checks(heading):
    assert extract_metadata(heading, Path('fixture.pdf')).receipt_number == '38416'
    assert receipt_numbers_in_text(heading) == {'38416'}


@pytest.mark.parametrize(('label', 'value'), (
    ('פרטי התרומה דווחו לרשות המיסים – אישור מספר 37', '37'),
    ('פרטי התרומה דווחו לרשות המסים\n– אישור מספר:\n 984135672', '984135672'),
    ('9841 : מספר אישור – המסים לרשות דווחו התרומה פרטי', '9841'),
    ('רפסמ האצקה:\n9841', '9841'),
    ('9841 : הקצאה מספר', '9841'),
    ('מספר אישור דיווח התרומה לרשות המסים\n: 9841', '9841'),
))
def test_tax_labels_never_supply_a_receipt_number(label, value):
    text = f'קבלה על תרומה מספר 38416\n{label}'
    meta = extract_metadata(text, Path('fixture.pdf'))
    assert (meta.receipt_number, meta.tax_report_number) == ('38416', value)
    assert receipt_numbers_in_text(text) == {'38416'}


def test_allocation_needs_donation_receipt_context():
    text = 'חשבונית מס\nקבלה מספר - 38416\nהקצאה מספר: 9841'
    meta = extract_metadata(text, Path('fixture.pdf'))
    assert (meta.receipt_number, meta.tax_report_number) == ('38416', '')


def test_conflicting_tax_labels_require_review():
    text = ('קבלה על תרומה 38416\nהקצאה מספר: 9841\n'
            'פרטי התרומה דווחו לרשות המסים – אישור מספר: 672')
    meta = extract_metadata(text, Path('fixture.pdf'))
    assert meta.receipt_number == '38416'
    assert not meta.tax_report_number
    assert IDENTIFIER_REVIEW_NOTES['tax_report_number'] in meta.notes


@pytest.mark.parametrize('indent', ('', '  '))
def test_missing_tax_value_does_not_consume_next_receipt_heading(indent):
    text = f'פרטי התרומה דווחו לרשות המסים – אישור מספר:\n{indent}38416 מספר תרומה על קבלה'
    meta = extract_metadata(text, Path('fixture.pdf'))
    assert (meta.receipt_number, meta.tax_report_number) == ('38416', '')


@pytest.mark.parametrize('swap', (False, True))
def test_reader_results_are_combined(monkeypatch, tmp_path, swap):
    receipt = 'קבלה מספר - 38416\n24/01/2026\n₪ 37.00'
    tax = 'פרטי התרומה דווחו לרשות המסים – אישור מספר 9841'
    engine_a, engine_b = (tax, receipt) if swap else (receipt, tax)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', lambda _: engine_a)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', lambda _: engine_b)
    meta = receipt_summary._extract_row(tmp_path / 'fixture.pdf', 'test-account', None, 2026)
    assert (meta.receipt_number, meta.tax_report_number) == ('38416', '9841')


def test_cached_correct_identifier_missing_from_publication_requires_approval(env, monkeypatch):
    from contextlib import closing
    import json

    from openpyxl import load_workbook

    from src import sync_state
    from src.sync_workbook import headers

    base, root = env
    source = put(base, document(), 'fixture.pdf')
    sha = sync_state.digest(source.read_bytes())

    def fresh(label):
        text = synthetic_text(source.read_bytes()).replace('קבלה מס 1001', label)
        for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
            monkeypatch.setattr(receipt_summary, reader, lambda _: text)

    fresh('קבלה מקור')
    saved(env)
    workbook = report(base)
    catalog = root / 'sync-primary.json'
    state = json.loads(catalog.read_bytes())
    assert state['documents'][sha]['metadata']['receipt_number'] == ''
    state['documents'][sha]['metadata']['receipt_number'] = '38416'
    catalog.write_text(json.dumps(state), encoding='utf-8')
    fresh('קבלה מספר - 38416')
    args = base/'receipts', base/'reports', base/'accounts', 'primary', root, 2026, [sha]
    before = workbook.read_bytes()
    preview, = sync_state.refresh_selected_identifiers(*args, preview=True)
    assert preview['proposals']['receipt_number'] == {
        'before': '38416', 'value': '38416', 'published_before': '',
    }
    sync_state.refresh_selected_identifiers(*args)
    assert workbook.read_bytes() == before
    assert not saved(env).get('corrections_published')
    sync_state.review_identifiers(
        base/'receipts', 'primary', root, 2026, [(sha, 'receipt_number', '38416', '')],
        reports_dir=base/'reports',
    )
    assert saved(env)['corrections_published'] == 1
    with closing(load_workbook(workbook)) as wb:
        (ws, row), = rows(wb)
        assert ws.cell(row, headers(ws)['מספר קבלה']).value == '38416'
    assert not saved(env).get('corrections_published')


def test_allocation_in_adjacent_invoice_section_is_ignored():
    text = 'קבלה על תרומה 38416\nחשבונית מס\nהקצאה מספר: 9841'
    meta = extract_metadata(text, Path('fixture.pdf'))
    assert (meta.receipt_number, meta.tax_report_number) == ('38416', '')


def test_legacy_wrapped_receipt_with_inline_number_label_is_preserved():
    text = 'קבלה-תרומה\n38416 מספר עסקה 9841'
    assert extract_metadata(text, Path('fixture.pdf')).receipt_number == '38416'
