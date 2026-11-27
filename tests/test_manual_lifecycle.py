from contextlib import closing
import json

from openpyxl import load_workbook
import pytest

from src import receipt_summary
from src.manual_receipts import diagnose_review, run_manual
from src.receipt_metadata_extractor import extract_metadata, receipt_numbers_in_text, IDENTIFIER_REVIEW_NOTES
from src.sync_state import digest, refresh_selected_identifiers
from src.user_scope import annual_report
from tests.test_manual_review_quality import readers
from tests.test_sync import env, total
from tests.test_user_scope import register


PREFIX = 'עמותה סינתטית\n24/01/2026\n₪ 100\n'
TEMPLATE = 'קבלה - עותק\nתודה על תרומתך\nמספר מוסד: 580000019\nמספר: AB/001237'


@pytest.mark.parametrize('text', [
    PREFIX + TEMPLATE,
    PREFIX + 'הלבק - קתוע\nתרומה\n580000019 : דסומ רפסמ\nAB/001237 : רפסמ',
])
def test_institution_serial_and_registry_anchor(tmp_path, text):
    meta = extract_metadata('580000111\n' + text, tmp_path / 'synthetic.pdf')
    assert (meta.receipt_number, meta.registration_number) == ('001237', '580000019')
    assert '001237' in receipt_numbers_in_text(text)


@pytest.mark.parametrize('text', [
    'תאריך: 12/001237\nמספר מוסד: 580000019\nקבלה',
    'מספר: AB/001237\nחשבונית\nמספר מוסד: 580000019',
    'מספר: AB/001237\nקבלה\nתאריך: 24/01/2026',
])
def test_slash_rule_requires_receipt_scope(tmp_path, text):
    assert extract_metadata(text, tmp_path / 'synthetic.pdf').receipt_number != '001237'


@pytest.mark.parametrize('header', [
    'קבלה מספר - 38416\nעותק',
    'עותק 38416 - רפסמ הלבק',
    '38416 - הלבק רפסמ\nקתוע',
])
def test_copy_receipt_separates_tax_confirmation(monkeypatch, tmp_path, header):
    p = tmp_path / 'fixture.pdf'
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', lambda _: PREFIX + 'קבלה על תרומה\n' + header)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', lambda _: PREFIX + 'קבלה על תרומה\n' + header +
                        '\n9841 : םיסמה תושרל המורתה חוויד רושיא רפסמ')
    meta = receipt_summary._extract_row(p, 'manual', None, 2026)
    assert (meta.receipt_number, meta.tax_report_number) == ('38416', '9841')
    assert not any(note in meta.notes for note in IDENTIFIER_REVIEW_NOTES.values())


def setup(env, monkeypatch):
    project, registry, _, _ = env
    scope = register(project, 'alpha')
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))
    inbox = scope.receipts / 'manual/inbox'
    inbox.mkdir(parents=True)
    return scope, inbox


def test_reconcile_legacy_copies_and_duplicate_queue(env, monkeypatch):
    scope, inbox = setup(env, monkeypatch)
    readers(monkeypatch, PREFIX + TEMPLATE)
    raw = b'%PDF-1.4 synthetic receipt'
    key = digest(raw)
    (inbox / 'new.pdf').write_bytes(raw)
    assert run_manual(scope, 2026)['published'] == 1
    review = inbox.parent / 'review'
    legacy_copy = review / f'{key}.pdf'
    legacy_copy.write_bytes(raw)
    unverified = review / ('a' * 64 + '.pdf')
    unverified.write_bytes(b'unverified')
    assert run_manual(scope, 2026)['manual_review_copies_removed'] == 1
    assert not legacy_copy.exists() and unverified.is_file()
    for _ in range(2):
        (inbox / 'duplicate.pdf').write_bytes(raw)
        assert run_manual(scope, 2026)['published'] == 0
    duplicate = review / f'{key}.duplicate.pdf'
    assert duplicate.read_bytes() == raw and sorted(review.iterdir()) == sorted([duplicate, unverified])
    path = annual_report(scope.reports, 'manual', 2026)
    with closing(load_workbook(path)) as wb:
        assert total(wb) == 100
        rows = list(wb['בדיקת מסמכים ידניים'].values)[1:]
        assert len(rows) == 1 and rows[0][2] == 'duplicate_manual_receipt' and rows[0][4] == key


