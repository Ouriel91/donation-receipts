from decimal import Decimal

import pytest

from src import receipt_summary
from src.manual_receipts import _inspect
from src.receipt_metadata_extractor import INSTITUTION_AMOUNT_REVIEW, _payment_totals
from tests.test_observed_receipt_layouts import A_PYPDF


@pytest.mark.parametrize('total', ['1,250.00', '475.00', '625.00'])
@pytest.mark.parametrize('conflict', [False, True])
def test_compact_rivhit_total_through_both_readers(tmp_path, monkeypatch, total, conflict):
    # Retain the observed heading, separate currency, standalone payment
    # value, and colon-before-label with a directly attached total.
    pymupdf = (
        '580871803 מספר מוסד\n'
        'קבלת תרומה15/007314 :מספר\nמקור\n'
        '24/01/2026\n'
        'תיאור פריט כמות ש"ח ליחידה סה"כ ש"ח\n'
        '1.00\n'
        '15/007314 :פרטי תשלום: קבלת תרומה                    מספר\n'
        'שקל חדש\n'
        f'{total}\n:סה"כ{total}\n'
        + (':סה"כ125.00\n' if conflict else '')
        + '27 פרטי התרומה דווחו לרשות המיסים – אישור מספר'
    )
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', lambda _: A_PYPDF)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', lambda _: pymupdf)
    pdf = tmp_path / 'anonymous.pdf'
    contents = b'%PDF-1.4 synthetic'
    pdf.write_bytes(contents)

    expected = Decimal(total.replace(',', ''))
    totals = {expected, Decimal('125.00')} if conflict else {expected}
    assert _payment_totals([pymupdf], '15/007314') == totals

    for meta in (receipt_summary._extract_row(pdf, 'manual', None, 2026),
                 _inspect(pdf, contents, 2026)[0]):
        assert (meta.receipt_number, meta.tax_report_number) == ('007314', '27')
        assert meta.payment_total_verified is not conflict
        assert meta.amount == ('' if conflict else str(expected))
        assert (INSTITUTION_AMOUNT_REVIEW in meta.notes) is conflict

    _, reason = _inspect(pdf, contents, 2026)
    assert reason == ('ambiguous_donation_amount' if conflict else None)
