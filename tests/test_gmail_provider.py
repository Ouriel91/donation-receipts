import pytest

from src.providers.gmail_provider import GmailProvider


def test_fetch_emails_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        GmailProvider().fetch_emails()


def test_has_fetch_emails_method():
    assert callable(getattr(GmailProvider(), "fetch_emails", None))
