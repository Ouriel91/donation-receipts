from pathlib import Path

from googleapiclient.discovery import build

from src.providers.gmail_auth import get_gmail_credentials

_QUERY = "newer_than:7d has:attachment"


class GmailProvider:
    def __init__(self, account_dir: Path) -> None:
        self.account_dir = account_dir

    def fetch_emails(self) -> list[dict]:
        creds = get_gmail_credentials(self.account_dir)
        service = build("gmail", "v1", credentials=creds)

        result = service.users().messages().list(userId="me", q=_QUERY).execute()

        emails = []
        for msg in result.get("messages", []):
            detail = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="full",
            ).execute()
            emails.append(_map_message(detail))
        return emails


def _map_message(detail: dict) -> dict:
    payload = detail.get("payload", {})
    headers = {
        h.get("name", ""): h.get("value", "")
        for h in payload.get("headers", [])
    }
    return {
        "message_id": detail["id"],
        "subject": headers.get("Subject", ""),
        "from": headers.get("From", ""),
        "date": _parse_date(headers.get("Date", "")),
        "body": detail.get("snippet", ""),
        "attachments": _extract_attachments(payload),
    }


def _extract_attachments(payload: dict) -> list[dict]:
    attachments = []
    for part in payload.get("parts", []):
        if part.get("parts"):
            attachments.extend(_extract_attachments(part))
            continue
        filename = part.get("filename", "")
        if not filename:
            continue
        attachments.append({
            "filename": filename,
            "content_type": part.get("mimeType", ""),
            "attachment_id": part.get("body", {}).get("attachmentId", ""),
        })
    return attachments


def _parse_date(raw: str) -> str:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return raw
