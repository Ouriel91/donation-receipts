"""Synthetic forward-only acceptance; production extraction, cache and XLSX I/O."""
from datetime import datetime, timezone, timedelta
import json
from unittest.mock import MagicMock
import openpyxl
import pytest

from tests.test_sync import env, document, mail, report, initial_workbook, total, put
from src.delta_sync import run_delta, read_state, discovery_window, all_rows
from src.sync_state import SyncError
from src.sync_workbook import headers, ID_HEADER, INDEX, donation_sheets, LIVE_HEADERS
from src.section46_live import LiveSection46

NOW = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
JAN = datetime(2027, 1, 2, 10, tzinfo=timezone.utc)


def run(env, monkeypatch, now=NOW, year=2026, plan=False):
    base, root, provider, network = env
    monkeypatch.setattr('src.delta_sync.LiveSection46', lambda path, registry: LiveSection46(
        path, registry, clock=lambda: now, transport=network, sleep=lambda n: None))
    # Keep authoritative server snapshot in the synthetic run's present.
    if network.side_effect is None:
        network.return_value = json.dumps({'Status': 200, 'Message': {
            'start_date': '2025-01-01', 'end_date': '2030-12-31', 'as_of_date': now.date().isoformat()}}).encode()
    return run_delta(provider=provider, receipts_dir=base/'receipts', reports_dir=base/'reports',
                     accounts_dir=base/'accounts', manifest_path=base/'manifest.json',
                     account='primary', year=year, root=root, plan=plan, now=now)


def state(env):
    return json.loads((env[1]/'delta-primary.json').read_text())


def messages(env, specs):
    provider = env[2]
    raws = {}
    emails = []
    for ident, number, date in specs:
        raw = document(number, '75', date)
        mail(provider, raw, msg=ident)
        emails += provider.fetch_emails.return_value
        raws[ident] = raw
    provider.fetch_emails.return_value = emails
    provider.download_attachment.side_effect = lambda mid, aid: raws[mid]


def test_initial_boundary_read_only_no_network_or_extraction(env, monkeypatch):
    base, root, provider, network = env
    initial_workbook(env)
    before = {str(p): p.read_bytes() for p in base.rglob('*') if p.is_file()}
    info = run(env, monkeypatch, plan=True)
    assert info['initial_discovery']
    assert info['search_start'] == '2026-01-01T00:00:00+02:00'
    assert info['authorized_years'] == [2026]
    provider.fetch_emails.assert_not_called()
    network.assert_not_called()
    assert {str(p): p.read_bytes() for p in base.rglob('*') if p.is_file()} == before


def test_checkpoint_overlap_downtime_and_new_year(env, monkeypatch):
    run(env, monkeypatch)
    info = run(env, monkeypatch, now=NOW+timedelta(days=5), plan=True)
    assert datetime.fromisoformat(info['search_start']) == NOW-timedelta(hours=48)
    info = run(env, monkeypatch, now=JAN, year=None, plan=True)
    assert info['authorized_years'] == [2026, 2027]
    assert datetime.fromisoformat(info['search_start']) == NOW-timedelta(hours=48)
    assert not info['initial_discovery']


def test_interrupted_discovery_preserves_checkpoint(env, monkeypatch):
    run(env, monkeypatch)
    before = (env[1]/'delta-primary.json').read_bytes()
    env[2].fetch_emails.side_effect = RuntimeError('synthetic failure')
    with pytest.raises(RuntimeError):
        run(env, monkeypatch, now=NOW+timedelta(days=5))
    assert (env[1]/'delta-primary.json').read_bytes() == before


