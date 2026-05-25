from dataclasses import dataclass


DONATION_KEYWORDS = [
    "תרומה",
    "קבלה",
    "אישור תרומה",
    "תרומתך",
    "סעיף 46",
    "donation",
    "receipt",
    "tax receipt",
    "thank you for your donation",
]


@dataclass
class DetectionResult:
    is_donation: bool
    confidence: str
    reasons: list[str]


def detect_donation_email(email: dict) -> DetectionResult:
    subject = str(email.get("subject", "")).lower()
    body = str(email.get("body", "")).lower()
    attachments = email.get("attachments", [])

    text = f"{subject} {body}"

    matched_keywords = [
        keyword for keyword in DONATION_KEYWORDS
        if keyword.lower() in text
    ]

    has_supported_attachment = any(
        str(att.get("filename", "")).lower().endswith((".pdf", ".jpg", ".jpeg", ".png"))
        for att in attachments
    )

    reasons = []

    for keyword in matched_keywords:
        reasons.append(f"matched keyword: {keyword}")

    if has_supported_attachment:
        reasons.append("has supported attachment")

    is_donation = bool(matched_keywords)

    if matched_keywords and has_supported_attachment:
        confidence = "high"
    elif matched_keywords:
        confidence = "medium"
    else:
        confidence = "low"

    return DetectionResult(
        is_donation=is_donation,
        confidence=confidence,
        reasons=reasons,
    )