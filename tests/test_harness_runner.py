import json
from pathlib import Path

import pytest

from src.harness_runner import run_harness
from src.providers.harness_provider import HarnessProvider

DONATION_EMAIL = {
    "message_id": "msg_001",
    "subject": "תודה על תרומתך",
    "from": "donations@example.org",
    "date": "2026-05-24",
    "body": "מצורפת קבלה עבור תרומתך.",
    "attachments": [{"filename": "receipt.pdf", "content_type": "application/pdf"}],
}

NORMAL_EMAIL = {
    "message_id": "msg_002",
    "subject": "Weekly Newsletter",
    "from": "news@example.com",
    "date": "2026-05-24",
    "body": "Welcome to this week's update.",
    "attachments": [],
}

DONATION_NO_ATTACHMENT = {
    "message_id": "msg_003",
    "subject": "Donation Confirmation",
    "from": "charity@example.org",
    "date": "2026-05-24",
    "body": "Thank you for your donation.",
    "attachments": [],
}

DONATION_UNSUPPORTED_ATTACHMENT = {
    "message_id": "msg_004",
    "subject": "Donation Confirmation",
    "from": "charity@example.org",
    "date": "2026-05-24",
    "body": "Thank you for your donation.",
    "attachments": [{"filename": "receipt.exe", "content_type": "application/octet-stream"}],
}


def write_email(directory: Path, email: dict) -> None:
    path = directory / f"{email['message_id']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(email, f, ensure_ascii=False)


def make_provider(directory: Path, *emails: dict) -> HarnessProvider:
    for email in emails:
        write_email(directory, email)
    return HarnessProvider(directory)


class TestRunHarness:
    def test_dry_run_writes_no_files(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, DONATION_EMAIL)

        receipts_dir = tmp_path / "receipts"
        manifest_path = tmp_path / "manifest.json"

        run_harness(provider, receipts_dir, manifest_path, dry_run=True)

        assert not receipts_dir.exists()

    def test_dry_run_writes_no_manifest(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, DONATION_EMAIL)

        manifest_path = tmp_path / "manifest.json"

        run_harness(provider, tmp_path / "receipts", manifest_path, dry_run=True)

        assert not manifest_path.exists()

    def test_skips_low_confidence(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, NORMAL_EMAIL)

        results = run_harness(
            provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert len(results) == 1
        assert results[0]["status"] == "skipped_low_confidence"

    def test_skips_already_processed(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()

        manifest_path = tmp_path / "manifest.json"
        receipts_dir = tmp_path / "receipts"

        run_harness(make_provider(harness_dir, DONATION_EMAIL), receipts_dir, manifest_path, dry_run=False)
        results = run_harness(HarnessProvider(harness_dir), receipts_dir, manifest_path, dry_run=False)

        assert results[0]["status"] == "skipped_duplicate"

    def test_processes_donation_email(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, DONATION_EMAIL)

        results = run_harness(
            provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert results[0]["status"] == "processed"
        assert results[0]["confidence"] == "high"
        assert len(results[0]["planned_paths"]) == 1
        assert results[0]["planned_paths"][0].endswith(".pdf")
        assert results[0]["skipped_attachments"] == []

    def test_skips_when_all_attachments_unsupported(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, DONATION_UNSUPPORTED_ATTACHMENT)

        manifest_path = tmp_path / "manifest.json"

        results = run_harness(
            provider, tmp_path / "receipts", manifest_path, dry_run=False
        )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert len(results[0]["skipped_attachments"]) == 1
        assert not manifest_path.exists()

    def test_marks_processed_when_not_dry_run(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, DONATION_EMAIL)

        manifest_path = tmp_path / "manifest.json"

        run_harness(provider, tmp_path / "receipts", manifest_path, dry_run=False)

        assert manifest_path.exists()
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["message_id"] == "msg_001"
        assert entries[0]["account"] == "harness"

    def test_medium_confidence_no_attachment_processed(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        provider = make_provider(harness_dir, DONATION_NO_ATTACHMENT)

        results = run_harness(
            provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert results[0]["confidence"] == "medium"
        assert results[0]["planned_paths"] == []

    def test_accepts_fake_provider(self, tmp_path):
        class FakeProvider:
            def fetch_emails(self) -> list[dict]:
                return [DONATION_EMAIL]

        results = run_harness(
            FakeProvider(), tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert results[0]["status"] == "processed"
