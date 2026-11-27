from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

from openpyxl import Workbook, load_workbook
import pytest

from src import main
from src.migrate_users import migrate
from src.providers.gmail_provider import verify_mailbox
from src.section46_live import LiveSection46
from src.report_layout import COLUMN_HEADERS_HE
from src.user_scope import UserScope
from tests.test_sync import document, env, mail, total


def register(project, user):
    scope = UserScope.open(user, project)
    scope.create()
    scope.configure('primary', f'{user}@example.test')
    return scope


@pytest.mark.parametrize('name', ['../other', 'manual', 'CoMbInEd', 'NUL', 'a/b', 'x:y'])
def test_identity_rejects_unsafe_names(tmp_path, name):
    with pytest.raises(ValueError):
        UserScope.open(name, tmp_path)
    with pytest.raises(ValueError):
        register(tmp_path, 'valid').account(name)


def test_account_links_and_mailbox_ownership(tmp_path):
    a = register(tmp_path, 'alpha')
    b = register(tmp_path, 'beta')
    with pytest.raises(ValueError):
        b.configure('second', 'ALPHA@example.test')
    service = Mock()
    service.users().getProfile().execute.return_value = {'emailAddress': 'alpha@example.test'}
    verify_mailbox(service, a.account('primary'))
    with pytest.raises(ValueError):
        verify_mailbox(service, b.account('primary'))
    (b.account('primary') / 'token.json').symlink_to(a.account('primary') / 'token.json')
    with pytest.raises(ValueError):
        b.require_account('primary')


def test_two_users_full_delta_and_review_via_cli(env, monkeypatch):
    base, registry, provider, network = env
    monkeypatch.chdir(base)
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))
    a = register(base, 'alpha')
    b = register(base, 'beta')

    def bind(path, query):
        provider.account_dir = path
        return provider

    monkeypatch.setattr(main, 'GmailProvider', bind)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr('src.delta_sync.LiveSection46', lambda path, cache: LiveSection46(
        path, cache, transport=network, clock=lambda: now, sleep=lambda _: None))

    def cli(scope, mode, *options):
        monkeypatch.setattr(main.UserScope, 'open', lambda _: scope)
        monkeypatch.setattr('sys.argv', ['receipts', '--user', scope.user,
            '--mode', mode, '--account', 'primary', '--year', '2026', *options])
        main.main()

    mail(provider, document('71234', '100', '21/03/2026'), msg='same')
    cli(a, 'full')
    path_a = a.reports / 'primary/2026/donation_summary_2026.xlsx'
    path_b = b.reports / 'primary/2026/donation_summary_2026.xlsx'
    before = path_a.read_bytes()
    assert not (a.runtime / 'organizations.json').exists()
    with path_a.open('rb') as stream:
        wb = load_workbook(stream)
        sheet = wb['מרץ']
        provenance = COLUMN_HEADERS_HE['organization_identity_provenance']
        column = next(cell.column for cell in sheet[1] if cell.value == provenance)
        assert '580000001' in json.dumps(json.loads(sheet.cell(2, column).value))
        wb.close()
    cache_before = (registry / 'organizations.json').read_bytes()
    mail(provider, document('81754', '75', '22/03/2026'), msg='same')
    cli(b, 'sync', '--delta')
    assert (b.runtime / 'delta-primary.json').is_file()
    state_a = a.runtime / 'sync-primary.json'
    a_before = state_a.read_bytes()
    with pytest.raises(SystemExit):
        cli(b, 'identifier-review', '--refresh-identifier', next(iter(json.loads(a_before)['documents'])))
    assert state_a.read_bytes() == a_before
    assert path_a.read_bytes() == before
    assert (registry / 'organizations.json').read_bytes() == cache_before
    for path, expected in [(path_a, 100), (path_b, 75)]:
        wb = load_workbook(path)
        try:
            assert total(wb) == expected
        finally:
            wb.close()


