from unittest.mock import patch

from src.pdf_donation_validator import is_donation_pdf
from src.pdf_text_extractor import PdfExtractionError

_EXTRACTOR = "src.pdf_donation_validator.extract_text_from_pdf_bytes"


def _mock_text(text: str):
    return patch(_EXTRACTOR, return_value=text)


def test_strong_signal_hebrew_accepts():
    with _mock_text("קבלה על תרומה\n05/06/2026\n₪200"):
        valid, reason = is_donation_pdf(b"")
    assert valid is True
    assert "matched" in reason


def test_strong_signal_english_accepts():
    with _mock_text("Thank you for your donation receipt."):
        valid, reason = is_donation_pdf(b"")
    assert valid is True
    assert "matched" in reason


def test_negative_signal_rejects():
    with _mock_text("TAX INVOICE\nOrder #1234\nTotal: $50"):
        valid, reason = is_donation_pdf(b"")
    assert valid is False
    assert "rejected" in reason


def test_strong_beats_negative():
    with _mock_text("תרומה\nחשבונית"):
        valid, reason = is_donation_pdf(b"")
    assert valid is True


def test_no_signal_rejects():
    with _mock_text("Hello world. Nothing relevant here."):
        valid, reason = is_donation_pdf(b"")
    assert valid is False
    assert "no donation signal" in reason


def test_visual_hebrew_accepted_via_normalized():
    # "המורת" is the visual-order (reversed) form of "תרומה".
    # The normalizer reverses it back to logical Hebrew, making the signal matchable.
    with _mock_text("המורת 200"):
        valid, reason = is_donation_pdf(b"")
    assert valid is True


def test_extraction_failure_rejects():
    with patch(_EXTRACTOR, side_effect=PdfExtractionError("bad PDF")):
        valid, reason = is_donation_pdf(b"garbage")
    assert valid is False
    assert "could not extract" in reason


def test_empty_text_rejects():
    with _mock_text(""):
        valid, reason = is_donation_pdf(b"")
    assert valid is False
    assert "no donation signal" in reason


def test_seif_46_accepts():
    with _mock_text("אישור לפי סעיף 46 לפקודת מס הכנסה"):
        valid, reason = is_donation_pdf(b"")
    assert valid is True


def test_payment_receipt_rejects():
    with _mock_text("קבלת תשלום עבור שירותים"):
        valid, reason = is_donation_pdf(b"")
    assert valid is False
    assert "rejected" in reason
