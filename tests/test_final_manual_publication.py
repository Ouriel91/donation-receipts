from contextlib import closing
import json

import pytest

from src.manual_receipts import _inspect, run_manual
from src.manual_exclusion import record_exclusion
from src.combined_report import run_combined
from src.receipt_metadata_extractor import extract_metadata
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.sync_state import digest, refresh_selected_identifiers, review_identifiers
from src.sync_workbook import load, headers
from src.user_scope import annual_report
from tests.test_manual_lifecycle import setup
from tests.test_manual_review_quality import readers
from tests.test_observed_receipt_layouts import A_PYPDF, B_END, B_PYPDF, C, C_END
from tests.test_sync import env, total
from pathlib import Path


@pytest.mark.parametrize('value', ['1,000.00', '500.00', '600.00'])
def test_rivhit_payment_total_ignores_quantity_and_keeps_short_tax(tmp_path, monkeypatch, value):
    raw = A_PYPDF.replace('20 ', '27 ').replace('15/007314 :פרטי תשלום: קבלת תרומה                    מספר',
        '15/007314 :פרטי תשלום: קבלת תרומה                    מספר\nשקל חדש\nסה"כ\n' + value)
    readers(monkeypatch, raw)
    path = tmp_path/'test.pdf'
    path.write_bytes(b'%PDF-1.4 synthetic')
    meta, reason = _inspect(path, path.read_bytes(), 2026)
    assert reason is None
    assert (meta.receipt_number, meta.amount, meta.tax_report_number) == ('007314', value.replace(',', ''), '27')


def test_rivhit_conflicting_payment_totals_stay_in_review():
    raw = A_PYPDF.replace('                    מספר', '                    מספר\nשקל חדש\nסה"כ 500.00\nסה"כ 100.00')
    assert not extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw).amount


def test_explicit_nine_digit_receipt_outweighs_donor_heuristic():
    raw = 'זהות 853986419\n' + C + C_END
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw)
    assert (meta.receipt_number, meta.donor_id, meta.tax_report_number) == ('853986419', '', '7310')
    raw += '\nמספר זהות: 828060214'
    assert extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw).donor_id == '828060214'


def test_exclusion_persists_and_combined_keeps_primary(env, monkeypatch):
    scope, inbox = setup(env, monkeypatch)
    raw = b'%PDF-1.4 synthetic receipt'
    readers(monkeypatch, B_PYPDF + B_END)
    (inbox/'test.pdf').write_bytes(raw)
    run_manual(scope, 2026)
    # This receipt remains present as a separate primary publication.
    from src.sync import run_sync
    primary_pdf = scope.receipts/'primary/2026/07_July/test.pdf'
    primary_pdf.parent.mkdir(parents=True)
    primary_pdf.write_bytes(raw)
    for _ in range(2):
        run_sync(provider=None, receipts_dir=scope.receipts, reports_dir=scope.reports,
            accounts_dir=scope.accounts, manifest_path=scope.manifest, account='primary', year=2026,
            root=scope.runtime, public_registry=scope.registry, full_report=True,
            resume_pending=True, refresh_base=True)
    primary = annual_report(scope.reports,'primary',2026)
    with closing(load(primary)) as workbook:
        assert workbook['סיכום 2026']['A1'].value == 'Donation Receipts annual summary v1'
    primary_before = primary.read_bytes()
    record_exclusion(scope, 2026, digest(raw), True, 'Verified primary duplicate')
    run_manual(scope, 2026, retry_review=True, fresh=True)
    with closing(load(annual_report(scope.reports,'manual',2026))) as workbook:
        assert total(workbook) == 0
        assert workbook['יולי'].row_dimensions[2].hidden
    assert run_combined(scope, 2026)['combined_donations'] == 1
    assert primary.read_bytes() == primary_before
    record_exclusion(scope, 2026, digest(raw), False, 'Reversal check')
    run_manual(scope, 2026)
    with closing(load(annual_report(scope.reports,'manual',2026))) as workbook:
        assert total(workbook) == 200
        assert not workbook['יולי'].row_dimensions[2].hidden
    assert run_combined(scope, 2026)['combined_donations'] == 1
    state = json.loads((scope.runtime/'sync-manual.json').read_bytes())
    history, = state['manual_exclusion_history'].values()
    assert [e['excluded'] for e in history] == [True, False]
    assert (scope.receipts/'manual/2026/07_July'/f'{digest(raw)}.pdf').read_bytes() == raw


def test_visual_payment_total_and_tax_label_without_colon():
    raw = A_PYPDF.replace('                    מספר', '                    מספר\n500.00 שקל חדש\nסה"כ')
    raw = raw.replace('27 פרטי התרומה דווחו לרשות המיסים – אישור מספר',
                      'פרטי התרומה דווחו לרשות המיסים – אישור מספר 38')
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw)
    assert (meta.amount, meta.tax_report_number) == ('500.00', '38')


def test_published_donor_correction_and_tax_clear_are_protected(env, monkeypatch):
    scope, inbox = setup(env, monkeypatch)
    old_text = ('קבלה מספר: 92280\nתרומה\nעמותה רשומה: 580472638\n'
                'תאריך 28/03/2026\n₪250.00\nמספר זהות: 853986419\n'
                'מספר אישור דיווח התרומה לרשות המסים: 1')
    readers(monkeypatch, old_text)
    raw = b'%PDF-1.4 synthetic prior publication'
    (inbox/'test.pdf').write_bytes(raw)
    assert run_manual(scope, 2026)['published'] == 1
    sha = digest(raw)
    readers(monkeypatch, C.split('\nמספר אישור')[0] + C_END + '\nזהות 853986419')
    args = scope.receipts, scope.reports, scope.accounts, 'manual', scope.runtime, 2026, [sha]
    proposal, = refresh_selected_identifiers(*args)
    assert proposal['proposals']['donor_id']['value'] == ''
    assert proposal['proposals']['receipt_number']['value'] == '853986419'
    decisions = [(sha, field, change['value'], change['published_before'])
                 for field, change in proposal['proposals'].items()]
    review_identifiers(scope.receipts, 'manual', scope.runtime, 2026, decisions,
        reports_dir=scope.reports, clear_manual_tax=[(sha, proposal['publication_id'], '1', 'Verified absent')])
    assert run_manual(scope, 2026)['corrections_published'] == 3
    with closing(load(annual_report(scope.reports,'manual',2026))) as workbook:
        assert total(workbook) == 250
        from src.sync_workbook import metadata
        row = metadata(workbook['מרץ'], 2)
        assert (row.receipt_number, row.donor_id, row.tax_report_number) == ('853986419', '', '')
    run_manual(scope, 2026, fresh=True)


def test_reporting_number_is_not_an_amount_after_empty_payment_total():
    raw = A_PYPDF.replace('                    מספר', '                    מספר\nשקל חדש\nסה"כ')
    meta = extract_metadata(normalize_hebrew_text(raw), Path('test.pdf'), raw)
    assert not meta.amount and not meta.payment_total_verified
    assert meta.tax_report_number == '27'
