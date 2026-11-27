from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook
import pymupdf

from src import main, pdf_text_extractor, potential_receipts
from src.combined_report import _PROVENANCE
from src.manual_receipts import run_manual
from src.sync_state import digest
from src.sync_workbook import LIVE_HEADERS, donation_sheets, headers
from src.user_scope import annual_report
from tests.test_potential_receipts import detail, request
from tests.test_sync import env, mail, total
from tests.test_user_scope import register


def pdf(number='71234', amount=100, date='21/03/2026', extra=''):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_htmlbox(page.rect,
            f'עמותה סינתטית<br>עמותה רשומה: 580000001<br>קבלה מספר: {number}<br>'
            f'{date}<br>₪{amount}<br>Donation receipt<br>{extra}')
        return doc.tobytes()


def real_extractors(monkeypatch):
    for module, name in [('src.receipt_summary', 'extract_text_from_pdf'),
                         ('src.receipt_summary', 'extract_text_from_pdf_pymupdf'),
                         ('src.pdf_donation_validator', 'extract_text_from_pdf_bytes')]:
        monkeypatch.setattr(f'{module}.{name}', getattr(pdf_text_extractor, name))


def test_gmail_potential_manual_combined_and_incremental_isolation(env, monkeypatch):
    project, registry, provider, _ = env
    real_extractors(monkeypatch)
    a = register(project, 'alpha')
    b = register(project, 'beta')
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))
    monkeypatch.setattr(main.UserScope, 'open', lambda user: {'alpha': a, 'beta': b}[user])
    provider.account_dir = a.account('primary')
    monkeypatch.setattr(main, 'GmailProvider', lambda *args, **kwargs: provider)

    def cli(mode, *options, user='alpha'):
        monkeypatch.setattr('sys.argv', ['src.main', '--mode', mode, '--user', user, '--year', '2026', *options])
        main.main()

    gmail_pdf = pdf()
    mail(provider, gmail_pdf)
    cli('full', '--account', 'primary')
    source = annual_report(a.reports, 'primary', 2026)
    with closing(load_workbook(source)) as wb:
        assert total(wb) == 100

    users = Mock()
    users.getProfile.return_value = request({'emailAddress': 'alpha@example.test'})
    users.messages().list.return_value = request({'messages': [{'id': 'aa'}]})
    users.messages().get.return_value = request(detail('aa', '<p>קבלה מספר 90123</p><button>לצפייה</button>'))
    monkeypatch.setattr(potential_receipts, 'build', lambda *a, **kw: SimpleNamespace(users=lambda: users))
    monkeypatch.setattr(potential_receipts, 'get_gmail_credentials', lambda _: None)
    cli('potential-receipts', '--account', 'primary')
    assert not (a.runtime / 'sync-manual.json').exists()
    potential = a.reports / 'manual/2026/potential_receipts_2026.xlsx'
    with closing(load_workbook(potential)) as wb:
        assert wb.active['G2'].value == 'ממתין לבדיקה'

    inbox = a.receipts / 'manual/inbox'
    inbox.mkdir(parents=True)
    independent = pdf('90123', 75, '20/04/2026')
    (inbox / 'same-donation-different-pdf.pdf').write_bytes(pdf(extra='Duplicate copy'))
    (inbox / 'independent.pdf').write_bytes(independent)
    annual = pdf('80000', 900, extra='Annual donation summary')
    (inbox / 'annual-reference.pdf').write_bytes(annual)
    ambiguous = pdf('80322', 900, extra='קבלה מספר: 80444')
    (inbox / 'multi-receipt.pdf').write_bytes(ambiguous)
    cli('manual')
    manual = annual_report(a.reports, 'manual', 2026)
    with closing(load_workbook(manual)) as wb:
        assert total(wb) == 175
        assert {row[2] for row in list(wb['בדיקת מסמכים ידניים'].values)[1:]} == {
            'annual_reference_document', 'ambiguous_receipt_identity'}
    assert (a.receipts / f'manual/2026/04_April/{digest(independent)}.pdf').read_bytes() == independent
    assert (a.receipts / f'manual/review/{digest(annual)}.pdf').read_bytes() == annual
    cli('combined')
    combined = annual_report(a.reports, 'combined', 2026)
    with closing(load_workbook(combined)) as wb:
        assert total(wb) == 175
        assert wb[_PROVENANCE].max_row == 4
        for ws in donation_sheets(wb):
            if ws.max_row > 1:
                col = headers(ws)['שם קובץ']
                assert ws.cell(2, col).hyperlink.target.startswith(str(a.receipts))
                assert ws.cell(2, headers(ws)[LIVE_HEADERS[0]]).value == 'מאושר'
    (inbox / 'gmail-copy.pdf').write_bytes(gmail_pdf)
    cli('manual')
    cli('combined')
    with closing(load_workbook(combined)) as wb:
        assert total(wb) == 175
        assert wb[_PROVENANCE].max_row == 4
    before = combined.read_bytes()
    cli('manual')
    cli('combined')
    assert combined.read_bytes() == before

    # A verified correction in the source workbook remains effective on rebuild.
    with closing(load_workbook(manual)) as wb:
        ws = wb['אפריל']
        amount_col = headers(ws)['סכום']
        ws.cell(2, amount_col).value = 80
        wb.save(manual)
    new_pdf = pdf('91122', 25, '22/05/2026')
    (inbox / 'new.pdf').write_bytes(new_pdf)
    assert run_manual(a, 2026)['manual_ingested'] == 1
    cli('combined')
    with closing(load_workbook(combined)) as wb:
        assert total(wb) == 205
    cli('combined', user='beta')
    with closing(load_workbook(annual_report(b.reports, 'combined', 2026))) as wb:
        assert total(wb) == 0
    with closing(load_workbook(potential)) as wb:
        assert wb.active['G2'].value == 'ממתין לבדיקה'


