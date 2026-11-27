from datetime import date

import pytest
from openpyxl import Workbook

from src import receipt_summary
from src.manual_receipts import _inspect
from src.receipt_metadata_extractor import INSTITUTION_AMOUNT_REVIEW, IDENTIFIER_REVIEW_NOTES
from src.sync_workbook import refresh_published_rows
from tests.test_observed_receipt_layouts import A_PYPDF
from tests.test_sync import env


@pytest.mark.parametrize('lines, expected, accepted', [
    ('תאריך קבלה: 6/3/2026\nתאריך הדפסה: 07/03/2026', '06/03/2026', True),
    ('06/03/2026\nתאריך הדפסה: 07/03/2026', '06/03/2026', True),
    ('6/3/2026\n06.03.2026', '06/03/2026', True),
    ('06/03/2026\n07/03/2026', '', False),
    ('תאריך קבלה: 06/03/2026\nתאריך קבלה: 07/03/2026', '', False),
    ('תאריך הדפסה: 07/03/2026', '', False),
])
def test_manual_rivhit_acceptance_requires_verified_date(tmp_path, monkeypatch, lines, expected, accepted):
    raw = A_PYPDF.replace('24/01/2026\n', '').replace(
        '                    מספר\n',
        '                    מספר\nשקל חדש\n475.00\n:סה"כ475.00\n')
    raw = lines + '\n' + raw
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', lambda _: raw.replace(':סה"כ475.00', ''))
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', lambda _: raw)
    pdf = tmp_path / 'anonymous.pdf'
    payload = b'%PDF-1.4 synthetic'
    pdf.write_bytes(payload)
    meta, reason = _inspect(pdf, payload, 2026)
    assert (meta.amount, meta.receipt_date) == ('475.00', expected)
    assert reason == (None if accepted else 'missing_or_ambiguous_date_or_amount')


def test_published_warning_refresh_respects_proofs_and_unresolved_duplicates():
    from types import SimpleNamespace

    ws = Workbook().active
    labels = receipt_summary.COLUMN_HEADERS_HE
    fields = ('receipt_number', 'tax_report_number', 'registration_number', 'organization_name',
              'receipt_date', 'amount', 'notes', 'donor_id', 'donor_match', 'severity', 'extraction_status')
    ws.append([labels[field] for field in fields])
    warning = 'חשד לכפילות — נדרשת בדיקה ידנית'
    historical = 'הערה שהוסיף המשתמש'
    notes = '; '.join(('שדה חובה חסר: מספר קבלה', 'מספר אישור דיווח חסר — דרוש אימות',
                       'תעודת זהות תורם לא תואמת', INSTITUTION_AMOUNT_REVIEW,
                       IDENTIFIER_REVIEW_NOTES['receipt_number'], warning, historical))
    ws.append(['001362', '3693', '580871803', 'Synthetic', date(2026, 3, 6), 475, notes,
               '828060214', 'אי התאמה', 'בעיה קריטית', 'לבדיקה'])
    entries = {'publication': {'documents': ['sha'], 'identifier_approvals': {'sha:receipt_number': 'proof'}}}
    records = {'documents': {'sha': {
        'verified_identifier_updates': {'receipt_number': {'value': '001362'}},
        'donation_amount_override': '475.00',
    }}}
    account = SimpleNamespace(expected_donor_ids=['828060214'], expected_donor_names=[])
    refresh_published_rows(entries, {'publication': (ws, 2)}, records, account, 2026)
    assert ws['G2'].value == warning + '; ' + historical
    assert ws['J2'].value == 'דורש תשומת לב'
    assert ws['K2'].value == 'חלקי'
    records['documents']['sha']['review_decision'] = {'action': 'distinct'}
    refresh_published_rows(entries, {'publication': (ws, 2)}, records, account, 2026)
    assert ws['G2'].value == historical
    assert ws['I2'].value == 'התאמה'
    assert ws['J2'].value == 'מוכן'
    assert ws['K2'].value == 'תקין'
    assert entries['publication']['identifier_approvals'] == {'sha:receipt_number': 'proof'}
    assert records['documents']['sha']['donation_amount_override'] == '475.00'




def test_rivhit_automatic_intake_publishes_without_override(env, monkeypatch):
    from contextlib import closing
    import json

    from src.manual_receipts import run_manual
    from src.sync_workbook import load, metadata
    from src.user_scope import annual_report
    from tests.test_manual_lifecycle import setup

    scope, inbox = setup(env, monkeypatch)
    raw = A_PYPDF.replace('24/01/2026\n', '').replace(
        '                    מספר\n',
        '                    מספר\nשקל חדש\n475.00\n:סה"כ475.00\n')
    raw = 'תאריך קבלה: 6/3/2026\nתאריך הדפסה: 07/03/2026\n' + raw
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', lambda _: raw)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', lambda _: raw)
    (inbox / 'automatic.pdf').write_bytes(b'%PDF-1.4 synthetic')

    assert run_manual(scope, 2026)['published'] == 1
    with closing(load(annual_report(scope.reports, 'manual', 2026))) as workbook:
        published = metadata(workbook['מרץ'], 2)
        assert (published.receipt_number, published.receipt_date, published.amount) == ('007314', '06/03/2026', '475')
    entry, = json.loads((scope.runtime / 'manual-intake.json').read_bytes())['documents'].values()
    assert 'donation_amount_override' not in entry
    assert 'donation_date_override' not in entry
    assert 'review_decision' not in entry