def test_generated_copy_with_distinct_hash_routes_to_review(env, monkeypatch):
    scope, inbox = setup(env, monkeypatch)
    readers(monkeypatch, PREFIX + TEMPLATE)
    (inbox / 'original.pdf').write_bytes(b'%PDF-1.4 synthetic original')
    assert run_manual(scope, 2026)['published'] == 1
    (inbox / 'copy.pdf').write_bytes(b'%PDF-1.4 synthetic copy')
    assert run_manual(scope, 2026)['manual_duplicates'] == 1
    readers(monkeypatch, PREFIX + TEMPLATE.replace('001237', '001238'))
    (inbox / 'different.pdf').write_bytes(b'%PDF-1.4 distinct donation')
    assert run_manual(scope, 2026)['published'] == 1
    with closing(load_workbook(annual_report(scope.reports, 'manual', 2026))) as wb:
        assert total(wb) == 200


def test_retry_unsupported_and_propose_published_identifier_without_overwrite(env, monkeypatch):
    scope, inbox = setup(env, monkeypatch)
    readers(monkeypatch, PREFIX + 'קבלה על תרומה\nעמותה רשומה: 580000019')
    first = b'%PDF-1.4 accepted earlier'
    (inbox / 'published.pdf').write_bytes(first)
    run_manual(scope, 2026)
    readers(monkeypatch, 'Synthetic nonreceipt')
    second = b'%PDF-1.4 earlier unsupported'
    (inbox / 'unsupported.pdf').write_bytes(second)
    run_manual(scope, 2026)
    readers(monkeypatch, PREFIX + TEMPLATE)
    diagnostic = list(diagnose_review(scope, 2026))
    assert diagnostic[0]['extractors']['pypdf']['receipt_number'] == '001237'
    assert diagnostic[0]['result'] == 'publishable' and len(diagnostic) == 2
    # The published receipt's label can be recovered with a protected proposal.
    proposals = refresh_selected_identifiers(scope.receipts, scope.reports, scope.accounts,
        'manual', scope.runtime, 2026, [digest(first)], preview=True)
    assert proposals[0]['proposals']['receipt_number']['value'] == '001237'
    readers(monkeypatch, PREFIX + TEMPLATE.replace('001237', '001238'))
    result = run_manual(scope, 2026, retry_review=True)
    assert result['manual_review_promoted'] == 1
    assert result['manual_review_copies_removed'] == 1
    assert not (inbox.parent / 'review' / f'{digest(second)}.pdf').exists()


def test_interrupted_review_cleanup_recovers_after_catalog_save(env, monkeypatch):
    scope, inbox = setup(env, monkeypatch)
    readers(monkeypatch, 'Synthetic nonreceipt')
    raw = b'%PDF-1.4 pending review'
    key = digest(raw)
    (inbox / 'submitted.pdf').write_bytes(raw)
    run_manual(scope, 2026)
    review = inbox.parent / 'review' / f'{key}.pdf'
    readers(monkeypatch, PREFIX + TEMPLATE)
    original_unlink = type(review).unlink

    def interrupt(path, *args, **kwargs):
        if path == review:
            raise OSError('interrupted before removing the old review copy')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(review), 'unlink', interrupt)
        with pytest.raises(OSError, match='interrupted'):
            run_manual(scope, 2026, retry_review=True)
    assert review.read_bytes() == raw
    result = run_manual(scope, 2026, retry_review=True)
    assert result['manual_review_promoted'] == 0 and result['manual_review_copies_removed'] == 1
    assert not review.exists()
    with closing(load_workbook(annual_report(scope.reports, 'manual', 2026))) as wb:
        assert total(wb) == 100