def test_manual_interruption_keeps_the_original_and_recovers_once(env, monkeypatch):
    project, registry, _, _ = env
    real_extractors(monkeypatch)
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))
    scope = register(project, 'alpha')
    inbox = scope.receipts / 'manual' / 'inbox'
    inbox.mkdir(parents=True)
    source = inbox / 'receipt.pdf'
    raw = pdf()
    source.write_bytes(raw)
    from src import manual_receipts
    original = manual_receipts.atomic_json

    def interrupt(path, data):
        if path.name == 'manual-intake.json':
            raise OSError('Simulated interruption')
        original(path, data)

    with monkeypatch.context() as temporary:
        temporary.setattr(manual_receipts, 'atomic_json', interrupt)
        with pytest.raises(OSError):
            run_manual(scope, 2026)
    assert source.read_bytes() == raw
    assert run_manual(scope, 2026)['manual_ingested'] == 1
    assert not source.exists()
    with closing(load_workbook(annual_report(scope.reports, 'manual', 2026))) as wb:
        assert total(wb) == 100


def test_combined_preserves_distinct_decision_with_old_shared_identity(env, monkeypatch):
    from dataclasses import replace
    from src import combined_report
    from src.receipt_metadata_extractor import ReceiptMetadata

    project, _, _, _ = env
    scope = register(project, 'alpha')
    original = scope.receipts / 'primary' / 'original.pdf'
    copied = scope.receipts / 'manual' / 'copy.pdf'
    for path in (original, copied):
        path.parent.mkdir(parents=True)
        path.write_bytes(pdf(extra=path.name))
    meta = ReceiptMetadata(receipt_number='1234', registration_number='580000001',
                           receipt_date='22/03/2026', amount='100')
    a = combined_report.PublishedDonation('primary', 'source-a', meta, 'מאושר', set(),
                                          frozenset({digest(original.read_bytes())}), (original,), False)
    m = combined_report.PublishedDonation('manual', 'source-m', replace(meta), 'מאושר', set(),
                                          frozenset({digest(copied.read_bytes())}), (copied,), False)
    monkeypatch.setattr(combined_report, '_read', lambda _, account, year: ([a if account == 'primary' else m], []))
    assert combined_report.run_combined(scope, 2026)['combined_donations'] == 1
    path = annual_report(scope.reports, 'combined', 2026)
    with closing(load_workbook(path)) as wb:
        old_id = wb[_PROVENANCE]['A2'].value
    m.distinct = True
    assert combined_report.run_combined(scope, 2026)['combined_donations'] == 2
    with closing(load_workbook(path)) as wb:
        assert total(wb) == 200
        assert wb[_PROVENANCE]['A2'].value == old_id
        assert wb[_PROVENANCE]['A3'].value != old_id
    stable = path.read_bytes()
    combined_report.run_combined(scope, 2026)
    assert path.read_bytes() == stable


def test_ambiguous_secondary_does_not_remove_existing_source_totals(env, monkeypatch):
    from dataclasses import replace
    from src import combined_report
    from src.receipt_metadata_extractor import ReceiptMetadata

    project, _, _, _ = env
    scope = register(project, 'alpha')
    meta = ReceiptMetadata(receipt_number='1234', registration_number='580000001',
                           receipt_date='22/03/2026', amount='100')
    def source(account, key, metadata):
        path = scope.receipts / account / f'{key}.pdf'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf(extra=key))
        return combined_report.PublishedDonation(account, key, metadata, 'מאושר', set(),
            frozenset({digest(path.read_bytes())}), (path,), False)
    primary = [source('primary', 'a', meta), source('primary', 'b', replace(meta, amount='80'))]
    manual = [source('manual', 'm', replace(meta, amount='200'))]
    monkeypatch.setattr(combined_report, '_read',
                        lambda _, account, year: (primary if account == 'primary' else manual, []))
    assert combined_report.run_combined(scope, 2026) == {'combined_donations': 2, 'combined_review': 3}
    with closing(load_workbook(annual_report(scope.reports, 'combined', 2026))) as wb:
        assert total(wb) == 180
