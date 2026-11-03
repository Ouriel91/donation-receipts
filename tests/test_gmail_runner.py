import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.gmail_runner import run_gmail
from src.providers.gmail_provider import GmailProvider

DONATION_EMAIL = {
    "message_id": "msg_g01",
    "subject": "תודה על תרומתך",
    "from": "donations@example.org",
    "date": "2026-05-24",
    "body": "מצורפת קבלה עבור תרומתך.",
    "attachments": [
        {"filename": "receipt.pdf", "content_type": "application/pdf", "attachment_id": "att001"}
    ],
}

NORMAL_EMAIL = {
    "message_id": "msg_g02",
    "subject": "Weekly Newsletter",
    "from": "news@example.com",
    "date": "2026-05-24",
    "body": "Welcome to this week's update.",
    "attachments": [],
}

DONATION_NO_ATTACHMENT = {
    "message_id": "msg_g03",
    "subject": "Donation Confirmation",
    "from": "charity@example.org",
    "date": "2026-05-24",
    "body": "Thank you for your donation.",
    "attachments": [],
}

DONATION_UNSUPPORTED_ATTACHMENT = {
    "message_id": "msg_g04",
    "subject": "Donation Confirmation",
    "from": "charity@example.org",
    "date": "2026-05-24",
    "body": "Thank you for your donation.",
    "attachments": [
        {"filename": "receipt.exe", "content_type": "application/octet-stream", "attachment_id": "att004"}
    ],
}

DONATION_JPG_ATTACHMENT = {
    "message_id": "msg_g05",
    "subject": "תודה על תרומתך",
    "from": "donations@example.org",
    "date": "2026-05-24",
    "body": "מצורפת קבלה עבור תרומתך.",
    "attachments": [
        {"filename": "receipt.jpg", "content_type": "image/jpeg", "attachment_id": "att005"}
    ],
}

_VALIDATOR = "src.gmail_runner.is_donation_pdf"


def make_mock_provider(
    account_dir: Path,
    emails: list[dict],
    attachment_bytes: bytes = b"fake-pdf-bytes",
) -> MagicMock:
    mock = MagicMock(spec=GmailProvider)
    mock.account_dir = account_dir
    mock.fetch_emails.return_value = emails
    mock.download_attachment.return_value = attachment_bytes
    return mock


class TestRunGmail:
    def test_dry_run_writes_no_files(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])
        receipts_dir = tmp_path / "receipts"

        run_gmail(provider, receipts_dir, tmp_path / "manifest.json", dry_run=True)

        assert not receipts_dir.exists()

    def test_dry_run_writes_no_manifest(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])
        manifest_path = tmp_path / "manifest.json"

        run_gmail(provider, tmp_path / "receipts", manifest_path, dry_run=True)

        assert not manifest_path.exists()

    def test_skips_low_confidence(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [NORMAL_EMAIL])

        results = run_gmail(
            provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert len(results) == 1
        assert results[0]["status"] == "skipped_low_confidence"

    def test_skips_already_processed(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        receipts_dir = tmp_path / "receipts"
        manifest_path = tmp_path / "manifest.json"

        with patch(_VALIDATOR, return_value=(True, "mocked")):
            run_gmail(make_mock_provider(account_dir, [DONATION_EMAIL]), receipts_dir, manifest_path, dry_run=False)
            results = run_gmail(make_mock_provider(account_dir, [DONATION_EMAIL]), receipts_dir, manifest_path, dry_run=False)

        assert results[0]["status"] == "skipped_duplicate"

    def test_processes_donation_email(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])

        results = run_gmail(
            provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True
        )

        assert results[0]["status"] == "processed"
        assert results[0]["confidence"] == "high"
        assert len(results[0]["planned_paths"]) == 1
        assert results[0]["planned_paths"][0].endswith(".pdf")
        assert results[0]["skipped_attachments"] == []

    def test_skips_when_all_attachments_unsupported(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_UNSUPPORTED_ATTACHMENT])
        manifest_path = tmp_path / "manifest.json"

        results = run_gmail(
            provider, tmp_path / "receipts", manifest_path, dry_run=False
        )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert len(results[0]["skipped_attachments"]) == 1
        assert not manifest_path.exists()

    def test_marks_processed_when_not_dry_run(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])
        manifest_path = tmp_path / "manifest.json"

        with patch(_VALIDATOR, return_value=(True, "mocked")):
            run_gmail(provider, tmp_path / "receipts", manifest_path, dry_run=False)

        assert manifest_path.exists()
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["message_id"] == "msg_g01"
        assert entries[0]["account"] == "myaccount"

    def test_download_called_with_correct_args(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])

        with patch(_VALIDATOR, return_value=(True, "mocked")):
            run_gmail(provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=False)

        provider.download_attachment.assert_called_once_with("msg_g01", "att001")

    def test_download_not_called_on_dry_run(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])

        run_gmail(provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=True)

        provider.download_attachment.assert_not_called()

    def test_reprocess_saves_attachment_when_file_exists(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        receipts_dir = tmp_path / "receipts"
        manifest_path = tmp_path / "manifest.json"

        with patch(_VALIDATOR, return_value=(True, "mocked")):
            run_gmail(make_mock_provider(account_dir, [DONATION_EMAIL]), receipts_dir, manifest_path, dry_run=False)
            results = run_gmail(make_mock_provider(account_dir, [DONATION_EMAIL]), receipts_dir, manifest_path, dry_run=False, reprocess=True)

        assert results[0]["status"] != "skipped_duplicate"
        assert len(results[0]["planned_paths"]) > 0

    def test_reprocess_saves_attachment_when_file_missing(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        receipts_dir = tmp_path / "receipts"
        manifest_path = tmp_path / "manifest.json"

        with patch(_VALIDATOR, return_value=(True, "mocked")):
            run_gmail(make_mock_provider(account_dir, [DONATION_EMAIL]), receipts_dir, manifest_path, dry_run=False)
            for f in receipts_dir.rglob("*.pdf"):
                f.unlink()
            results = run_gmail(make_mock_provider(account_dir, [DONATION_EMAIL]), receipts_dir, manifest_path, dry_run=False, reprocess=True)

        assert results[0]["status"] != "skipped_duplicate"
        assert len(list(receipts_dir.rglob("*.pdf"))) > 0

    def test_download_failure_skips_attachment(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])
        provider.download_attachment.side_effect = ValueError("API error")
        manifest_path = tmp_path / "manifest.json"

        results = run_gmail(
            provider, tmp_path / "receipts", manifest_path, dry_run=False
        )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert any("download failed" in r for r in results[0]["skipped_attachments"])
        assert not manifest_path.exists()

    def test_pdf_content_rejected_skips_attachment(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_EMAIL])
        manifest_path = tmp_path / "manifest.json"

        with patch(_VALIDATOR, return_value=(False, "rejected: invoice")):
            results = run_gmail(
                provider, tmp_path / "receipts", manifest_path, dry_run=False
            )

        assert results[0]["status"] == "skipped_no_supported_attachments"
        assert any("PDF content rejected" in r for r in results[0]["skipped_attachments"])
        assert not manifest_path.exists()

    def test_non_pdf_attachment_not_validated(self, tmp_path):
        account_dir = tmp_path / "myaccount"
        provider = make_mock_provider(account_dir, [DONATION_JPG_ATTACHMENT])

        with patch(_VALIDATOR) as mock_validator:
            run_gmail(provider, tmp_path / "receipts", tmp_path / "manifest.json", dry_run=False)

        mock_validator.assert_not_called()
