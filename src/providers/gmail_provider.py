import base64
from src.logger import stage, emit, metric, current
import json
import http.client
import ssl
import socket
import errno
from google.auth.exceptions import TransportError
from httplib2 import ServerNotFoundError
from datetime import datetime, timezone
import time
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.providers.gmail_auth import get_gmail_credentials

_QUERY = "newer_than:7d has:attachment"
_RETRY_DELAYS = (5, 10, 20, 40, 80)


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
            retryable = error.resp.status in (408, 429, 500, 502, 503, 504) or (error.resp.status == 403 and reasons.intersection(
                {"rateLimitExceeded", "userRateLimitExceeded"}
            ))
            if not retryable or attempt == len(_RETRY_DELAYS):
                raise
        except OSError as error:
            if (not isinstance(error, (ConnectionError, TimeoutError, socket.gaierror, ssl.SSLEOFError))
                    and error.errno not in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE}
                    and getattr(error, 'winerror', None) not in {10050, 10051, 10052, 10053, 10054, 10060, 10065}):
                raise
            if attempt == len(_RETRY_DELAYS):
                raise
        except (http.client.IncompleteRead,
                http.client.RemoteDisconnected, ssl.SSLEOFError, socket.gaierror,
                TransportError, ServerNotFoundError):
            if attempt == len(_RETRY_DELAYS):
                raise
        delay = _RETRY_DELAYS[attempt]
        print(f"[GMAIL] Transient connection/server failure; retry {attempt + 1}/{len(_RETRY_DELAYS)} in {delay}s")
        run = current()
        emit(run.phase if run is not None else 'GMAIL', 'retry', attempt=attempt+1, delay_seconds=delay)
        time.sleep(delay)


def verify_mailbox(service, account_dir):
    """Check the actual Gmail profile before accessing messages or attachments."""
    from src.private_runtime import safe_path

    binding = safe_path(account_dir / 'mailbox.txt')
    expected = binding.read_text(encoding='utf-8').strip().casefold()
    profile = _execute_with_rate_limit_retry(service.users().getProfile(userId='me'))
    if not expected or profile.get('emailAddress', '').casefold() != expected:
        raise ValueError('Gmail profile does not match the configured mailbox')


class GmailProvider:
    def __init__(self, account_dir: Path, query: str | None = None) -> None:
        self.account_dir = account_dir
        self._query = query or _QUERY
        self._verified_service = None

    def _service(self):
        if self._verified_service is None:
            from src.private_runtime import safe_path
            safe_path(self.account_dir / 'mailbox.txt').read_bytes()
            creds = get_gmail_credentials(self.account_dir)
            service = build('gmail', 'v1', credentials=creds)
            verify_mailbox(service, self.account_dir)
            self._verified_service = service
        return self._verified_service

    def fetch_emails(self) -> list[dict]:
        service = self._service()

        all_messages = []
        pages = 0
        stage('GMAIL')
        page_token = None
        while True:
            kwargs: dict = {"userId": "me", "q": self._query}
            if page_token:
                kwargs["pageToken"] = page_token
            request = service.users().messages().list(**kwargs)
            result = _execute_with_rate_limit_retry(request)
            all_messages.extend(result.get("messages", []))
            pages += 1
            metric('messages_found', len(all_messages), absolute=True)
            emit('GMAIL', 'page', pages=pages, messages_found=len(all_messages))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        emails = []
        emit('GMAIL', 'details', details_completed=0, messages_total=len(all_messages))
        for detail_index, msg in enumerate(all_messages, 1):
            request = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="full",
            )
            detail = _execute_with_rate_limit_retry(request)
            if detail.get('id') != msg['id']:
                raise ValueError('Gmail identity changed')
            emails.append(_map_message(detail))
            if detail_index % 50 == 0 or detail_index == len(all_messages):
                emit('GMAIL', 'details', details_completed=detail_index, messages_total=len(all_messages))
        return emails

    def fetch_emails_resumable(self, progress, persist):
        """Persist IDs and next token together, then cache each full descriptor.

        An expired Gmail page token restarts listing in the SAME bounded query;
        IDs and completed descriptors remain deduplicated and need no refetch.
        """
        service = self._service()
        stage('GMAIL')
        while not progress.get('listed'):
            kwargs = {'userId': 'me', 'q': progress['query']}
            if progress.get('next_page'):
                kwargs['pageToken'] = progress['next_page']
            try:
                result = _execute_with_rate_limit_retry(service.users().messages().list(**kwargs))
            except HttpError as exc:
                if exc.resp.status == 400 and progress.get('next_page') and not progress.get('token_reset'):
                    progress.update(next_page=None, token_reset=True)
                    persist()
                    continue
                raise
            for message in result.get('messages', []):
                key = message['id']
                if not isinstance(key, str) or not key:
                    raise ValueError('Invalid Gmail identity')
                progress.setdefault('ids', {}).setdefault(key, None)
            progress['next_page'] = result.get('nextPageToken')
            progress['listed'] = not bool(progress['next_page'])
            progress['pages'] = progress.get('pages', 0) + 1
            persist()
            emit('GMAIL', 'page', pages=progress['pages'], messages_found=len(progress.get('ids', {})))
        completed = sum(value is not None for value in progress.get('ids', {}).values())
        emit('GMAIL', 'details', details_completed=completed, messages_total=len(progress.get('ids', {})))
        for key, value in progress.get('ids', {}).items():
            if value is not None:
                continue
            try:
                detail = _execute_with_rate_limit_retry(service.users().messages().get(
                    userId='me', id=key, format='full'))
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                # Preserve a durable unavailable candidate, NOT a processed donation.
                progress['ids'][key] = {'message_id': key, 'discovery_unavailable': 'message_not_found'}
                completed += 1
                persist()
                continue
            mapped = _map_message(detail)
            if mapped['message_id'] != key:
                raise ValueError('Gmail identity changed')
            progress['ids'][key] = mapped
            persist()
            completed += 1
            if completed % 50 == 0 or completed == len(progress['ids']):
                emit('GMAIL', 'details', details_completed=completed, messages_total=len(progress['ids']))
        return list(progress.get('ids', {}).values())

    def fetch_message(self, message_id: str) -> dict:
        """Refresh only a durable failed item, even outside the search overlap."""
        service = self._service()
        detail = _execute_with_rate_limit_retry(service.users().messages().get(
            userId='me', id=message_id, format='full'))
        return _map_message(detail)

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        service = self._service()
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
        "arrival_date": _parse_arrival_date(detail.get("internalDate"), headers.get("Date", "")),
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


def _parse_arrival_date(internal_date, header_date):
    """Gmail arrival controls storage only; donation date controls tax reporting."""
    if internal_date is not None:
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000, timezone.utc).strftime('%Y-%m-%d')
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return _parse_date(header_date)