def test_historical_baseline_new_full_enrichment_and_repeat(env, monkeypatch):
    base, root, provider, network = env
    old, original = initial_workbook(env)
    # An unrelated old PDF must never be extracted in the delta path.
    put(base, b'not a PDF', filename='unrelated.pdf')
    mail(provider, document('2002', '75', '21/03/2026'))
    assert run(env, monkeypatch)['published'] == 1
    wb = openpyxl.load_workbook(report(base))
    assert len(all_rows(wb)) == 2
    assert total(wb) == 200
    old_ws, row = next(iter(all_rows(wb).values()))
    cols = headers(old_ws)
    assert old_ws.cell(row, cols['שם עמותה']).value == 'שם שתוקן ידנית'
    assert old_ws.cell(row, cols['הערות']).value == 'תיקון ידני לשימור'
    assert ID_HEADER not in cols or old_ws.cell(row, cols[ID_HEADER]).value is None
    new_ws = wb['מרץ']; cols = headers(new_ws)
    assert new_ws.cell(2, cols['שם עמותה']).value == 'השם הרשמי הסינתטי'
    assert new_ws.cell(2, cols[LIVE_HEADERS[0]]).value == 'מאושר'
    original_name = next(iter(state(env)['documents'].values()))['metadata']['organization_name']
    assert any(original_name == c.value for row in wb['Registry identity audit'] for c in row)
    assert new_ws.cell(2, cols['קובץ']).hyperlink if 'קובץ' in cols else any(c.hyperlink for c in new_ws[2])
    assert wb['ביקורת קיימת']['A1'].value == 'בדיקה ידנית היסטורית'
    assert list(report(base).parent.glob('.backup-*.xlsx'))[0].read_bytes() == original
    wb.close()
    before = report(base).read_bytes()
    assert run(env, monkeypatch)['reports_updated'] == 0
    assert report(base).read_bytes() == before
    assert network.call_count == 1


@pytest.mark.parametrize('old_conflict', [False, True])
def test_unrelated_history_ambiguity_and_specific_duplicate(env, monkeypatch, old_conflict):
    base, root, provider, network = env
    initial_workbook(env)
    wb = openpyxl.load_workbook(report(base))
    ws = next(donation_sheets(wb))
    # An ambiguous historical pair remains separate and requires no PDF identity.
    for cell in ws[2]:
        ws.cell(3, cell.column, cell.value)
    wb.save(report(base)); wb.close()
    before = report(base).read_bytes()
    mail(provider, document('1001' if old_conflict else '3333', '75', '15/01/2026'))
    if old_conflict:
        with pytest.raises(SyncError, match='Pending work'):
            run(env, monkeypatch)
        assert report(base).read_bytes() == before
        assert next(iter(state(env)['documents'].values()))['reason'] == 'historical_duplicate_candidate'
        network.assert_not_called()
    else:
        assert run(env, monkeypatch)['published'] == 1
        wb = openpyxl.load_workbook(report(base))
        assert len(all_rows(wb)) == 3
        wb.close()


def test_publication_failure_and_recovery_outside_overlap(env, monkeypatch):
    mail(env[2], document('9999'))
    real = __import__('src.delta_sync', fromlist=['replace_verified']).replace_verified
    monkeypatch.setattr('src.delta_sync.replace_verified', MagicMock(side_effect=OSError('synthetic')))
    with pytest.raises(OSError):
        run(env, monkeypatch)
    assert next(iter(state(env)['documents'].values()))['stage'] in ('validated', 'enriched')
    env[2].fetch_emails.return_value = []
    monkeypatch.setattr('src.delta_sync.replace_verified', real)
    assert run(env, monkeypatch, now=NOW+timedelta(days=10))['published'] == 1
    assert run(env, monkeypatch, now=NOW+timedelta(days=10))['published'] == 0


def test_legacy_processed_skipped_without_publication_claim(env, monkeypatch):
    base, _, provider, network = env
    mail(provider, document())
    (base/'manifest.json').write_text(json.dumps([{'account': 'primary', 'message_id': 'new-message', 'saved_files': ['unavailable']}]))
    assert run(env, monkeypatch)['published'] == 0
    provider.download_attachment.assert_not_called()
    assert not state(env)['documents']
    assert state(env)['messages']['new-message']['stage'] == 'legacy_processed'


@pytest.mark.parametrize('date,reason', [('01/01/2020', 'unauthorized_tax_year'), ('', 'invalid_donation_date'),
                                        ('01/01/2099', 'invalid_donation_date')])
def test_invalid_or_old_dates_retained_before_publication(env, monkeypatch, date, reason):
    mail(env[2], document('5555', date=date))
    with pytest.raises(SyncError):
        run(env, monkeypatch)
    assert not list((env[0]/'reports').rglob('*.xlsx'))
    assert next(iter(state(env)['documents'].values()))['reason'] == reason
    env[3].assert_not_called()


