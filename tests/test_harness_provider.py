import json
from pathlib import Path

import pytest

from src.providers.harness_provider import HarnessProvider

EMAIL_A = {"message_id": "msg_001", "subject": "Receipt", "from": "a@example.com", "date": "2026-05-24", "body": "", "attachments": []}
EMAIL_B = {"message_id": "msg_002", "subject": "Other",   "from": "b@example.com", "date": "2026-05-24", "body": "", "attachments": []}


def write_email(directory: Path, email: dict) -> None:
    path = directory / f"{email['message_id']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(email, f, ensure_ascii=False)


class TestHarnessProvider:
    def test_returns_all_emails(self, tmp_path):
        write_email(tmp_path, EMAIL_A)
        write_email(tmp_path, EMAIL_B)

        emails = HarnessProvider(tmp_path).fetch_emails()

        assert len(emails) == 2
        assert {e["message_id"] for e in emails} == {"msg_001", "msg_002"}

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            HarnessProvider(tmp_path / "nonexistent").fetch_emails()

    def test_empty_dir_returns_empty_list(self, tmp_path):
        assert HarnessProvider(tmp_path).fetch_emails() == []
