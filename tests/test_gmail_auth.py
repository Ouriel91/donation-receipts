from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from src.providers.gmail_auth import SCOPES, get_gmail_credentials


def test_loads_existing_valid_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")

    mock_creds = MagicMock()
    mock_creds.valid = True

    with patch("src.providers.gmail_auth.Credentials.from_authorized_user_file", return_value=mock_creds):
        result = get_gmail_credentials(tmp_path)

    assert result is mock_creds


def test_refreshes_expired_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh-token-value"
    mock_creds.to_json.return_value = '{"token": "refreshed"}'

    with patch("src.providers.gmail_auth.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("src.providers.gmail_auth.Request") as mock_request:
        result = get_gmail_credentials(tmp_path)

    mock_creds.refresh.assert_called_once_with(mock_request.return_value)
    assert result is mock_creds


def test_runs_browser_flow_when_no_token(tmp_path):
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}")

    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "new"}'
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = mock_creds

    with patch("src.providers.gmail_auth.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow):
        result = get_gmail_credentials(tmp_path)

    mock_flow.run_local_server.assert_called_once_with(port=0)
    assert result is mock_creds


def test_persists_token_after_browser_flow(tmp_path):
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}")

    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "new"}'
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = mock_creds

    with patch("src.providers.gmail_auth.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow):
        get_gmail_credentials(tmp_path)

    token_path = tmp_path / "token.json"
    assert token_path.exists()
    assert token_path.read_text() == '{"token": "new"}'


def test_refresh_error_falls_back_to_browser_flow(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}")

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "old-refresh-token"
    mock_creds.refresh.side_effect = RefreshError("Token has been expired or revoked.")

    new_creds = MagicMock()
    new_creds.to_json.return_value = '{"token": "new-from-browser"}'
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = new_creds

    with patch("src.providers.gmail_auth.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("src.providers.gmail_auth.Request"), \
         patch("src.providers.gmail_auth.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow):
        result = get_gmail_credentials(tmp_path)

    mock_flow.run_local_server.assert_called_once_with(port=0)
    assert result is new_creds


def test_refresh_error_persists_new_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}")

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "old-refresh-token"
    mock_creds.refresh.side_effect = RefreshError("Token has been expired or revoked.")

    new_creds = MagicMock()
    new_creds.to_json.return_value = '{"token": "new-from-browser"}'
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = new_creds

    with patch("src.providers.gmail_auth.Credentials.from_authorized_user_file", return_value=mock_creds), \
         patch("src.providers.gmail_auth.Request"), \
         patch("src.providers.gmail_auth.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow):
        get_gmail_credentials(tmp_path)

    assert token_file.read_text() == '{"token": "new-from-browser"}'
