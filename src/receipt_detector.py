from dataclasses import dataclass


STRONG_DONATION_KEYWORDS = [
    # Hebrew
    "תרומה",
    "קבלה על תרומה",
    "אישור תרומה",
    "תרומתך",
    "סעיף 46",
    "עמותה",
    'מלכ"ר',
    # English
    "donation",
    "donation receipt",
    "tax deductible",
    "charity",
    "thank you for your donation",
]

GENERIC_POSITIVE_KEYWORDS = [
    "קבלה",
    "receipt",
    "tax receipt",
]

NEGATIVE_KEYWORDS = [
    # Hebrew
    "חשבונית",
    "חשבונית מס",
    # English
    "invoice",
    "tax invoice",
    "order",
    "purchase",
    "shipping",
    "delivery",
    "payment receipt",
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

    matched_strong = [k for k in STRONG_DONATION_KEYWORDS if k.lower() in text]
    matched_generic = [k for k in GENERIC_POSITIVE_KEYWORDS if k.lower() in text]
    matched_negative = [k for k in NEGATIVE_KEYWORDS if k.lower() in text]

    has_strong = bool(matched_strong)
    has_generic = bool(matched_generic)
    has_negative = bool(matched_negative)

    has_supported_attachment = any(
        str(att.get("filename", "")).lower().endswith((".pdf", ".jpg", ".jpeg", ".png"))
        for att in attachments
    )

    reasons = []
    for keyword in matched_strong:
        reasons.append(f"matched keyword: {keyword}")
    for keyword in matched_generic:
        reasons.append(f"matched keyword: {keyword}")
    for keyword in matched_negative:
        reasons.append(f"negative signal: {keyword}")
    if has_supported_attachment:
        reasons.append("has supported attachment")

    if has_negative and not has_strong:
        confidence = "low"
    elif has_strong and has_supported_attachment:
        confidence = "high"
    elif has_strong:
        confidence = "medium"
    elif has_generic and not has_negative and has_supported_attachment:
        confidence = "medium"
    else:
        confidence = "low"

    is_donation = confidence != "low"

    return DetectionResult(
        is_donation=is_donation,
        confidence=confidence,
        reasons=reasons,
    )
