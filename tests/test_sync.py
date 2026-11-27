"""Synthetic acceptance: real persistence/rendering/consolidation/enrichment.

Only Gmail, PDF text/metadata boundary, and official HTTP transport are faked.
No private runtime files or external services are accessed.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import openpyxl
import pytest

from src.organization_cache import OrganizationCache
from src.receipt_metadata_extractor import ReceiptMetadata
from src.receipt_summary import generate_summary_workbook
from src.section46_live import LiveSection46 as RealLive
from src.sync import publish, run_sync
from src.sync_workbook import ID_HEADER, INDEX, LIVE_HEADERS, OFFICIAL, SOURCES, headers, donation_sheets

NOW = datetime.now(timezone.utc)


def document(number='1001', amount='100', date='15/01/2026', variant='original'):
    return json.dumps({'receipt_number': number, 'amount': amount, 'receipt_date': date,
                       'registration_number': '580000001', 'organization_name': 'עמותה סינתטית',
                       'variant': variant}, ensure_ascii=False).encode()


def synthetic_text(raw):
    value = json.loads(raw)
    return ('עמותה סינתטית\nעמותה רשומה: 580000001\n'
            + 'קבלה מס ' + value['receipt_number'] + '\nתאריך ' + value['receipt_date']
            + '\nסה"כ ₪' + value['amount'] + '\nתרומה\n')


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'registry'
    root.mkdir()
    csv = tmp_path / 'synthetic.csv'
    csv.write_text('מספר עמותה,שם עמותה בעברית\n580000001,השם הרשמי הסינתטי\n', encoding='utf-8-sig')
    cache = OrganizationCache(root / 'organizations.json')
    cache.import_registry(csv)
    network = MagicMock(return_value=json.dumps({'Status': 200, 'Message': {
        'start_date': '2025-01-01', 'end_date': '2030-12-31', 'as_of_date': NOW.date().isoformat()}}).encode())
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf', lambda p: synthetic_text(p.read_bytes()))
    monkeypatch.setattr('src.receipt_summary.extract_text_from_pdf_pymupdf', lambda p: synthetic_text(p.read_bytes()))
    monkeypatch.setattr('src.pdf_donation_validator.extract_text_from_pdf_bytes', synthetic_text)
    monkeypatch.setattr('src.sync.LiveSection46', lambda path, registry: RealLive(
        path, registry, transport=network, clock=lambda: NOW, sleep=lambda n: None))
    provider = MagicMock()
    provider.account_dir = tmp_path / 'accounts' / 'primary'
    provider.fetch_emails.return_value = []
    return tmp_path, root, provider, network


def put(base, raw, arrival='01_January', year=2026, filename='old.pdf'):
    path = base / 'receipts/primary' / str(year) / arrival / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def mail(provider, raw, msg='new-message', date='2026-11-10', attachment_id='attachment'):
    provider.fetch_emails.return_value = [{
        'message_id': msg, 'subject': 'Donation receipt', 'body': 'Thank you for your donation',
        'date': date, 'attachments': [{'filename': 'receipt.pdf', 'attachment_id': attachment_id}]}]
    provider.download_attachment.side_effect = None
    provider.download_attachment.return_value = raw


def sync(env, year=2026, allowed_years=None):
    base, root, provider, _ = env
    return run_sync(provider=provider, receipts_dir=base / 'receipts', reports_dir=base / 'reports',
                    accounts_dir=base / 'accounts', manifest_path=base / 'manifest.json',
                    account='primary', year=year, root=root, allowed_years=allowed_years)


def report(base, year=2026):
    return base / f'reports/primary/{year}/donation_summary_{year}.xlsx'


def rows(wb):
    return [(ws, row) for ws in donation_sheets(wb) for row in range(2, ws.max_row + 1)
            if ws.cell(row, headers(ws)[ID_HEADER]).value]


def total(wb, label='שנתי'):
    return next(row[2] for row in wb['סיכום'].iter_rows(values_only=True) if row[0] == label)


def initial_workbook(env):
    base, _, _, _ = env
    old = put(base, document())
    path, _ = generate_summary_workbook(base / 'receipts', base / 'reports', 'primary', 2026)
    wb = openpyxl.load_workbook(path)
    sheet = next(donation_sheets(wb))
    cols = headers(sheet)
    sheet.cell(2, cols['סכום'], 125)  # Human correction differs from extraction.
    sheet.cell(2, cols['שם עמותה'], 'שם שתוקן ידנית')
    sheet.cell(2, cols['הערות'], 'תיקון ידני לשימור')
    wb.create_sheet('ביקורת קיימת')['A1'] = 'בדיקה ידנית היסטורית'
    wb.save(path)
    wb.close()
    return old, path.read_bytes()


def test_full_acceptance_historical_new_repeat_and_manual_corrections(env):
    base, root, provider, network = env
    original, before = initial_workbook(env)
    mail(provider, document('2002', '75', '21/03/2026'))
    result = sync(env)
    assert result['published'] == 1
    assert network.call_count == 1
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 2
    assert total(wb) == 200
    assert total(wb, '2026-01') == 125
    assert total(wb, '2026-03') == 75
    old_ws, old_row = rows(wb)[0]
    cols = headers(old_ws)
    assert old_ws.cell(old_row, cols['סכום']).value == 125
    assert old_ws.cell(old_row, cols['שם עמותה']).value == 'שם שתוקן ידנית'
    assert old_ws.cell(old_row, cols['הערות']).value == 'תיקון ידני לשימור'
    new_ws, new_row = rows(wb)[1]
    assert new_ws.cell(new_row, headers(new_ws)['שם עמותה']).value == 'השם הרשמי הסינתטי'
    assert new_ws.cell(new_row, headers(new_ws)[LIVE_HEADERS[0]]).value == 'מאושר'
    assert new_ws.title == 'מרץ'  # March donation, November Gmail arrival.
    assert wb['ביקורת קיימת']['A1'].value == 'בדיקה ידנית היסטורית'
    assert wb[INDEX].sheet_state == 'veryHidden'
    assert list(report(base).parent.glob('.backup-*.xlsx'))[0].read_bytes() == before
    assert original.read_bytes() == document()
    wb.close()
    published = report(base).read_bytes()
    result = sync(env)
    assert result['published'] == 0
    assert result['reports_updated'] == 0
    assert report(base).read_bytes() == published
    assert network.call_count == 1


def test_cross_run_copy_reuses_consolidation_and_keeps_all_links(env):
    base, _, provider, network = env
    initial_workbook(env)
    sync(env)
    mail(provider, document(variant='copy'), msg='another-message')
    result = sync(env)
    assert result['published'] == 0
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 1
    assert total(wb) == 125
    assert wb[SOURCES].max_row == 3  # Original and copy, one logical donation.
    assert network.call_count == 1
    assert len(json.loads(wb[INDEX]['B2'].value)['documents']) == 2
    wb.close()
    assert sync(env)['reports_updated'] == 0


def test_failed_publication_after_ingestion_recovers_and_cache_is_reused(env):
    base, root, provider, network = env
    _, original = initial_workbook(env)
    mail(provider, document('2002', '75', '21/03/2026'))
    with patch('src.sync_workbook.atomic_bytes', side_effect=PermissionError):
        with pytest.raises(PermissionError):
            sync(env)
    assert report(base).read_bytes() == original
    assert len(json.loads((base / 'manifest.json').read_text())) == 1
    state = json.loads((root / 'sync-primary.json').read_text())
    assert len(state['documents']) == 2
    assert not any('published' in entry for entry in state['documents'].values())
    assert network.call_count == 1
    assert sync(env)['published'] == 1
    assert network.call_count == 1
    assert sync(env)['published'] == 0
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 2
    assert total(wb) == 200
    wb.close()


def test_late_receipt_routes_to_other_year_and_historical_gap_unknown(env):
    base, _, provider, network = env
    initial_workbook(env)
    mail(provider, document('2020-1', '40', '03/02/2020'))
    result = sync(env, allowed_years=[2020, 2026])
    assert result['cross_year'] == 1
    current, previous = openpyxl.load_workbook(report(base)), openpyxl.load_workbook(report(base, 2020))
    assert len(rows(current)) == 1
    assert total(current) == 125
    assert total(previous) == 40
    ws, row = rows(previous)[0]
    assert ws.title == 'פברואר'
    assert 'לא ידוע' in ws.cell(row, headers(ws)[LIVE_HEADERS[0]]).value
    assert network.call_count == 1
    current.close()
    previous.close()
    assert sync(env, allowed_years=[2020, 2026])['published'] == 0
    assert network.call_count == 1


def test_user_amount_and_tax_corrections_and_relative_links_survive(env):
    base, _, provider, network = env
    initial_workbook(env)
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    ws, row = rows(wb)[0]
    cols = headers(ws)
    ws.cell(row, cols['סכום'], 130)
    ws.cell(row, cols[LIVE_HEADERS[0]], 'תיקון ידני — שמור')
    ws.cell(row, cols['שם קובץ']).hyperlink = '../../../../receipts/primary/2026/01_January/old.pdf'
    wb.save(report(base))
    wb.close()
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    ws, row = rows(wb)[0]
    assert ws.cell(row, headers(ws)[LIVE_HEADERS[0]]).value == 'תיקון ידני — שמור'
    assert total(wb) == 130
    assert len(rows(wb)) == 1
    assert network.call_count == 1
    wb.close()


def test_gmail_outage_still_publishes_disk_receipts(env):
    base, _, provider, network = env
    initial_workbook(env)
    put(base, document('2002', '75', '21/03/2026'), arrival='11_November', filename='pending.pdf')
    provider.fetch_emails.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        sync(env)
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 2
    assert total(wb) == 200
    wb.close()


def test_cross_year_partial_publication_is_independently_recoverable(env):
    base, _, provider, network = env
    put(base, document('old', '50', '02/01/2025'))
    put(base, document('new', '100', '02/01/2026'), filename='new.pdf')
    from src.sync_workbook import atomic_bytes
    def fail_second(path, raw):
        if '/2026/' in str(path):
            raise PermissionError()
        return atomic_bytes(path, raw)
    with patch('src.sync_workbook.atomic_bytes', side_effect=fail_second):
        with pytest.raises(PermissionError):
            sync(env, allowed_years=[2025, 2026])
    before = report(base, 2025).read_bytes()
    assert not report(base).exists()
    assert sync(env, allowed_years=[2025, 2026])['published'] == 1
    assert report(base, 2025).read_bytes() == before
    assert sync(env, allowed_years=[2025, 2026])['published'] == 0


def test_missing_donation_date_stays_pending_not_misrouted(env):
    base, root, provider, _ = env
    mail(provider, document('unknown', '40', ''))
    with pytest.raises(ValueError, match='pending a valid donation date'):
        sync(env)
    assert not report(base).exists()
    assert len(json.loads((root / 'sync-primary.json').read_text())['documents']) == 1


def test_identical_bytes_across_messages_are_one_document(env):
    base, root, provider, network = env
    mail(provider, document())
    sync(env)
    mail(provider, document(), msg='forwarded', attachment_id='changed')
    assert sync(env)['published'] == 0
    assert len(json.loads((root / 'sync-primary.json').read_text())['documents']) == 1
    assert network.call_count == 1


def test_partial_message_changing_attachment_ids_cannot_duplicate_files(env):
    base, _, provider, _ = env
    mail(provider, document())
    provider.fetch_emails.return_value[0]['attachments'].append({'filename': 'two.pdf', 'attachment_id': 'two'})
    provider.download_attachment.side_effect = [document(), TimeoutError()]
    with pytest.raises(RuntimeError):
        sync(env)
    assert not (base / 'manifest.json').exists()
    provider.fetch_emails.return_value[0]['attachments'][0]['attachment_id'] = 'changed-by-provider'
    provider.download_attachment.side_effect = [document(), document('2002', '75', '21/03/2026')]
    assert sync(env)['published'] == 1
    assert len(list((base / 'receipts').rglob('*.pdf'))) == 2


def test_terminal_rejections_checkpoint_without_permanent_failures(env):
    base, _, provider, _ = env
    mail(provider, document())
    with patch('src.gmail_runner.is_donation_pdf', return_value=(False, 'not donation')):
        assert sync(env)['rejected_attachments'] == 1
        assert sync(env)['rejected_attachments'] == 0
    assert provider.download_attachment.call_count == 1


def test_removed_identity_fails_closed_without_readding(env):
    base, _, _, _ = env
    initial_workbook(env)
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    ws, row = rows(wb)[0]
    ws.cell(row, headers(ws)[ID_HEADER]).value = None
    wb.save(report(base))
    wb.close()
    before = report(base).read_bytes()
    with pytest.raises(ValueError, match='publication identity'):
        sync(env)
    assert report(base).read_bytes() == before


def test_unrecognized_receipt_folder_is_not_ingested(env):
    base, _, _, _ = env
    put(base, document())
    extra = base / 'receipts/primary/2026/manual-copy'
    extra.mkdir()
    (extra / 'ignored.pdf').write_bytes(b'not part of receipt storage')
    assert sync(env)['published'] == 1
    assert sync(env)['published'] == 0


def test_bootstrap_relative_source_link_and_missing_source_match_are_safe(env):
    base, _, _, _ = env
    initial_workbook(env)
    wb = openpyxl.load_workbook(report(base))
    ws = next(donation_sheets(wb))
    ws.cell(2, headers(ws)['שם קובץ']).hyperlink = '../../../receipts/primary/2026/01_January/old.pdf'
    # Existing source-audit link is also intentionally removed, so fallback must
    # fail closed rather than duplicate a manually corrected historical amount.
    ws.cell(2, headers(ws)['שם קובץ']).hyperlink = None
    wb.remove(wb['קובצי מקור'])
    wb.save(report(base))
    wb.close()
    before = report(base).read_bytes()
    with pytest.raises(ValueError, match='Ambiguous historical/document binding'):
        sync(env)
    assert report(base).read_bytes() == before


def test_manually_changed_summary_is_archived_and_row_totals_restored(env):
    base, _, _, _ = env
    initial_workbook(env)
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    wb['סיכום']['C2'] = 999
    wb.save(report(base))
    wb.close()
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    assert total(wb) == 125
    archived = [ws for ws in wb if ws.title.startswith('סיכום ידני קודם')]
    assert archived[0]['C2'].value == 999
    wb.close()


def test_gmail_internal_arrival_is_distinct_from_header_date():
    from src.providers.gmail_provider import _map_message
    stamp = int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp() * 1000)
    email = _map_message({'id': 'synthetic', 'internalDate': str(stamp), 'payload': {
        'headers': [{'name': 'Date', 'value': 'Mon, 15 Jan 2024 12:00:00 +0000'}]}})
    assert email['date'] == '2024-01-15'
    assert email['arrival_date'] == '2026-11-10'


def test_initial_relative_pdf_links_bootstrap_without_duplicate(env):
    base, _, _, _ = env
    initial_workbook(env)
    wb = openpyxl.load_workbook(report(base))
    for ws in wb:
        for row in ws:
            for cell in row:
                if cell.hyperlink and cell.hyperlink.target.endswith('old.pdf'):
                    cell.hyperlink = '../../../receipts/primary/2026/01_January/old.pdf'
    wb.save(report(base))
    wb.close()
    assert sync(env)['published'] == 0
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 1
    assert total(wb) == 125
    wb.close()


def test_conflicting_receipt_copies_retain_existing_conservative_review_rule(env):
    base, _, provider, _ = env
    initial_workbook(env)
    sync(env)
    mail(provider, document('1001', '150', '15/01/2026', variant='conflicting'))
    assert sync(env)['published'] == 1
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 2
    assert total(wb) == 275
    assert any('חשד לכפילות' in ws.cell(row, headers(ws)['הערות']).value for ws, row in rows(wb))
    wb.close()
    assert sync(env)['published'] == 0


def test_sync_cli_search_has_no_arrival_year_cutoff(monkeypatch, env):
    import sys
    import src.main as cli
    from tests.test_user_scope import register
    scope = register(env[0], 'alpha')
    monkeypatch.setattr(cli.UserScope, 'open', lambda _: scope)
    monkeypatch.setattr(sys, 'argv', ['main', '--user', 'alpha', '--mode', 'sync', '--account', 'primary', '--year', '2026', '--delta'])
    with patch('src.main.GmailProvider') as provider, patch('src.delta_sync.run_delta', return_value={'published': 0}):
        cli.main()
        assert provider.call_args.kwargs['query'] == 'has:attachment'


def test_all_historical_rows_receive_new_official_columns(env):
    base, _, _, _ = env
    put(base, document())
    put(base, document('1002', '20', '25/01/2026'), filename='second.pdf')
    generate_summary_workbook(base / 'receipts', base / 'reports', 'primary', 2026)
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    for ws, row in rows(wb):
        assert ws.cell(row, headers(ws)[OFFICIAL]).value == 'השם הרשמי הסינתטי'
        assert ws.cell(row, headers(ws)[LIVE_HEADERS[0]]).value == 'מאושר'
    wb.close()


@pytest.mark.parametrize('all_covered', [True, False])
def test_document_backed_cache_considers_all_tax_years_before_first_prepare(env, all_covered):
    base, root, _, network = env
    cache = OrganizationCache(root / 'organizations.json')
    cache.store_tax_evidence({
        'registration_number': '580000001', 'tax_file_number': '987654321',
        'source_url': 'https://www.gov.il/he/service/confirmation-of-donations',
        'retrieved_at': NOW.isoformat(), 'evidence_sha256': 'a' * 64,
        'verification': 'operator_attested_official_intervals',
        'approval_periods': [['2025-01-01', '2027-12-31' if all_covered else '2025-12-31']],
        'denial_periods': []})
    put(base, document('old', '50', '02/01/2025'))
    put(base, document('new', '100', '02/01/2026'), filename='new.pdf')
    sync(env, allowed_years=[2025, 2026])
    assert network.call_count == (0 if all_covered else 1)
    sync(env, allowed_years=[2025, 2026])
    assert network.call_count == (0 if all_covered else 1)


def test_error_after_successful_atomic_replace_does_not_republish(env):
    base, _, provider, _ = env
    mail(provider, document())
    from src.sync_workbook import atomic_bytes
    def committed_then_error(path, raw):
        atomic_bytes(path, raw)
        raise OSError('synthetic post-commit interruption')
    with patch('src.sync_workbook.atomic_bytes', side_effect=committed_then_error):
        with pytest.raises(OSError):
            sync(env)
    assert sync(env)['published'] == 0
    wb = openpyxl.load_workbook(report(base))
    assert len(rows(wb)) == 1
    wb.close()


def test_bad_backup_and_unsupported_workbook_fail_before_replacement(env):
    base, _, _, _ = env
    _, before = initial_workbook(env)
    backup = report(base).with_name('.backup-' + hashlib.sha256(before).hexdigest() + '.xlsx')
    backup.write_bytes(b'corrupt existing backup')
    with pytest.raises(ValueError, match='Backup verification'):
        sync(env)
    assert report(base).read_bytes() == before
    from zipfile import ZipFile
    with ZipFile(report(base), 'a') as package:
        package.writestr('customXml/item1.xml', '<private-extension/>')
    original = report(base).read_bytes()
    with pytest.raises(ValueError, match='Unsupported Excel features'):
        sync(env)
    assert report(base).read_bytes() == original


def test_filename_arrival_fallback_requires_explicit_local_date_override(env):
    base, root, _, _ = env
    path = put(base, document('missing-date', '40', ''), filename='10_11_26__receipt.pdf')
    with pytest.raises(ValueError, match='pending a valid donation date'):
        sync(env, allowed_years=[2020, 2026])
    state_path = root / 'sync-primary.json'
    state = json.loads(state_path.read_text())
    entry = next(iter(state['documents'].values()))
    assert entry['metadata']['receipt_date'] == '10/11/2026'  # Extraction fallback preserved as audit.
    entry['donation_date_override'] = '03/02/2020'
    state_path.write_text(json.dumps(state), encoding='utf-8')
    assert sync(env, allowed_years=[2020, 2026])['cross_year'] == 1
    assert not report(base).exists()
    assert report(base, 2020).exists()
    assert path.exists()


def test_excel_rewritten_source_audit_links_do_not_republish_or_append(env):
    base, _, _, _ = env
    initial_workbook(env)
    sync(env)
    wb = openpyxl.load_workbook(report(base))
    wb[SOURCES]['C2'].hyperlink = '../../../receipts/primary/2026/01_January/old.pdf'
    ws, row = rows(wb)[0]
    cell = ws.cell(row, headers(ws)['מקורות סנכרון'])
    cell.hyperlink.target = None
    cell.hyperlink.id = None  # A location-only Excel link has no external relationship ID.
    cell.hyperlink.location = "'מקורות סנכרון'!A2"
    wb.save(report(base))
    wb.close()
    before = report(base).read_bytes()
    assert sync(env)['reports_updated'] == 0
    assert report(base).read_bytes() == before


def test_future_misread_donation_date_is_held_not_routed(env):
    base, _, provider, network = env
    mail(provider, document('future', '100', '15/01/2062'))
    with pytest.raises(ValueError, match='pending a valid donation date'):
        sync(env)
    assert not report(base, 2062).exists()
    assert network.call_count == 0


def test_preflight_plan_reads_storage_without_gmail_api_or_workbook_writes(env):
    base, root, provider, network = env
    _, before = initial_workbook(env)
    put(base, document('old-year', '40', '15/02/2020'), filename='late.pdf')
    counts = run_sync(provider=provider, receipts_dir=base / 'receipts', reports_dir=base / 'reports',
                      accounts_dir=base / 'accounts', manifest_path=base / 'manifest.json',
                      account='primary', year=2026, root=root, plan=True, allowed_years=[2020, 2026])
    assert counts['plan_only']
    assert counts['years']['2020']['new_donations'] == 1
    assert counts['years']['2026']['historical_rows'] == 1
    assert counts['api_requests'] == 0
    assert network.call_count == 0
    provider.fetch_emails.assert_not_called()
    assert report(base).read_bytes() == before
    assert not report(base, 2020).exists()


def test_invalid_arrival_date_stays_retryable_instead_of_checkpointing_loss(env):
    base, _, provider, network = env
    mail(provider, document(), date='bad-arrival-date')
    with pytest.raises(RuntimeError, match='Attachment download failures'):
        sync(env)
    assert not (base / 'manifest.json').exists()
    provider.download_attachment.assert_not_called()
    assert network.call_count == 0
    provider.fetch_emails.return_value[0]['date'] = '2026-11-10'
    assert sync(env)['published'] == 1


@pytest.mark.parametrize('flag', ['--plan', '--delta'])
def test_sync_only_flags_cannot_accidentally_start_legacy_gmail(monkeypatch, flag):
    import sys
    import src.main as cli
    monkeypatch.setattr(sys, 'argv', ['main', '--mode', 'gmail', '--account', 'primary', flag])
    with patch('src.main.GmailProvider') as provider:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        provider.assert_not_called()


@pytest.mark.parametrize('already_unified', [False, True])
@pytest.mark.parametrize('explicit_scope', [False, True])
def test_unapproved_late_year_cannot_publish_even_an_authorized_report(env, already_unified, explicit_scope):
    base, root, provider, network = env
    initial_workbook(env)
    if already_unified:
        sync(env)
    original = report(base).read_bytes()
    network.reset_mock()
    mail(provider, document('late-unapproved', '40', '03/02/2020'))
    arguments = dict(provider=provider, receipts_dir=base / 'receipts', reports_dir=base / 'reports',
                     accounts_dir=base / 'accounts', manifest_path=base / 'manifest.json', account='primary', year=2026, root=root)
    with patch('src.sync.replace_verified') as publish_report:
        with pytest.raises(ValueError, match='Tax year not authorized'):
            run_sync(**arguments, allowed_years=[2026] if explicit_scope else None)
        publish_report.assert_not_called()
    assert network.call_count == 0
    assert report(base).read_bytes() == original
    assert not report(base, 2020).exists()
    assert (base / 'manifest.json').exists()  # Ingestion retained independently.
    assert len(list((base / 'receipts').rglob('*.pdf'))) == 2
    # Explicit broader authorization recovers the same saved donation once.
    assert run_sync(**arguments, allowed_years=[2020, 2026])['published'] == 1
    assert run_sync(**arguments, allowed_years=[2020, 2026])['published'] == 0


@pytest.mark.parametrize('old_date,old_amount,known_total,missing', [
    (None, 125, 200, 0), ('not a date', None, 75, 1), ('15/01/2026', 'manual review', 75, 1)])
def test_delivery_accepts_unchanged_incomplete_history_without_dropping_donations(env, old_date, old_amount, known_total, missing):
    from delivery import windows_acceptance as acceptance
    base, root, provider, _ = env
    initial_workbook(env)
    wb = openpyxl.load_workbook(report(base))
    ws = next(donation_sheets(wb))
    columns = headers(ws)
    ws.cell(2, columns['תאריך']).value = old_date
    ws.cell(2, columns['סכום']).value = old_amount
    wb.save(report(base))
    wb.close()
    baseline = {'repo': str(base), 'registry': str(root), 'backups': [], 'expected_years': [2026], 'pdfs': {},
                'reports': {str(report(base)): acceptance.sha(report(base))},
                'workbooks': {'2026': acceptance.workbook_snapshot(report(base))}}
    session = base / 'private-acceptance'
    acceptance.write_json(session / 'baseline.json', baseline)
    mail(provider, document('new-complete', '75', '21/03/2026'))
    assert sync(env)['published'] == 1
    acceptance.verify_history(base, session)
    assert acceptance.verify_rows_and_totals(base, acceptance.annual_reports(base), baseline) == (2, 1)
    wb = openpyxl.load_workbook(report(base))
    annual = next(r for r in wb['סיכום'].iter_rows(values_only=True) if r[0] == 'שנתי')
    assert annual[1:4] == (2, known_total, missing)
    wb.close()


def test_sync_cli_forwards_explicit_year_authorization(monkeypatch, tmp_path):
    import sys
    import src.main as cli
    from tests.test_user_scope import register
    scope = register(tmp_path, 'alpha')
    monkeypatch.setattr(cli.UserScope, 'open', lambda _: scope)
    monkeypatch.setattr(sys, 'argv', ['main', '--user', 'alpha', '--mode', 'sync', '--account', 'primary', '--year', '2026',
                                      '--delta', '--allowed-years', '2026'])
    with patch('src.main.GmailProvider'), patch('src.delta_sync.run_delta', return_value={}) as run:
        cli.main()
        assert run.call_args.kwargs['allowed_years'] == [2026]


def historical_review_workbook(env, amounts=('100', '150'), linked_second=False, blank_names=False):
    """Existing historical rows without guessed physical document bindings."""
    from src.receipt_deduplication import Donation
    from src.receipt_summary import SOURCE_SHEET, _write_month_sheet
    base, _, _, _ = env
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    sources = wb.create_sheet(SOURCE_SHEET)
    sources.append(['תרומה בדוח', 'חודש שמירת מקור', 'פתיחת קובץ מקור'])
    donations = []
    for index, amount in enumerate(amounts):
        source = base / f'receipts/primary/2026/01_January/historical-{index}.pdf'
        if linked_second and index == 1:
            source = put(base, document('1001', amount), filename=source.name)
        meta = ReceiptMetadata(account='primary', file_name=source.name, receipt_number='1001',
                               registration_number='580000001', receipt_date='15/01/2026', amount=amount,
                               organization_name='' if blank_names else 'historical extracted name',
                               severity='warning', notes='חשד לכפילות — נשמר בנפרד לבדיקה ידנית')
        donations.append(Donation(meta, (source,), suspected_group=1))
    _write_month_sheet(wb, '01_January', donations, sources)
    wb.create_sheet('סיכום').append(['historical reviewed total', sum(float(a) for a in amounts)])
    wb.create_sheet('ביקורת היסטורית')['A1'] = 'review evidence must remain'
    path = report(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    wb.close()
    return path


@pytest.mark.parametrize('linked_second', [False, True])
def test_historical_only_suspected_group_is_preserved_without_false_source_abort(env, linked_second):
    base, root, provider, network = env
    path = historical_review_workbook(env, linked_second=linked_second)
    original = path.read_bytes()
    plan = run_sync(provider=provider, receipts_dir=base / 'receipts', reports_dir=base / 'reports',
                    accounts_dir=base / 'accounts', manifest_path=base / 'manifest.json', account='primary',
                    year=2026, root=root, plan=True, allowed_years=[2026])
    assert plan['historical_review_groups'] == 1
    assert plan['historical_review_rows'] == 2
    assert plan['unresolved_historical_document_groups'] == 0
    assert plan['years']['2026']['new_donations'] == 0
    assert path.read_bytes() == original
    provider.fetch_emails.assert_not_called()
    network.assert_not_called()
    assert sync(env)['published'] == 0
    wb = openpyxl.load_workbook(path)
    assert len(rows(wb)) == 2
    assert total(wb) == 250
    for ws, row in rows(wb):
        assert ws.cell(row, headers(ws)['הערות']).value == 'חשד לכפילות — נשמר בנפרד לבדיקה ידנית'
    assert wb['ביקורת היסטורית']['A1'].value == 'review evidence must remain'
    identities = [ws.cell(row, headers(ws)[ID_HEADER]).value for ws, row in rows(wb)]
    assert len(set(identities)) == 2
    wb.close()
    committed = path.read_bytes()
    assert sync(env)['reports_updated'] == 0
    assert path.read_bytes() == committed
    assert network.call_count == 1


@pytest.mark.parametrize('source_kind', ['missing', 'existing-outside-catalog'])
def test_unresolved_historical_source_and_unbound_document_still_block(env, source_kind):
    base, root, provider, network = env
    path = historical_review_workbook(env, amounts=('125',))
    if source_kind == 'existing-outside-catalog':
        outside = base / 'external-synthetic.pdf'
        outside.write_bytes(document('1001', '125'))
        wb = openpyxl.load_workbook(path)
        ws = next(donation_sheets(wb))
        ws.cell(2, headers(ws)['שם קובץ']).hyperlink = outside.as_uri()
        wb.save(path)
        wb.close()
        assert outside.is_file()  # Unresolved identity is not proof of absence.
    original = path.read_bytes()
    put(base, document('1001', '100'), filename='unbound-candidate.pdf')
    arguments = dict(provider=provider, receipts_dir=base / 'receipts', reports_dir=base / 'reports',
                     accounts_dir=base / 'accounts', manifest_path=base / 'manifest.json', account='primary',
                     year=2026, root=root, allowed_years=[2026])
    for plan in (True, False):
        with patch('src.sync.replace_verified') as publication:
            with pytest.raises(ValueError, match='Ambiguous historical/document binding') as exc:
                run_sync(**arguments, plan=plan)
            assert exc.value.code == 'historical_source_conflict'
            publication.assert_not_called()
        assert path.read_bytes() == original
    network.assert_not_called()


def test_exact_merge_of_multiple_historical_identities_remains_blocked(env):
    base, root, provider, network = env
    path = historical_review_workbook(env, amounts=('100', '100'), blank_names=True)
    original = path.read_bytes()
    put(base, document('1001', '100'), filename='matching-copy.pdf')
    with pytest.raises(ValueError) as exc:
        run_sync(provider=provider, receipts_dir=base / 'receipts', reports_dir=base / 'reports',
                 accounts_dir=base / 'accounts', manifest_path=base / 'manifest.json', account='primary',
                 year=2026, root=root, plan=True, allowed_years=[2026])
    assert exc.value.code == 'historical_consolidation_conflict'
    assert path.read_bytes() == original
    network.assert_not_called()


def test_single_exact_legacy_match_reuses_consolidation_without_duplicate(env):
    base, _, _, _ = env
    path = historical_review_workbook(env, amounts=('100',), blank_names=True)
    put(base, document('1001', '100'), filename='matching-copy.pdf')
    assert sync(env)['published'] == 0
    wb = openpyxl.load_workbook(path)
    assert len(rows(wb)) == 1
    assert total(wb) == 100
    assert len(json.loads(wb[INDEX]['B2'].value)['documents']) == 1
    wb.close()
    assert sync(env)['published'] == 0


def test_pending_catalog_assignment_does_not_count_as_published_document():
    from src.receipt_deduplication import Donation
    from src.sync import historical_review_counts
    meta = ReceiptMetadata()
    groups = [Donation(meta, (Path('legacy:old'),), suspected_group=1),
              Donation(meta, (Path('pending-document'),), suspected_group=1)]
    counts = historical_review_counts(groups, {'legacy:old': meta}, {'legacy:old': 'historical-id'})
    assert counts['unresolved_historical_document_groups'] == 1
    assert counts['historical_review_groups'] == 0
