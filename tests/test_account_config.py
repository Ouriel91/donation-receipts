import json
import pytest
from src.account_config import load_accounts, get_enabled_accounts


def write_config(tmp_path, data):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_load_accounts_returns_all(tmp_path):
    path = write_config(tmp_path, {"accounts": [{"name": "primary", "enabled": True}, {"name": "secondary", "enabled": False}]})
    accounts = load_accounts(path)
    assert len(accounts) == 2


def test_get_enabled_accounts_filters_disabled(tmp_path):
    path = write_config(tmp_path, {"accounts": [{"name": "primary", "enabled": True}, {"name": "secondary", "enabled": False}]})
    enabled = get_enabled_accounts(path)
    assert len(enabled) == 1
    assert enabled[0]["name"] == "primary"


def test_get_enabled_accounts_all_disabled(tmp_path):
    path = write_config(tmp_path, {"accounts": [{"name": "primary", "enabled": False}]})
    assert get_enabled_accounts(path) == []


def test_get_enabled_accounts_missing_enabled_field(tmp_path):
    path = write_config(tmp_path, {"accounts": [{"name": "primary"}]})
    assert get_enabled_accounts(path) == []


def test_load_accounts_missing_file():
    with pytest.raises(FileNotFoundError):
        load_accounts("/nonexistent/accounts.json")


def test_load_accounts_malformed_json(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_accounts(str(path))


def test_load_accounts_missing_key(tmp_path):
    path = write_config(tmp_path, {"wrong_key": []})
    with pytest.raises(ValueError, match="accounts"):
        load_accounts(str(path))