def test_months_and_years_rollover_append_and_repeat(env, monkeypatch):
    messages(env, [('dec', '6001', '31/12/2026'), ('jan', '6002', '01/01/2027')])
    assert run(env, monkeypatch, now=JAN, year=None)['published'] == 2
    for year, title in [(2026, 'דצמבר'), (2027, 'ינואר')]:
        wb = openpyxl.load_workbook(report(env[0], year)); ws = wb[title]
        assert ws.sheet_view.rightToLeft is True
        assert ws.freeze_panes == 'A2'
        assert all(c.font.bold for c in ws[1] if c.value)
        assert total(wb) == 75
        wb.close()
    feb = datetime(2027, 2, 2, tzinfo=timezone.utc)
    messages(env, [('jan2', '6003', '31/01/2027'), ('feb', '6004', '01/02/2027')])
    assert run(env, monkeypatch, now=feb, year=None)['published'] == 2
    wb = openpyxl.load_workbook(report(env[0], 2027))
    assert wb['ינואר'].max_row == 3 and wb['פברואר'].max_row == 2
    assert list(headers(wb['ינואר'])) == list(headers(wb['פברואר']))
    assert total(wb) == 225
    wb.close()
    before = report(env[0], 2027).read_bytes()
    assert run(env, monkeypatch, now=feb, year=None)['reports_updated'] == 0
    assert report(env[0], 2027).read_bytes() == before


def test_no_new_donations_no_empty_reports(env, monkeypatch):
    assert run(env, monkeypatch, now=JAN, year=None)['reports_updated'] == 0
    assert not (env[0]/'reports').exists()


def test_multi_year_partial_failure_recovery(env, monkeypatch):
    messages(env, [('dec', '7001', '31/12/2026'), ('jan', '7002', '01/01/2027')])
    from src.delta_sync import replace_verified
    def fail_second(path, before, wb):
        if '2027' in str(path):
            raise OSError('synthetic interruption')
        return replace_verified(path, before, wb)
    monkeypatch.setattr('src.delta_sync.replace_verified', fail_second)
    with pytest.raises(OSError):
        run(env, monkeypatch, now=JAN, year=None)
    first = report(env[0]).read_bytes()
    env[2].fetch_emails.return_value = []
    monkeypatch.setattr('src.delta_sync.replace_verified', replace_verified)
    assert run(env, monkeypatch, now=JAN, year=None)['published'] == 1
    assert report(env[0]).read_bytes() == first
    wb = openpyxl.load_workbook(report(env[0], 2027)); assert len(all_rows(wb)) == 1; wb.close()


def test_69_baseline_rows_unresolved_sources_are_not_reconstructed(env, monkeypatch):
    initial_workbook(env)
    wb = openpyxl.load_workbook(report(env[0])); ws = next(donation_sheets(wb)); cols = headers(ws)
    for row in range(3, 71):
        for cell in ws[2]:
            ws.cell(row, cell.column, cell.value)
        ws.cell(row, cols['מספר קבלה'], 'old-' + str(row))
    wb.save(report(env[0])); wb.close()
    mail(env[2], document('genuinely-new', '75', '01/02/2026'))
    assert run(env, monkeypatch)['published'] == 1
    wb = openpyxl.load_workbook(report(env[0]))
    assert len(all_rows(wb)) == 70
    assert total(wb) == 69*125+75
    assert wb[INDEX].max_row == 2  # Only ONE real new publication identity.
    wb.close()
    assert len(state(env)['documents']) == 1


def test_incomplete_historical_row_preserved_in_totals(env, monkeypatch):
    initial_workbook(env)
    wb = openpyxl.load_workbook(report(env[0])); ws = next(donation_sheets(wb)); cols = headers(ws)
    ws.cell(2, cols['תאריך']).value = None
    ws.cell(2, cols['סכום']).value = None
    wb.save(report(env[0])); wb.close()
    mail(env[2], document('9001'))
    run(env, monkeypatch)
    wb = openpyxl.load_workbook(report(env[0]))
    assert len(all_rows(wb)) == 2
    assert total(wb) == 100
    assert next(r[3] for r in wb['סיכום'].values if r[0] == 'שנתי') == 1
    assert total(wb, 'תאריך לבדיקה') == 0
    wb.close()


def test_download_failure_durable_queue_retried_after_downtime(env, monkeypatch):
    mail(env[2], document('8001'))
    env[2].download_attachment.side_effect = OSError('synthetic')
    with pytest.raises(SyncError):
        run(env, monkeypatch)
    assert state(env)['messages']['new-message']['stage'] == 'discovered'
    env[2].fetch_emails.return_value = []
    env[2].download_attachment.side_effect = None
    assert run(env, monkeypatch, now=NOW+timedelta(days=20))['published'] == 1


