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


def test_hebrew_invoice_not_detected():
    email = {
        "subject": "חשבונית מס מספר 1234",
        "body": "מצורף חשבונית עבור השירות שקיבלת.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"


def test_english_invoice_not_detected():
    email = {
        "subject": "Invoice #1234",
        "body": "Thank you for your order. Please find your purchase details below.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"


def test_payment_receipt_not_detected():
    email = {
        "subject": "payment receipt",
        "body": "Your payment has been received.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"


def test_generic_receipt_without_donation_context():
    email = {
        "subject": "Your receipt",
        "body": "Here is your receipt for the service.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"


def test_delivery_email_not_detected():
    email = {
        "subject": "Your delivery is on the way",
        "body": "Your shipping confirmation. Delivery expected tomorrow.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is False
    assert result.confidence == "low"


def test_invoice_with_strong_donation_signal():
    email = {
        "subject": "Invoice - תרומה לעמותה",
        "body": "אישור תרומה עבורך. סעיף 46 מאושר.",
        "attachments": [],
    }

    result = detect_donation_email(email)

    assert result.is_donation is True
    assert result.confidence != "low"