from pathlib import Path
from unittest.mock import MagicMock, patch

from src.providers.gmail_provider import GmailProvider, _QUERY, _extract_attachments, _map_message


def _make_service(messages=None, detail=None):
    mock_service = MagicMock()
    mock_service.users().messages().list().execute.return_value = (
        {"messages": messages} if messages is not None else {}
    )
    if detail is not None:
        mock_service.users().messages().get().execute.return_value = detail
    return mock_service


_DETAIL = {
    "id": "abc123",
    "snippet": "Test snippet",
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Test Subject"},
            {"name": "From", "value": "donor@example.com"},
            {"name": "Date", "value": "Mon, 01 Jan 2024 10:00:00 +0000"},
        ],
        "parts": [],
    },
}


def test_fetch_emails_returns_list(tmp_path):
    mock_service = _make_service(messages=[{"id": "abc123"}], detail=_DETAIL)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        result = GmailProvider(tmp_path).fetch_emails()
    assert isinstance(result, list)
    assert len(result) == 1


def test_fetch_emails_empty_inbox(tmp_path):
    mock_service = _make_service(messages=None)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        result = GmailProvider(tmp_path).fetch_emails()
    assert result == []


def test_fetch_emails_maps_fields_correctly(tmp_path):
    mock_service = _make_service(messages=[{"id": "abc123"}], detail=_DETAIL)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        result = GmailProvider(tmp_path).fetch_emails()
    email = result[0]
    assert email["message_id"] == "abc123"
    assert email["subject"] == "Test Subject"
    assert email["from"] == "donor@example.com"
    assert email["date"] == "2024-01-01"
    assert email["body"] == "Test snippet"
    assert email["attachments"] == []


def test_fetch_emails_date_parsing():
    detail = {
        "id": "xyz",
        "snippet": "",
        "payload": {
            "headers": [
                {"name": "Date", "value": "Fri, 15 Mar 2024 08:30:00 -0500"},
            ]
        },
    }
    result = _map_message(detail)
    assert result["date"] == "2024-03-15"


def test_no_attachments_returns_empty_list():
    payload = {
        "parts": [
            {"filename": "", "mimeType": "text/plain", "body": {}},
        ]
    }
    assert _extract_attachments(payload) == []


def test_single_pdf_attachment():
    payload = {
        "parts": [
            {
                "filename": "receipt.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "att001"},
            }
        ]
    }
    result = _extract_attachments(payload)
    assert result == [
        {"filename": "receipt.pdf", "content_type": "application/pdf", "attachment_id": "att001"}
    ]


def test_multiple_attachments():
    payload = {
        "parts": [
            {"filename": "doc.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
            {"filename": "scan.jpg", "mimeType": "image/jpeg", "body": {"attachmentId": "a2"}},
        ]
    }
    result = _extract_attachments(payload)
    assert len(result) == 2
    assert result[0]["filename"] == "doc.pdf"
    assert result[1]["filename"] == "scan.jpg"


def test_nested_parts_extracted():
    payload = {
        "parts": [
            {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "filename": "nested.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "a3"},
                    }
                ],
            }
        ]
    }
    result = _extract_attachments(payload)
    assert result == [
        {"filename": "nested.pdf", "content_type": "application/pdf", "attachment_id": "a3"}
    ]


def test_missing_filename_skipped():
    payload = {
        "parts": [
            {"filename": "", "mimeType": "text/html", "body": {}},
            {"filename": "keep.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a4"}},
        ]
    }
    result = _extract_attachments(payload)
    assert len(result) == 1
    assert result[0]["filename"] == "keep.pdf"


# --- download_attachment tests ---

def _make_attachment_service(data: str | None):
    mock_service = MagicMock()
    mock_service.users().messages().attachments().get().execute.return_value = (
        {"data": data} if data is not None else {}
    )
    return mock_service


def test_download_attachment_returns_bytes(tmp_path):
    import base64
    encoded = base64.urlsafe_b64encode(b"hello").decode()
    mock_service = _make_attachment_service(encoded)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        result = GmailProvider(tmp_path).download_attachment("msg1", "att1")
    assert isinstance(result, bytes)


def test_download_attachment_decodes_base64url(tmp_path):
    import base64
    raw = b"receipt content \xff\xfe"
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    mock_service = _make_attachment_service(encoded)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        result = GmailProvider(tmp_path).download_attachment("msg1", "att1")
    assert result == raw


def test_download_attachment_missing_data_raises(tmp_path):
    import pytest
    mock_service = _make_attachment_service(None)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        with pytest.raises(ValueError, match="No data in attachment response"):
            GmailProvider(tmp_path).download_attachment("msg1", "att1")


def test_download_attachment_calls_correct_api(tmp_path):
    import base64
    encoded = base64.urlsafe_b64encode(b"data").decode()
    mock_service = _make_attachment_service(encoded)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        GmailProvider(tmp_path).download_attachment("msgXYZ", "attABC")
    mock_service.users().messages().attachments().get.assert_any_call(
        userId="me", messageId="msgXYZ", id="attABC"
    )


# --- query parameter tests ---

def test_default_query_used(tmp_path):
    mock_service = _make_service(messages=None)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        GmailProvider(tmp_path).fetch_emails()
    mock_service.users().messages().list.assert_called_with(userId="me", q=_QUERY)


def test_custom_query_used(tmp_path):
    custom_query = "newer_than:90d has:attachment"
    mock_service = _make_service(messages=None)
    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        GmailProvider(tmp_path, query=custom_query).fetch_emails()
    mock_service.users().messages().list.assert_called_with(userId="me", q=custom_query)


def _make_detail(msg_id: str, subject: str) -> dict:
    return {
        "id": msg_id,
        "snippet": f"Snippet for {msg_id}",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "donor@example.com"},
                {"name": "Date", "value": "Mon, 01 Jan 2024 10:00:00 +0000"},
            ],
            "parts": [],
        },
    }


def test_fetch_emails_paginates_all_pages(tmp_path):
    mock_service = MagicMock()
    mock_service.users().messages().list().execute.side_effect = [
        {"messages": [{"id": "msg1"}], "nextPageToken": "token_page2"},
        {"messages": [{"id": "msg2"}]},
    ]
    mock_service.users().messages().get().execute.side_effect = [
        _make_detail("msg1", "Receipt Jan"),
        _make_detail("msg2", "Receipt Feb"),
    ]

    with patch("src.providers.gmail_provider.get_gmail_credentials"), \
         patch("src.providers.gmail_provider.build", return_value=mock_service):
        result = GmailProvider(tmp_path).fetch_emails()

    assert len(result) == 2
    assert result[0]["message_id"] == "msg1"
    assert result[1]["message_id"] == "msg2"