def test_crash_after_workbook_commit_before_acknowledgement(env, monkeypatch):
    mail(env[2], document('8002'))
    from src.delta_sync import atomic_json
    failed = False
    def interrupt(path, value):
        nonlocal failed
        if path.name == 'delta-primary.json' and report(env[0]).exists() and not failed:
            failed = True
            raise OSError('synthetic crash')
        return atomic_json(path, value)
    monkeypatch.setattr('src.delta_sync.atomic_json', interrupt)
    with pytest.raises(OSError):
        run(env, monkeypatch)
    env[2].fetch_emails.return_value = []
    assert run(env, monkeypatch)['published'] == 0
    wb = openpyxl.load_workbook(report(env[0])); assert len(all_rows(wb)) == 1; wb.close()


def test_repeated_attachment_new_message_no_duplicate(env, monkeypatch):
    raw = document('8003')
    mail(env[2], raw, 'one'); run(env, monkeypatch)
    mail(env[2], raw, 'two'); result = run(env, monkeypatch)
    assert result['published'] == result['reports_updated'] == 0
    assert len(state(env)['documents']) == 1


def test_cross_run_equivalent_document_uses_existing_consolidation(env, monkeypatch):
    mail(env[2], document('8004', variant='first')); run(env, monkeypatch)
    mail(env[2], document('8004', variant='second'), 'copy')
    assert run(env, monkeypatch)['published'] == 0
    wb = openpyxl.load_workbook(report(env[0])); assert len(all_rows(wb)) == 1
    assert len(json.loads(wb[INDEX]['B2'].value)['documents']) == 2
    wb.close()
    assert env[3].call_count == 1


@pytest.mark.parametrize('response', [b'{"Status":200,"Message":"Charity association number not found"}',
                                     b'{"Status":200,"Message":{}}', None])
def test_api_unknown_is_explicit_and_recheck_does_not_duplicate(env, monkeypatch, response):
    network = env[3]
    network.side_effect = (lambda n: response) if response else TimeoutError('synthetic')
    mail(env[2], document('8100'))
    result = run(env, monkeypatch)
    assert result['published'] == 1 and result['pending_recheck'] == 1
    wb = openpyxl.load_workbook(report(env[0])); ws = next(donation_sheets(wb))
    assert ws.cell(2, headers(ws)[LIVE_HEADERS[0]]).value.startswith('לא ידוע')
    wb.close()
    before = report(env[0]).read_bytes()
    run(env, monkeypatch)
    assert network.call_count == 1
    assert report(env[0]).read_bytes() == before
    network.side_effect = None
    # not-found evidence has 30-day freshness; failures have a one-day cooldown.
    result = run(env, monkeypatch, now=NOW+timedelta(days=31))
    assert result['published'] == 0 and result['pending_recheck'] == 0
    wb = openpyxl.load_workbook(report(env[0])); assert len(all_rows(wb)) == 1
    assert next(iter(donation_sheets(wb))).cell(2, headers(next(iter(donation_sheets(wb))))[LIVE_HEADERS[0]]).value == 'מאושר'
    wb.close()


def test_fresh_cache_avoids_api_for_another_donation(env, monkeypatch):
    mail(env[2], document('8200')); run(env, monkeypatch)
    mail(env[2], document('8201'), 'second'); run(env, monkeypatch)
    assert env[3].call_count == 1


def test_future_checkpoint_blocks_without_changes(env, monkeypatch):
    run(env, monkeypatch)
    before = (env[1]/'delta-primary.json').read_bytes()
    with pytest.raises(SyncError, match='clock'):
        run(env, monkeypatch, now=NOW-timedelta(days=1))
    assert (env[1]/'delta-primary.json').read_bytes() == before


def test_operational_lock_prevents_overlapping_discovery(env, monkeypatch):
    from src.private_runtime import operational_lock
    with operational_lock(env[1]):
        with pytest.raises(BlockingIOError):
            run(env, monkeypatch)
    env[2].fetch_emails.assert_not_called()


def test_excel_owner_file_blocks_before_api_or_publication(env, monkeypatch):
    initial_workbook(env)
    path = report(env[0]); original = path.read_bytes()
    path.with_name('~$'+path.name).write_bytes(b'Excel open')
    mail(env[2], document('8300'))
    with pytest.raises(SyncError, match='Close Excel'):
        run(env, monkeypatch)
    assert path.read_bytes() == original
    env[3].assert_not_called()


