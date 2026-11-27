from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.main import _run_product
from src.potential_receipts import run_discovery
from src.providers.gmail_provider import GmailProvider
from src.receipt_metadata_extractor import ReceiptMetadata


def test_product_preflights_every_account_before_running_full(tmp_path, monkeypatch):
    accounts = tmp_path / 'accounts'
    (accounts / 'primary').mkdir(parents=True)
    (accounts / 'secondary').mkdir()
    scope = SimpleNamespace(user='synthetic', accounts=accounts, project=tmp_path,
                            require_account=Mock(side_effect=[None, ValueError('invalid account')]))
    run = Mock()
    monkeypatch.setattr('subprocess.run', run)
    with pytest.raises(ValueError, match='invalid account'):
        _run_product(scope, 2026, None)
    run.assert_not_called()


def test_product_runs_full_and_potential_then_manual_and_combined(tmp_path, monkeypatch):
    accounts = tmp_path / 'accounts'
    (accounts / 'primary').mkdir(parents=True)
    scope = SimpleNamespace(user='synthetic', accounts=accounts, project=tmp_path,
                            require_account=Mock())
    run = Mock()
    monkeypatch.setattr('subprocess.run', run)
    _run_product(scope, 2026, None)
    commands = [call.args[0] for call in run.call_args_list]
    assert [c[c.index('--mode') + 1] for c in commands] == [
        'full', 'potential-receipts', 'manual', 'combined']
    assert all(c[c.index('--user') + 1] == 'synthetic' for c in commands)


def test_full_discovery_rejects_mismatched_message_after_transient_retry(monkeypatch):
    provider = GmailProvider(Path('synthetic'))
    service = Mock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {'messages': [{'id': 'expected'}]}
    messages.get.return_value.execute.side_effect = [
        TimeoutError(), {'id': 'different'}]
    monkeypatch.setattr(provider, '_service', lambda: service)
    monkeypatch.setattr('src.providers.gmail_provider.time.sleep', lambda _: None)
    with pytest.raises(ValueError, match='Gmail identity changed'):
        provider.fetch_emails()
    assert messages.get.return_value.execute.call_count == 2


def test_scoped_discovery_rejects_linked_mailbox_before_credentials(tmp_path, monkeypatch):
    accounts = tmp_path / 'accounts'
    folder = accounts / 'primary'
    folder.mkdir(parents=True)
    target = tmp_path / 'mailbox-fixture'
    target.write_text('synthetic@example.invalid', encoding='utf-8')
    try:
        (folder / 'mailbox.txt').symlink_to(target)
    except OSError:
        pytest.skip('symbolic links unavailable')
    credentials = Mock()
    monkeypatch.setattr('src.potential_receipts.get_gmail_credentials', credentials)
    with pytest.raises(ValueError, match='Link in operational path'):
        run_discovery(accounts, tmp_path / 'reports', 'primary', 2026, scoped=True)
    credentials.assert_not_called()


def test_phase5_scope_metadata_loads_without_inventing_proof():
    meta = ReceiptMetadata(receipt_series='fixture', receipt_terminal='terminal')
    assert (meta.receipt_series, meta.receipt_terminal) == ('fixture', 'terminal')
    assert not meta.receipt_scope_checked
