from pathlib import Path
import hashlib
from src.logger import stage, emit, metric, snapshot, note_exception

from src.attachment_saver import plan_receipt_path, save_attachment
from src.manifest import was_processed, mark_processed, remember_attachment_origin, recover_attachment_origins
from src.pdf_donation_validator import is_donation_pdf
from src.providers.gmail_provider import GmailProvider
from src.receipt_detector import detect_donation_email


def run_gmail(
    provider: GmailProvider,
    receipts_dir: Path,
    manifest_path: Path,
    dry_run: bool,
    reprocess: bool = False,
    resumable: bool = False,
    pdf_only: bool = False,
    recover_origins: bool = False,
) -> list[dict]:
    account = provider.account_dir.name

    print("[GMAIL] Loading emails...")

    stage('GMAIL')
    emails = provider.fetch_emails()
    metric('messages_total', len(emails), absolute=True)
    stage('DOWNLOAD', messages_total=len(emails))
    print(f"[GMAIL] Found {len(emails)} emails")
    print()

    results = []

    for message_index, email in enumerate(emails):
        metric('messages_scanned', message_index, absolute=True)
        if message_index and message_index % 50 == 0:
            snapshot('DOWNLOAD', 'batch', 'messages_scanned', 'messages_total', 'new_files', 'existing_files', 'failed_files', 'rejected_files')
        message_id = email.get("message_id", "unknown")

        if not reprocess and was_processed(message_id, account, manifest_path):
            if recover_origins and resumable and not dry_run:
                recover_attachment_origins(provider, manifest_path, account, receipts_dir, email)
            metric('skipped_messages')
            print(f"[MSG {message_id}] SKIP - already processed")
            results.append({
                "message_id": message_id,
                "status": "skipped_duplicate",
                "dry_run": dry_run,
            })
            continue

        detection = detect_donation_email(email)

        if detection.confidence == "low":
            metric('rejected_messages')
            print(f"[MSG {message_id}] SKIP - low confidence")
            results.append({
                "message_id": message_id,
                "status": "skipped_low_confidence",
                "confidence": detection.confidence,
                "planned_paths": [],
                "dry_run": dry_run,
            })
            continue

        date_value = email.get("arrival_date", email.get("date", "")) if resumable else email.get("date", "")
        attachments = email.get("attachments", [])
        planned_paths = []
        skipped_attachments = []
        download_failed = False

        for attachment in attachments:
            filename = attachment.get("filename", "")
            attachment_id = attachment.get("attachment_id", "")

            if pdf_only and not filename.lower().endswith('.pdf'):
                metric('rejected_files')
                skipped_attachments.append('unsupported sync attachment (PDF required)')
                continue

            try:
                target_path = plan_receipt_path(
                    base_dir=receipts_dir,
                    account=account,
                    date_value=date_value,
                    original_filename=filename,
                )
            except ValueError as e:
                metric('failed_files' if resumable else 'rejected_files')
                if resumable:
                    download_failed = True  # Invalid arrival/path must remain retryable, never terminal.
                skipped_attachments.append(str(e))
                continue

            if dry_run:
                content = b""
            else:
                try:
                    content = provider.download_attachment(message_id, attachment_id)
                except Exception as e:
                    metric('failed_files')
                    note_exception(e)
                    download_failed = True
                    skipped_attachments.append(f"download failed for {filename}: {e}")
                    continue

                if filename.lower().endswith(".pdf"):
                    valid, reason = is_donation_pdf(content)
                    if not valid:
                        metric('rejected_files')
                        skipped_attachments.append(
                            f"PDF content rejected ({reason}): {filename}"
                        )
                        continue

            if resumable and not dry_run:
                # Physical identity is content, independent of changing Gmail attachment IDs.
                target_path = target_path.parent / (hashlib.sha256(content).hexdigest() + Path(filename).suffix.lower())
            if resumable and target_path.exists() and not dry_run:
                if target_path.read_bytes() != content:
                    raise ValueError('Existing receipt differs; refusing overwrite')
                metric('existing_files')
            elif resumable and not dry_run:
                from src.private_runtime import new_bytes
                new_bytes(target_path, content)
                metric('new_files')
            else:
                save_attachment(content, target_path, dry_run=dry_run)
                if not dry_run:
                    metric('new_files')
            if resumable and not dry_run and filename.lower().endswith('.pdf'):
                remember_attachment_origin(manifest_path, account, target_path, content, filename)
            if resumable and not dry_run and callable(getattr(type(provider), 'attachment_saved', None)):
                provider.attachment_saved(attachment_id, target_path, content)
            planned_paths.append(str(target_path))

        if not planned_paths:
            if resumable and not dry_run and not download_failed:
                mark_processed(account, message_id, [], manifest_path)
            print(f"[MSG {message_id}] SKIP - no supported attachments")
            for reason in skipped_attachments:
                print(f"  skipped attachment: {reason}")
            results.append({
                "message_id": message_id,
                "status": "skipped_no_supported_attachments",
                "confidence": detection.confidence,
                "planned_paths": [],
                "skipped_attachments": skipped_attachments,
                "retry_required": download_failed,
                "dry_run": dry_run,
            })
            continue

        dry_run_label = " [DRY RUN]" if dry_run else ""
        print(f"[MSG {message_id}] PROCESSED (confidence: {detection.confidence})")
        for path in planned_paths:
            print(f"  ->{dry_run_label} {path}")
        for reason in skipped_attachments:
            print(f"  skipped attachment: {reason}")

        if not dry_run and not download_failed:
            mark_processed(account, message_id, planned_paths, manifest_path)

        results.append({
            "message_id": message_id,
            "status": "processed",
            "confidence": detection.confidence,
            "planned_paths": planned_paths,
            "skipped_attachments": skipped_attachments,
            "retry_required": download_failed,
            "dry_run": dry_run,
        })
    metric('messages_scanned', len(emails), absolute=True)
    snapshot('DOWNLOAD', 'complete', 'messages_scanned', 'messages_total', 'new_files', 'existing_files', 'failed_files', 'rejected_files')
    return results
