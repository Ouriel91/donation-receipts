import json
from pathlib import Path

import pytest

from src.account_config import AccountConfig, load_account_config


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# load_account_config
# ---------------------------------------------------------------------------


def test_returns_none_when_config_absent(tmp_path):
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    (accounts_dir / "testaccount").mkdir()

    result = load_account_config(accounts_dir, "testaccount")

    assert result is None


def test_loads_display_name(tmp_path):
    accounts_dir = tmp_path / "accounts"
    _write_config(
        accounts_dir / "testaccount" / "config.json",
        {"display_name": "Test Account"},
    )

    config = load_account_config(accounts_dir, "testaccount")

    assert config is not None
    assert config.display_name == "Test Account"


def test_display_name_defaults_to_account_when_absent(tmp_path):
    accounts_dir = tmp_path / "accounts"
    _write_config(
        accounts_dir / "testaccount" / "config.json",
        {},
    )

    config = load_account_config(accounts_dir, "testaccount")

    assert config is not None
    assert config.display_name == "testaccount"


def test_loads_expected_donor_names(tmp_path):
    accounts_dir = tmp_path / "accounts"
    _write_config(
        accounts_dir / "testaccount" / "config.json",
        {
            "display_name": "Test Account",
            "expected_donor_names": ["ישראל ישראלי", "Israel Israelson"],
        },
    )

    config = load_account_config(accounts_dir, "testaccount")

    assert config is not None
    assert config.expected_donor_names == ["ישראל ישראלי", "Israel Israelson"]


def test_loads_expected_donor_ids(tmp_path):
    accounts_dir = tmp_path / "accounts"
    _write_config(
        accounts_dir / "testaccount" / "config.json",
        {
            "display_name": "Test Account",
            "expected_donor_ids": ["000000001", "000000002"],
        },
    )

    config = load_account_config(accounts_dir, "testaccount")

    assert config is not None
    assert config.expected_donor_ids == ["000000001", "000000002"]


def test_missing_optional_fields_default_to_empty_lists(tmp_path):
    accounts_dir = tmp_path / "accounts"
    _write_config(
        accounts_dir / "testaccount" / "config.json",
        {"display_name": "Test Account"},
    )

    config = load_account_config(accounts_dir, "testaccount")

    assert config is not None
    assert config.expected_donor_names == []
    assert config.expected_donor_ids == []


def test_loads_all_fields_together(tmp_path):
    accounts_dir = tmp_path / "accounts"
    _write_config(
        accounts_dir / "primary" / "config.json",
        {
            "display_name": "Primary Account",
            "expected_donor_names": ["ראובן כהן"],
            "expected_donor_ids": ["111111118"],
        },
    )

    config = load_account_config(accounts_dir, "primary")

    assert config == AccountConfig(
        display_name="Primary Account",
        expected_donor_names=["ראובן כהן"],
        expected_donor_ids=["111111118"],
    )
