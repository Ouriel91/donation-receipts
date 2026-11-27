from contextlib import closing
import json

from openpyxl import load_workbook
import pytest

from src import receipt_summary
from src.manual_receipts import _inspect, diagnose_review, run_manual
from src.sync_state import digest
from src.user_scope import annual_report
from tests.test_sync import env, total
from tests.test_user_scope import register


def readers(monkeypatch, text):
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', lambda _: text)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', lambda _: text)


BASE = 'עמותה סינתטית\nעמותה רשומה: 580000001\n24/01/2026\n'


@pytest.mark.parametrize(('lines', 'reason'), [
    ('קבלה על תרומה\n₪ 100', None),
    ('קבלה על תרומה 38416\n₪ 100\nהקצאה מספר: 9841\nפרטי התרומה דווחו לרשות המסים – אישור מספר: 672', None),
    ('קבלה על תרומה 38416\n₪ 100\nפרטי התרומה דווחו לרשות המסים – אישור מספר: 672', None),
    ('קבלה מספר: 38416\nקבלה מספר: 73815\nתרומה\n₪ 100', 'ambiguous_receipt_identity'),
    ('קבלה על תרומה 38416\n₪ 100\n₪ 75', 'ambiguous_donation_amount'),
    ('קבלה על תרומה 38416\nסה"כ ₪ 100\nפריט ₪ 75', None),
    ('Annual donation summary\nDonation receipt\n₪ 100', 'annual_reference_document'),
    ('Donate now — download your donation receipt\n₪ 100', 'unsupported_document'),
])
def test_manual_uses_authoritative_receipt_conflict(monkeypatch, tmp_path, lines, reason):
    readers(monkeypatch, BASE + lines)
    path = tmp_path / 'fixture.pdf'
    raw = b'%PDF-1.4 synthetic'
    path.write_bytes(raw)
    flags = {}
    meta, result = _inspect(path, raw, 2026, flags)
    assert result == reason
    if 'הקצאה מספר' in lines:
        assert meta.receipt_number == '38416'
        assert meta.tax_report_number == ''
        assert flags['old_identifier_note'] and flags['tax_conflict'] and not flags['receipt_conflict']
    elif 'אישור מספר: 672' in lines:
        assert (meta.receipt_number, meta.tax_report_number) == ('38416', '672')


def test_diagnose_and_retry_existing_review_without_moving_source(env, monkeypatch):
    project, registry, _, _ = env
    scope = register(project, 'alpha')
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))
    readers(monkeypatch, BASE + 'קבלה על תרומה\n₪ 100')
    raw = b'%PDF-1.4 synthetic original'
    key = digest(raw)
    review = scope.receipts / 'manual' / 'review' / f'{key}.pdf'
    review.parent.mkdir(parents=True)
    review.write_bytes(raw)
    state_path = scope.runtime / 'manual-intake.json'
    state_path.parent.mkdir(parents=True)
    entry = {'metadata': {'file_name': 'receipt.pdf'}, 'paths': [f'review/{key}.pdf'],
             'logical_id': None, 'stage': 'review', 'reason': 'ambiguous_receipt_identity'}
    state_path.write_text(json.dumps({'version': 1, 'documents': {key: entry}}))
    before = state_path.read_bytes()
    evidence, = diagnose_review(scope, 2026)
    assert evidence['old_receipt_set_mismatch'] and evidence['result'] == 'publishable'
    assert evidence['sha256_prefix'] == key[:16]
    assert state_path.read_bytes() == before and not scope.reports.exists()
    assert run_manual(scope, 2026, retry_review=True)['manual_review_promoted'] == 1
    assert not review.exists()
    assert (scope.receipts / 'manual/2026/01_January' / f'{key}.pdf').read_bytes() == raw
    assert run_manual(scope, 2026, retry_review=True)['manual_review_promoted'] == 0
    with closing(load_workbook(annual_report(scope.reports, 'manual', 2026))) as wb:
        assert total(wb) == 100


def test_retry_keeps_original_name_for_remaining_review(env, monkeypatch):
    project, registry, _, _ = env
    scope = register(project, 'alpha')
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))
    readers(monkeypatch, 'Donate now — download your donation receipt')
    raw = b'%PDF-1.4 synthetic notice'
    key = digest(raw)
    review = scope.receipts / 'manual/review' / f'{key}.pdf'
    review.parent.mkdir(parents=True)
    review.write_bytes(raw)
    state_path = scope.runtime / 'manual-intake.json'
    state_path.parent.mkdir(parents=True)
    original = 'original-filename.pdf'
    entry = {'metadata': {'file_name': original}, 'paths': [f'review/{key}.pdf'],
             'logical_id': None, 'stage': 'review', 'reason': 'ambiguous_receipt_identity'}
    state_path.write_text(json.dumps({'version': 1, 'documents': {key: entry}}))
    assert run_manual(scope, 2026, retry_review=True)['manual_review_promoted'] == 0
    result = json.loads(state_path.read_bytes())['documents'][key]
    assert result['metadata']['file_name'] == original and result['reason'] == 'unsupported_document'
    assert review.read_bytes() == raw
