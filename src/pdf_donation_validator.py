from src.hebrew_text_normalizer import normalize_hebrew_text
from src.pdf_text_extractor import extract_text_from_pdf_bytes

_PDF_STRONG_SIGNALS = [
    "תרומה",
    "קבלה תרומה",
    "קבלה על תרומה",
    "סעיף 46",
    "אישור דיווח התרומה",
    "לרשות המיסים",
    "donation",
    "donation receipt",
    "tax deductible",
    "charity",
]

_PDF_NEGATIVE_SIGNALS = [
    "invoice",
    "receipt/invoice",
    "payment receipt",
    "קבלת תשלום",
    "אישור תשלום",
    "חשבונית",
    "חשבונית מס",
]


def is_donation_pdf(content: bytes) -> tuple[bool, str]:
    try:
        raw_text = extract_text_from_pdf_bytes(content)
    except Exception as e:
        return False, f"could not extract PDF text: {e}"

    normalized = normalize_hebrew_text(raw_text)
    # Search both raw and normalized to cover PDFs with visual or logical Hebrew ordering
    combined = (raw_text + "\n" + normalized).lower()

    for signal in _PDF_STRONG_SIGNALS:
        if signal.lower() in combined:
            return True, f"matched: {signal}"

    for signal in _PDF_NEGATIVE_SIGNALS:
        if signal.lower() in combined:
            return False, f"rejected: {signal}"

    return False, "no donation signal found"
