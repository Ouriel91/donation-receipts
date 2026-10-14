import base64
import json
import time
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.providers.gmail_auth import get_gmail_credentials

_QUERY = "newer_than:7d has:attachment"
_RETRY_DELAYS = (5, 15, 30, 60, 120)


def _execute_with_rate_limit_retry(request):
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return request.execute()
        except HttpError as error:
            try:
                details = json.loads(error.content)
                reasons = {item.get("reason") for item in details.get("error", {}).get("errors", [])}
            except (ValueError, TypeError, AttributeError):
                reasons = set()
            retryable = error.resp.status in (403, 429) and reasons.intersection(
                {"rateLimitExceeded", "userRateLimitExceeded"}
            )
            if not retryable or attempt == len(_RETRY_DELAYS):
                raise
            delay = _RETRY_DELAYS[attempt]
            print(f"[GMAIL] Rate limit reached; retrying in {delay} seconds...")
            time.sleep(delay)


class GmailProvider:
    def __init__(self, account_dir: Path, query: str | None = None) -> None:
        self.account_dir = account_dir
        self._query = query or _QUERY

    def fetch_emails(self) -> list[dict]:
        creds = get_gmail_credentials(self.account_dir)
        service = build("gmail", "v1", credentials=creds)

        all_messages = []
        page_token = None
        while True:
            kwargs: dict = {"userId": "me", "q": self._query}
            if page_token:
                kwargs["pageToken"] = page_token
            request = service.users().messages().list(**kwargs)
            result = _execute_with_rate_limit_retry(request)
            all_messages.extend(result.get("messages", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        emails = []
        for msg in all_messages:
            request = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="full",
            )
            detail = _execute_with_rate_limit_retry(request)
            emails.append(_map_message(detail))
        return emails

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        creds = get_gmail_credentials(self.account_dir)
        service = build("gmail", "v1", credentials=creds)
        response = _execute_with_rate_limit_retry(
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
        )
        data = response.get("data")
        if not data:
            raise ValueError(
                f"No data in attachment response for message={message_id}, attachment={attachment_id}"
            )
        padding = (4 - len(data) % 4) % 4
        return base64.urlsafe_b64decode(data + "=" * padding)


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