def test_configured_timezone_midnight_boundary(env, monkeypatch):
    account = env[0]/'accounts/primary'; account.mkdir(parents=True)
    (account/'config.json').write_text(json.dumps({'operating_timezone': 'America/New_York'}))
    info = run(env, monkeypatch, now=datetime(2027, 1, 1, 1, tzinfo=timezone.utc), year=None, plan=True)
    assert info['search_start'] == '2026-01-01T00:00:00-05:00'
    assert info['authorized_years'] == [2025, 2026]


def test_expired_live_evidence_triggers_official_adapter(env, monkeypatch):
    mail(env[2], document('8400')); run(env, monkeypatch)
    mail(env[2], document('8401'), 'next'); run(env, monkeypatch, now=NOW+timedelta(days=31))
    assert env[3].call_count == 2


def test_historical_interval_not_covered_stays_unknown(env, monkeypatch):
    env[3].side_effect = lambda n: json.dumps({'Status': 200, 'Message': {
        'start_date': '2026-10-01', 'end_date': '2030-12-31', 'as_of_date': '2026-11-11'}}).encode()
    mail(env[2], document('8500', date='01/01/2026'))
    result = run(env, monkeypatch)
    assert result['pending_recheck'] == 1
    assert 'לא ידוע' in next(iter(state(env)['documents'].values()))['section46_status']


def test_existing_february_append_after_missed_runs(env, monkeypatch):
    first = datetime(2027, 2, 2, tzinfo=timezone.utc)
    mail(env[2], document('8600', date='01/02/2027'))
    run(env, monkeypatch, now=first, year=None)
    mail(env[2], document('8601', date='02/02/2027'), 'late')
    later = first+timedelta(days=8)
    run(env, monkeypatch, now=later, year=None)
    assert f'after:{int((first-timedelta(hours=48)).timestamp())-1}' in env[2]._query
    wb = openpyxl.load_workbook(report(env[0], 2027))
    assert wb['פברואר'].max_row == 3
    assert len(wb.sheetnames) == len(set(wb.sheetnames))
    assert total(wb) == 200
    wb.close()


def test_cli_dynamic_year_without_fixed_year(monkeypatch, tmp_path):
    import src.main as main
    import sys
    from unittest.mock import patch
    from tests.test_user_scope import register
    scope = register(tmp_path, 'alpha')
    monkeypatch.setattr(main.UserScope, 'open', lambda _: scope)
    monkeypatch.setattr(sys, 'argv', ['main', '--user', 'alpha', '--mode', 'sync', '--account', 'primary', '--delta', '--plan'])
    with patch('src.delta_sync.run_delta', return_value={}) as delta:
        main.main()
    assert delta.call_args.kwargs['year'] is None
    assert delta.call_args.kwargs['plan'] is True


def test_unverified_identity_keeps_original_name_and_resolves_same_row(env, monkeypatch):
    from src.organization_cache import OrganizationCache
    root = env[1]
    # Retain the existing valid cache schema, with no verified identity entries.
    original = (root/'organizations.json').read_bytes()
    # The registry API schema is public; replace the synthetic registry via an
    # official import containing a different synthetic association.
    other = env[0]/'other.csv'
    other.write_text('מספר עמותה,שם עמותה בעברית\n580000002,אחר\n', encoding='utf-8-sig')
    OrganizationCache(root/'organizations.json').import_registry(other)
    mail(env[2], document('8700'))
    result = run(env, monkeypatch)
    assert result['pending_recheck'] == 1
    wb = openpyxl.load_workbook(report(env[0])); ws = next(donation_sheets(wb)); cols = headers(ws)
    extracted = next(iter(state(env)['documents'].values()))['metadata']['organization_name']
    assert ws.cell(2, cols['שם מקורי (קבלה)']).value == extracted
    assert ws.cell(2, cols['שם עמותה']).value == extracted
    wb.close()
    (root/'organizations.json').write_bytes(original)
    result = run(env, monkeypatch)
    assert result['published'] == 0 and result['pending_recheck'] == 0
    wb = openpyxl.load_workbook(report(env[0])); ws = next(donation_sheets(wb)); cols = headers(ws)
    assert len(all_rows(wb)) == 1
    assert ws.cell(2, cols['שם עמותה']).value == 'השם הרשמי הסינתטי'
    assert ws.cell(2, cols['שם מקורי (קבלה)']).value == extracted
    wb.close()


