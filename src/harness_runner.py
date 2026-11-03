from pathlib import Path

from src.attachment_saver import plan_receipt_path, save_attachment
from src.manifest import was_processed, mark_processed
from src.providers.email_provider import EmailProvider
from src.receipt_detector import detect_donation_email

HARNESS_ACCOUNT = "harness"


def run_harness(
    provider: EmailProvider,
    receipts_dir: Path,
    manifest_path: Path,
    dry_run: bool,
) -> list[dict]:
    print("[HARNESS] Loading emails...")

    emails = provider.fetch_emails()
    print(f"[HARNESS] Found {len(emails)} emails")
    print()

    results = []
    processed_count = 0
    skipped_count = 0

    for email in emails:
        message_id = email.get("message_id", "unknown")

        if was_processed(message_id, HARNESS_ACCOUNT, manifest_path):
            print(f"[MSG {message_id}] SKIP - already processed")
            results.append({
                "message_id": message_id,
                "status": "skipped_duplicate",
                "dry_run": dry_run,
            })
            skipped_count += 1
            continue

        detection = detect_donation_email(email)

        if detection.confidence == "low":
            print(f"[MSG {message_id}] SKIP - low confidence")
            results.append({
                "message_id": message_id,
                "status": "skipped_low_confidence",
                "confidence": detection.confidence,
                "planned_paths": [],
                "dry_run": dry_run,
            })
            skipped_count += 1
            continue

        date_value = email.get("date", "")
        attachments = email.get("attachments", [])
        planned_paths = []
        skipped_attachments = []

        for attachment in attachments:
            filename = attachment.get("filename", "")
            try:
                target_path = plan_receipt_path(
                    base_dir=receipts_dir,
                    account=HARNESS_ACCOUNT,
                    date_value=date_value,
                    original_filename=filename,
                )
                save_attachment(b"", target_path, dry_run=dry_run)
                planned_paths.append(str(target_path))
            except ValueError as e:
                skipped_attachments.append(str(e))

        if not planned_paths:
            print(f"[MSG {message_id}] SKIP - no supported attachments")
            for reason in skipped_attachments:
                print(f"  skipped attachment: {reason}")
            results.append({
                "message_id": message_id,
                "status": "skipped_no_supported_attachments",
                "confidence": detection.confidence,
                "planned_paths": [],
                "skipped_attachments": skipped_attachments,
                "dry_run": dry_run,
            })
            skipped_count += 1
            continue

        dry_run_label = " [DRY RUN]" if dry_run else ""
        print(f"[MSG {message_id}] PROCESSED (confidence: {detection.confidence})")
        for path in planned_paths:
            print(f"  ->{dry_run_label} {path}")
        for reason in skipped_attachments:
            print(f"  skipped attachment: {reason}")

        if not dry_run:
            mark_processed(HARNESS_ACCOUNT, message_id, planned_paths, manifest_path)

        results.append({
            "message_id": message_id,
            "status": "processed",
            "confidence": detection.confidence,
            "planned_paths": planned_paths,
            "skipped_attachments": skipped_attachments,
            "dry_run": dry_run,
        })
        processed_count += 1

    print()
    print(f"[HARNESS] Done. {processed_count} processed, {skipped_count} skipped (dry_run={dry_run})")

    return results
