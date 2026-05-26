import json
from pathlib import Path

import pytest

from src.harness_runner import load_harness_emails, run_harness

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


class TestLoadHarnessEmails:
    def test_returns_all_emails(self, tmp_path):
        write_email(tmp_path, DONATION_EMAIL)
        write_email(tmp_path, NORMAL_EMAIL)

        emails = load_harness_emails(tmp_path)

        assert len(emails) == 2
        ids = {e["message_id"] for e in emails}
        assert ids == {"msg_001", "msg_002"}

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_harness_emails(tmp_path / "nonexistent")


class TestRunHarness:
    def test_dry_run_writes_no_files(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_EMAIL)

        receipts_dir = tmp_path / "receipts"
        manifest_path = tmp_path / "manifest.json"

        run_harness(harness_dir, receipts_dir, manifest_path, dry_run=True)

        assert not receipts_dir.exists()

    def test_dry_run_writes_no_manifest(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_EMAIL)

        manifest_path = tmp_path / "manifest.json"

        run_harness(harness_dir, tmp_path / "receipts", manifest_path, dry_run=True)

        assert not manifest_path.exists()

    def test_skips_low_confidence(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, NORMAL_EMAIL)

        results = run_harness(
            harness_dir, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert len(results) == 1
        assert results[0]["status"] == "skipped_low_confidence"

    def test_skips_already_processed(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_EMAIL)

        manifest_path = tmp_path / "manifest.json"
        receipts_dir = tmp_path / "receipts"

        # First run — real write to populate manifest
        run_harness(harness_dir, receipts_dir, manifest_path, dry_run=False)

        # Second run — should skip
        results = run_harness(harness_dir, receipts_dir, manifest_path, dry_run=False)

        assert results[0]["status"] == "skipped_duplicate"

    def test_processes_donation_email(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_EMAIL)

        results = run_harness(
            harness_dir, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert results[0]["status"] == "processed"
        assert results[0]["confidence"] == "high"
        assert len(results[0]["planned_paths"]) == 1
        assert results[0]["planned_paths"][0].endswith(".pdf")
        assert results[0]["skipped_attachments"] == []

    def test_skips_when_all_attachments_unsupported(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_UNSUPPORTED_ATTACHMENT)

        manifest_path = tmp_path / "manifest.json"

        results = run_harness(
            harness_dir, tmp_path / "receipts", manifest_path, dry_run=False
        )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert len(results[0]["skipped_attachments"]) == 1
        assert not manifest_path.exists()

    def test_marks_processed_when_not_dry_run(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_EMAIL)

        manifest_path = tmp_path / "manifest.json"

        run_harness(harness_dir, tmp_path / "receipts", manifest_path, dry_run=False)

        assert manifest_path.exists()
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["message_id"] == "msg_001"
        assert entries[0]["account"] == "harness"

    def test_medium_confidence_no_attachment_processed(self, tmp_path):
        harness_dir = tmp_path / "emails"
        harness_dir.mkdir()
        write_email(harness_dir, DONATION_NO_ATTACHMENT)

        results = run_harness(
            harness_dir, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert results[0]["confidence"] == "medium"
        assert results[0]["planned_paths"] == []