def test_old_published_recheck_is_deferred_without_authorizing_old_year(env, monkeypatch):
    env[3].side_effect = TimeoutError('synthetic')
    mail(env[2], document('8800'))
    assert run(env, monkeypatch)['pending_recheck'] == 1
    before = report(env[0]).read_bytes()
    env[2].fetch_emails.return_value = []
    result = run(env, monkeypatch, now=datetime(2028, 1, 2, tzinfo=timezone.utc), year=None)
    assert result['deferred_rechecks'] == 1
    assert result['pending_review'] == 0
    doc = next(iter(state(env)['documents'].values()))
    assert doc['stage'] == 'published_recheck'
    assert doc['recheck_deferred_reason'] == 'unauthorized_tax_year'
    assert report(env[0]).read_bytes() == before
    assert env[3].call_count == 1


def test_missing_downloaded_pdf_is_pending_failure_not_success(env, monkeypatch):
    mail(env[2], document('8801'))
    from src.delta_sync import _extract_row
    monkeypatch.setattr('src.delta_sync._extract_row', MagicMock(side_effect=OSError('synthetic crash')))
    with pytest.raises(OSError):
        run(env, monkeypatch)
    doc = next(iter(state(env)['documents'].values()))
    source = env[0]/'receipts/primary'/doc['paths'][0]
    source.unlink()  # Synthetic loss between download and extraction.
    monkeypatch.setattr('src.delta_sync._extract_row', _extract_row)
    env[2].fetch_emails.return_value = []
    with pytest.raises(SyncError):
        run(env, monkeypatch)
    doc = next(iter(state(env)['documents'].values()))
    assert doc['stage'] == 'review' and doc['reason'] == 'document_source_unavailable'
    assert not report(env[0]).exists()


def test_failed_queue_refreshes_attachment_descriptor_outside_overlap(env, monkeypatch):
    raw = document('8802')
    mail(env[2], raw, attachment_id='stale')
    fresh = dict(env[2].fetch_emails.return_value[0])
    fresh['attachments'] = [{'filename': 'receipt.pdf', 'attachment_id': 'fresh'}]
    env[2].download_attachment.side_effect = OSError('stale synthetic identifier')
    with pytest.raises(SyncError):
        run(env, monkeypatch)
    env[2].fetch_emails.return_value = []
    monkeypatch.setattr(type(env[2]), 'fetch_message', lambda provider, key: fresh, raising=False)
    env[2].download_attachment.side_effect = lambda mid, aid: raw if aid == 'fresh' else (_ for _ in ()).throw(OSError('stale'))
    assert run(env, monkeypatch, now=NOW+timedelta(days=10))['published'] == 1


def test_new_workbook_data_styles_survive_cross_workbook_copy(env, monkeypatch):
    mail(env[2], document('008803', '123.45'))
    run(env, monkeypatch)
    wb = openpyxl.load_workbook(report(env[0])); ws = next(donation_sheets(wb)); cols = headers(ws)
    assert ws.cell(2, cols['סכום']).number_format == '#,##0.##'
    assert ws.cell(2, cols['מספר קבלה']).number_format == '@'
    assert ws.cell(2, cols['מספר קבלה']).value == '008803'
    wb.close()


def test_new_ambiguous_copy_does_not_demote_existing_published_recheck(env, monkeypatch):
    env[3].side_effect = TimeoutError('synthetic')
    mail(env[2], document('8900', '100'))
    run(env, monkeypatch)
    original = report(env[0]).read_bytes()
    mail(env[2], document('8900', '200', variant='conflict'), 'conflict')
    with pytest.raises(SyncError):
        run(env, monkeypatch)
    assert sorted(doc['stage'] for doc in state(env)['documents'].values()) == ['published_recheck', 'review']
    assert report(env[0]).read_bytes() == original


def test_invalid_private_stage_fails_closed_without_echoing_values(env, monkeypatch):
    run(env, monkeypatch)
    path = env[1]/'delta-primary.json'
    value = state(env)
    value['documents']['synthetic'] = {'stage': 'invalid-untrusted-value'}
    path.write_text(json.dumps(value))
    env[2].fetch_emails.reset_mock()
    before = path.read_bytes()
    with pytest.raises(SyncError, match='Invalid durable processing stage'):
        run(env, monkeypatch, plan=True)
    assert path.read_bytes() == before
    env[2].fetch_emails.assert_not_called()
