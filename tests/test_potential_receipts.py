import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from src import potential_receipts as discovery


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def request(value):
    return SimpleNamespace(execute=lambda: value)


def detail(key, text, *, subject='אישור תרומתך', mime='text/html', parts=None):
    part = {'mimeType': mime, 'body': {'data': encoded(text)}}
    return {'id': key, 'threadId': 'abc123', 'payload': {**part, 'headers': [
        {'name': 'subject', 'value': subject},
        {'name': 'From', 'value': '=HYPERLINK("https://example.invalid")'},
        {'name': 'Date', 'value': 'Tue, 03 Mar 2026 12:00:00 +0200'},
    ], 'parts': parts or []}}


@pytest.mark.parametrize('body', (
    'תרמו עכשיו. אחרי התרומה תקבלו קבלה להורדה',
    'אנו מבקשים תרומה. לחצו כאן כדי לתרום',
    'תודה על תרומתך! לצפייה בגלריית התמונות לחץ כאן',
    'Download a donation appeal flyer',
))
def test_rejects_appeals_and_unrelated_actions(body):
    assert discovery.detect('', body) is None


def test_gmail_mime_body_excludes_pdf_and_does_not_treat_href_as_content():
    button = '<a href="https://example.invalid/secret" aria-label="להורדת הקבלה"><img></a>'
    messages = Mock()
    item = detail('a1', button)
    assert discovery._candidate(messages, item)
    item['payload']['parts'] = [{'mimeType': 'application/pdf', 'body': {}}]
    assert discovery._candidate(messages, item) is None
    item = detail('a1', '<a href="https://example.invalid/download">more</a>', subject='קבלה על תרומה')
    assert discovery._candidate(messages, item) is None
    messages.attachments.assert_not_called()


def test_large_plain_body_can_use_gmail_mime_body_attachment():
    messages = Mock()
    messages.attachments.return_value.get.return_value = request({
        'data': encoded('תודה על תרומתכם. לצפייה בקבלה מספר 19473 לחץ כאן')})
    item = detail('a1', '', mime='text/plain')
    item['payload']['body'] = {'attachmentId': 'body'}
    assert discovery._candidate(messages, item)[-1] == '19473'
    messages.attachments.return_value.get.assert_called_once_with(userId='me', messageId='a1', id='body')


def test_discovery_pages_dedup_status_and_account_isolation(monkeypatch, tmp_path):
    messages = Mock()
    users = Mock()
    users.messages.return_value = messages
    users.getProfile.return_value = request({'emailAddress': 'test+donor@example.invalid'})
    monkeypatch.setattr(discovery, 'build', lambda *_a, **_kw: SimpleNamespace(users=lambda: users))
    monkeypatch.setattr(discovery, 'get_gmail_credentials', lambda _: None)
    messages.list.side_effect = lambda **kw: request(
        {'messages': [{'id': 'aa'}]} if 'pageToken' in kw else
        {'messages': [{'id': 'aa'}, {'id': 'bb'}], 'nextPageToken': 'next'})
    messages.get.side_effect = lambda **kw: request(detail(
        kw['id'], '<p>קבלה מספר 19473</p><button>לצפייה</button>') if kw['id'] == 'aa'
        else detail('bb', '', parts=[{'filename': 'proof.PDF'}]))
    accounts, reports = tmp_path / 'accounts', tmp_path / 'reports'
    (accounts / 'second').mkdir(parents=True)
    report_dir = reports / 'second' / '2026'
    report_dir.mkdir(parents=True)
    canonical = report_dir / 'donation_summary_2026.xlsx'
    canonical.write_bytes(b'unchanged canonical')
    args = (accounts, reports, 'second', 2026)
    now = datetime(2026, 3, 5, tzinfo=timezone.utc)
    assert discovery.run_discovery(*args, now=now) == 1
    report = reports / 'manual' / '2026' / 'potential_receipts_2026.xlsx'
    wb = load_workbook(report)
    sheet = wb.active
    assert sheet['E2'].value == '19473'
    assert sheet['B2'].data_type == 's'
    assert sheet['F2'].hyperlink.target == (
        'https://mail.google.com/mail/u/?authuser=test%2Bdonor%40example.invalid#all/abc123')
    sheet['G2'] = 'טופל'
    wb.save(report)
    wb.close()
    assert discovery.run_discovery(*args, now=now) == 0
    full_query = messages.list.call_args.kwargs['q']
    assert discovery.run_discovery(*args, delta=True, now=now) == 0
    assert messages.list.call_args.kwargs['q'] != full_query
    assert messages.list.call_args.kwargs['includeSpamTrash'] is False
    wb = load_workbook(report)
    assert wb.active.max_row == 2 and wb.active['G2'].value == 'טופל'
    wb.close()
    assert canonical.read_bytes() == b'unchanged canonical'
    before = report.read_bytes()
    messages.list.side_effect = RuntimeError('interrupted')
    with pytest.raises(RuntimeError):
        discovery.run_discovery(*args, delta=True, now=now)
    assert report.read_bytes() == before
    users.getProfile.return_value = request({'emailAddress': 'another@example.invalid'})
    with pytest.raises(ValueError, match='report_account_conflict'):
        discovery.run_discovery(*args, now=now)
    assert report.read_bytes() == before