def test_migration_preserves_canonical_corrections_and_identity(tmp_path):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    runtime = old / 'private'
    account = old / 'accounts/primary'
    account.mkdir(parents=True)
    runtime.mkdir()
    pdf = old / 'receipts/primary/2026/01_January/receipt.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'synthetic receipt')
    state = {'version': 1, 'documents': {'a': {'logical_id': 'preserved',
             'approved_amount': '99', 'paths': ['2026/01_January/receipt.pdf']}}}
    (runtime / 'sync-primary.json').write_text(json.dumps(state))
    book = old / 'reports/primary/2026/donation_summary_2026.xlsx'
    book.parent.mkdir(parents=True)
    wb = Workbook()
    wb.active['A1'] = 'Protected edit'
    wb.active['A2'] = '=40+59'
    original_root = tmp_path / 'original-project'
    original_ref = original_root / pdf.relative_to(old)
    wb.active['A3'].hyperlink = original_ref.as_uri()
    wb.active['A4'] = f'=HYPERLINK("{original_ref.as_uri()}","Source")'
    wb.create_sheet('Archive')['A1'] = 'Preserved publication'
    wb.save(book)
    wb.close()
    before = book.read_bytes()
    args = old, runtime, new, 'alpha', 'primary', 'alpha@example.test'
    assert migrate(*args) == 3
    assert not new.exists()
    assert migrate(*args, apply=True, legacy_link_root=original_root) == 3
    assert book.read_bytes() == before
    scope = UserScope.open('alpha', new)
    scope.require()
    scope.require_account('primary')
    assert json.loads((scope.runtime / 'sync-primary.json').read_bytes()) == state
    migrated = load_workbook(scope.reports / 'primary/2026/donation_summary_2026.xlsx')
    assert migrated.active['A1'].value == 'Protected edit'
    assert migrated.active['A2'].value == '=40+59'
    assert migrated.active['A3'].hyperlink.target == str(scope.receipts / pdf.relative_to(old / 'receipts'))
    assert migrated.active['A4'].value == f'=HYPERLINK("{scope.receipts / pdf.relative_to(old / "receipts")}","Source")'
    assert scope.identity_scope('primary') == 'primary'
    assert migrated['Archive']['A1'].value == 'Preserved publication'
    migrated.close()
    with pytest.raises(FileExistsError):
        migrate(*args, apply=True)


def test_potential_queue_uses_account_message_keys_and_checkpoints(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src import potential_receipts as discovery
    from tests.test_potential_receipts import detail, request

    scope = register(tmp_path, 'alpha')
    scope.configure('second', 'second@example.test')
    users = Mock()
    messages = users.messages.return_value
    monkeypatch.setattr(discovery, 'build', lambda *a, **kw: SimpleNamespace(users=lambda: users))
    monkeypatch.setattr(discovery, 'get_gmail_credentials',
                        lambda path: (path / 'mailbox.txt').read_text().strip())
    messages.list.return_value = request({'messages': [{'id': 'aa'}]})
    messages.get.return_value = request(detail('aa', '<p>קבלה מספר 19473</p><button>לצפייה</button>'))

    for account, day, expected in [('primary', 5, 1), ('second', 10, 1), ('primary', 5, 0)]:
        users.getProfile.return_value = request({'emailAddress': (scope.account(account) / 'mailbox.txt').read_text().strip()})
        count = discovery.run_discovery(scope.accounts, scope.reports, account, 2026, scoped=True,
                                       now=datetime(2026, 3, day, tzinfo=timezone.utc))
        assert count == expected

    path = scope.reports / 'manual/2026/potential_receipts_2026.xlsx'
    with path.open('rb') as stream:
        wb = load_workbook(stream)
        assert [row[7:] for row in list(wb.active.values)[1:]] == [('aa', 'primary'), ('aa', 'second')]
        assert wb['_scan']['B2'].value == '2026-03-10T00:00:00+00:00'
        assert wb['_scan']['D1'].value == 'account-checkpoints-v1'
        wb.close()


def test_pdf_link_resolution_never_reads_another_account(tmp_path):
    from src.workbook_maintenance import resolve_encoding_target

    a = register(tmp_path, 'alpha')
    b = register(tmp_path, 'beta')
    target = b.receipts / 'primary/receipt.pdf'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'synthetic document')
    assert resolve_encoding_target(
        target.as_uri(), a.reports, permitted_root=a.receipts / 'primary'
    ) == (None, 'source_outside_account')



def test_failed_migration_preserves_legacy_and_leaves_no_visible_copy(tmp_path):
    source = tmp_path / 'old'
    runtime = source / 'legacy-runtime'
    (source / 'accounts' / 'primary').mkdir(parents=True)
    runtime.mkdir()
    original = runtime / 'sync-primary.json'
    original.write_text('{"version":1}')
    workbook = source / 'reports/primary/2026/donation_summary_2026.xlsx'
    workbook.parent.mkdir(parents=True)
    wb = Workbook()
    wb.active['A1'] = 'Unmapped source'
    wb.active['A1'].hyperlink = (source / 'missing.pdf').as_uri()
    wb.save(workbook)
    wb.close()
    destination = tmp_path / 'new'
    args = source, runtime, destination, 'alpha', 'primary', 'alpha@example.test'
    with pytest.raises(ValueError, match='could not be mapped'):
        migrate(*args, apply=True)
    assert original.read_text() == '{"version":1}'
    assert not destination.exists()
    assert not list(tmp_path.glob('.receipt-migration-*'))


def test_rescan_uses_explicit_user_scope(tmp_path, monkeypatch):
    from src import rescan

    scope = register(tmp_path, 'alpha')
    monkeypatch.setattr(UserScope, 'open', lambda _: scope)
    run = Mock(return_value={})
    monkeypatch.setattr(rescan, 'run_rescan', run)
    monkeypatch.setattr('sys.argv', ['rescan', '--user', 'alpha', '--account', 'primary', '--year', '2026', '--plan'])
    assert rescan.main() == 0
    assert run.call_args.kwargs['root'] == scope.runtime
    assert run.call_args.kwargs['provider'].account_dir == scope.account('primary')
