"""Synthetic shared-root checks; never touch the user's AppData."""
from datetime import datetime, timezone

import pytest

from src.private_runtime import registry_root
from src.registry_sync import refresh


def test_windows_default_and_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr('src.private_runtime.sys.platform', 'win32')
    monkeypatch.delenv('DONATION_RECEIPTS_REGISTRY_ROOT', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    expected = tmp_path / 'DonationReceiptsRuntime' / 'registry'
    assert registry_root() == expected
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(tmp_path / 'configured'))
    assert registry_root() == tmp_path / 'configured'
    assert registry_root(tmp_path / 'explicit') == tmp_path / 'explicit'
    monkeypatch.delenv('DONATION_RECEIPTS_REGISTRY_ROOT')
    monkeypatch.delenv('LOCALAPPDATA')
    with pytest.raises(ValueError, match='LOCALAPPDATA'):
        registry_root()


def test_refresh_default_reuses_configured_existing_registry(tmp_path, monkeypatch):
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(tmp_path / 'shared'))
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    raw = 'מספר עמותה,שם עמותה בעברית\n123456789,עמותה סינתטית\n'.encode()
    assert refresh(downloader=lambda: raw, clock=lambda: now)['status'] == 'success'
    # This is also the resolver imported by annual enrichment and Section 46 CLI.
    import src.enrich_2026 as annual
    import src.section46_cli as section
    assert annual.registry_root() == section.registry_root() == registry_root()
    live = registry_root() / 'section46-live.json'
    live.write_bytes(b'preserved local evidence')
    original = (registry_root() / 'organizations.json').read_bytes()
    assert refresh(downloader=lambda: pytest.fail('unnecessary download'), clock=lambda: now)['status'] == 'not_due'
    assert (registry_root() / 'organizations.json').read_bytes() == original
    assert live.read_bytes() == b'preserved local evidence'