@pytest.mark.parametrize('html', (
    '<div style="display:none"><div>תרומה קבלה להורדה</div></div>',
    '<input type="hidden" value="תרומה קבלה להורדה">',
))
def test_hidden_html_does_not_supply_evidence(html):
    messages = Mock()
    item = detail('a1', html + 'News', subject='News')
    assert discovery._candidate(messages, item) is None


def test_direct_receipt_action_survives_generic_footer_appeal():
    assert discovery.detect('קבלה על התרומה', 'להורדת הקבלה מספר 19473. תרמו עכשיו')
    assert discovery.detect('Donation receipt', 'Download your receipt #19473. Donate now.')[1] == '19473'


def test_unknown_account_stops_before_gmail_and_output(monkeypatch, tmp_path):
    auth = Mock()
    monkeypatch.setattr(discovery, 'get_gmail_credentials', auth)
    with pytest.raises(ValueError, match='configured_account_directory_missing'):
        discovery.run_discovery(tmp_path/'accounts', tmp_path/'reports', 'typo', 2026)
    auth.assert_not_called()
    assert not (tmp_path/'reports').exists()


@pytest.fixture
def legacy_report(tmp_path):
    from openpyxl.worksheet.datavalidation import DataValidation

    legacy, destination = discovery._report_paths(tmp_path, 'second', 2026)
    legacy.parent.mkdir(parents=True)
    workbook = discovery._open_report(None, 'test@example.invalid', 'second')
    workbook['_scan']['A2'] = '2026-03-01T00:00:00+00:00'
    workbook['_scan']['A3'] = None
    workbook['_scan']['B6'] = 'review-metadata'
    sheet = workbook.active
    sheet.data_validations.dataValidation.clear()
    validation = DataValidation(type='list', formula1=discovery._LEGACY_STATUS_FORMULA)
    sheet.add_data_validation(validation)
    validation.add(discovery._STATUS_RANGE)
    for number, status in enumerate(('טופל', 'ממתין להורדה', 'כבר קיים במערכת', 'הערה אישית'), 2):
        sheet.append(['2026-01-01', 'sender', 'subject', 'reason', '', 'Gmail', status, f'a{number}'])
        sheet.cell(number, 6).hyperlink = f'https://mail.google.com/mail/u/0/#all/a{number}'
    workbook.save(legacy)
    workbook.close()
    return legacy, destination


def test_relocation_preserves_rows_status_links_state_and_is_idempotent(tmp_path, legacy_report):
    legacy, destination = legacy_report
    with discovery.closing(load_workbook(legacy)) as before:
        old_rows = list(before.active.values)
        links = [before.active[f'F{n}'].hyperlink.target for n in range(2, 6)]
    assert discovery.relocate_report(tmp_path, 'second', 2026) == 'relocated'
    assert not legacy.exists()
    with discovery.closing(load_workbook(destination)) as after:
        assert list(after.active.values) == old_rows
        assert [after.active[f'F{n}'].hyperlink.target for n in range(2, 6)] == links
        assert after['_scan']['A2'].value == '2026-03-01T00:00:00+00:00'
        assert after['_scan']['B6'].value == 'review-metadata'
        assert after['_scan']['A3'].value == 'second'
        validation, = after.active.data_validations.dataValidation
        assert validation.formula1 == '"ממתין לבדיקה,להורדה,הורד,נקלט,כבר קיים,לא רלוונטי"'
    content = destination.read_bytes()
    assert discovery.relocate_report(tmp_path, 'second', 2026) == 'already_relocated'
    assert destination.read_bytes() == content


def test_relocation_never_overwrites_independent_destination(tmp_path, legacy_report):
    legacy, destination = legacy_report
    destination.parent.mkdir(parents=True)
    source = legacy.read_bytes()
    destination.write_bytes(source)
    with pytest.raises(discovery.DiscoveryError, match='relocation_destination_exists'):
        discovery.relocate_report(tmp_path, 'second', 2026)
    assert destination.read_bytes() == legacy.read_bytes() == source


