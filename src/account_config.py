from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AccountConfig:
    display_name: str
    expected_donor_names: list[str] = field(default_factory=list)
    expected_donor_ids: list[str] = field(default_factory=list)


def load_account_config(accounts_dir: Path, account: str) -> AccountConfig | None:
    """Returns None gracefully when config.json is absent."""
    path = accounts_dir / account / "config.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return AccountConfig(
        display_name=data.get("display_name", account),
        expected_donor_names=data.get("expected_donor_names", []),
        expected_donor_ids=data.get("expected_donor_ids", []),
    )
