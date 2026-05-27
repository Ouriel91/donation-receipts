from pathlib import Path
from unittest.mock import MagicMock, patch

from src.providers.gmail_provider import GmailProvider, _map_message


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
        ]
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