def test_relocation_retries_after_link_before_source_unlink(tmp_path, legacy_report, monkeypatch):
    from pathlib import Path

    legacy, destination = legacy_report
    unlink = Path.unlink
    with monkeypatch.context() as patch:
        def interrupted(path, *args, **kwargs):
            if path == legacy:
                raise PermissionError('interrupted')
            return unlink(path, *args, **kwargs)
        patch.setattr(Path, 'unlink', interrupted)
        with pytest.raises(PermissionError):
            discovery.relocate_report(tmp_path, 'second', 2026)
    assert legacy.samefile(destination)
    same_bytes = destination.read_bytes()
    assert discovery.relocate_report(tmp_path, 'second', 2026) == 'relocated'
    assert destination.read_bytes() == same_bytes and not legacy.exists()


def test_legacy_report_and_other_account_are_blocked_before_auth(tmp_path, legacy_report, monkeypatch):
    reports = tmp_path
    accounts = tmp_path / 'accounts'
    for account in ('second', 'other'):
        (accounts / account).mkdir(parents=True)
    auth = Mock()
    monkeypatch.setattr(discovery, 'get_gmail_credentials', auth)
    with pytest.raises(discovery.DiscoveryError, match='legacy_report_requires_relocation'):
        discovery.run_discovery(accounts, reports, 'other', 2026)
    discovery.relocate_report(reports, 'second', 2026)
    with pytest.raises(discovery.DiscoveryError, match='report_account_conflict'):
        discovery.run_discovery(accounts, reports, 'other', 2026)
    auth.assert_not_called()


def test_delta_appends_to_relocated_report_and_failure_is_atomic(tmp_path, legacy_report, monkeypatch):
    _, destination = legacy_report
    accounts = tmp_path / 'accounts'
    (accounts / 'second').mkdir(parents=True)
    discovery.relocate_report(tmp_path, 'second', 2026)
    messages = Mock()
    users = Mock()
    users.messages.return_value = messages
    users.getProfile.return_value = request({'emailAddress': 'test@example.invalid'})
    monkeypatch.setattr(discovery, 'build', lambda *_a, **_kw: SimpleNamespace(users=lambda: users))
    monkeypatch.setattr(discovery, 'get_gmail_credentials', lambda _: None)
    messages.get.return_value = request(detail('bb', '<p>קבלה מספר 19473</p><button>לצפייה</button>'))
    messages.list.side_effect = [request({'messages': [{'id': 'a2'}, {'id': 'bb'}], 'nextPageToken': 'next'}),
                                 RuntimeError('interrupted after accumulating an appended row')]
    original = destination.read_bytes()
    now = datetime(2026, 3, 5, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError):
        discovery.run_discovery(accounts, tmp_path, 'second', 2026, delta=True, now=now)
    assert destination.read_bytes() == original
    messages.list.side_effect = None
    messages.list.return_value = request({'messages': [{'id': 'a2'}, {'id': 'bb'}]})
    assert discovery.run_discovery(accounts, tmp_path, 'second', 2026, delta=True, now=now) == 1
    assert discovery.run_discovery(accounts, tmp_path, 'second', 2026, now=now) == 0
    with discovery.closing(load_workbook(destination)) as workbook:
        assert [r[-1] for r in list(workbook.active.values)[1:]] == ['a2', 'a3', 'a4', 'a5', 'bb']
        assert workbook.active['G2'].value == 'טופל'
        assert workbook.active['G6'].value == 'ממתין לבדיקה'
        assert workbook['_scan']['A2'].value == now.isoformat()


def test_second_legacy_account_cannot_overwrite_or_mix_with_relocated_report(tmp_path, legacy_report):
    from shutil import copyfile

    first, destination = legacy_report
    second, _ = discovery._report_paths(tmp_path, 'other', 2026)
    second.parent.mkdir(parents=True)
    copyfile(first, second)
    duplicate = second.read_bytes()
    discovery.relocate_report(tmp_path, 'second', 2026)
    moved = destination.read_bytes()
    with pytest.raises(discovery.DiscoveryError, match='relocation_destination_exists'):
        discovery.relocate_report(tmp_path, 'other', 2026)
    assert second.read_bytes() == duplicate and destination.read_bytes() == moved


def test_unexpected_status_validation_stops_before_relocation(tmp_path, legacy_report):
    legacy, destination = legacy_report
    with discovery.closing(load_workbook(legacy)) as workbook:
        workbook.active.data_validations.dataValidation[0].formula1 = '"custom"'
        workbook.save(legacy)
    original = legacy.read_bytes()
    with pytest.raises(discovery.DiscoveryError, match='status_validation_conflict'):
        discovery.relocate_report(tmp_path, 'second', 2026)
    assert legacy.read_bytes() == original and not destination.exists()


def test_conflicting_cli_modes_fail_before_relocation(monkeypatch, capsys):
    from src import main

    monkeypatch.setattr('sys.argv', [
        'src.main', '--mode', 'potential-receipts', '--account', 'second',
        '--year', '2026', '--relocate-report', '--delta',
    ])
    with pytest.raises(SystemExit) as stopped:
        main.main()
    assert stopped.value.code == 2
    assert '--relocate-report' in capsys.readouterr().err
