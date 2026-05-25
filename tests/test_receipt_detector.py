from src.receipt_detector import detect_donation_email


def test_detects_hebrew_donation_with_pdf():
    email = {
        "subject": "תודה על תרומתך",
        "body": "מצורפת קבלה עבור תרומתך.",
        "attachments": [
            {"filename": "receipt.pdf", "content_type": "application/pdf"}
        ],
    }

    result = detect_donation_email(email)

    assert result.is_donation is True
    assert result.confidence == "high"
    assert "has supported attachment" in result.reasons


def test_detects_english_donation_without_attachment():
    email = {
        "subject": "Donation Confirmation",
        "body": "Thank you for your donation.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is True
    assert result.confidence == "medium"


def test_ignores_normal_email():
    email = {
        "subject": "Weekly Newsletter",
        "body": "Welcome to this week's update.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"
    assert result.reasons == []


def test_supported_attachment_alone_is_not_enough():
    email = {
        "subject": "Monthly report",
        "body": "Attached is your file.",
        "attachments": [
            {"filename": "document.pdf", "content_type": "application/pdf"}
        ],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"
    assert result.reasons == ["has supported attachment"]