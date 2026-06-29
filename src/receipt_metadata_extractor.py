from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DATE_PATTERN = re.compile(r"(?<!\d)(\d{2})[./](\d{2})[./](\d{4})(?!\d)")

# Amount: ₪ prefix (standard), or total indicator (normal + normalizer-reversed forms)
# followed by optional shekel word then digits.
_AMOUNT_SHEKEL_PREFIX = re.compile(r"₪\s*([\d,]+(?:\.\d{1,2})?)")
_AMOUNT_TOTAL = re.compile(
    r'(?:סה"כ|הס"כ)\s*(?:₪|שקל|לקש)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.UNICODE,
)
_AMOUNT_SHEKEL_WORD = re.compile(r"(?:שקל|לקש)\s*([\d,]+(?:\.\d{1,2})?)", re.UNICODE)

_NINE_DIGITS = re.compile(r"\b(\d{9})\b")

# Explicit anchor patterns capturing (anchor, number) or (number, anchor).
# [^\S\n]* matches horizontal whitespace only — no newline crossing — so multi-line
# layouts fall through to the window-based search below.
# Longer alternatives are listed first to prevent a prefix from matching early.
_EXPLICIT_REG_BEFORE = re.compile(
    r'(מלכ"ר|מלכ״ר|מלכר'
    r'|עמותה[^\S\n]+רשומה|עמותה'
    r'|התומע[^\S\n]+המושר|התומע'   # reversed forms produced by normalize_hebrew_text
    r'|מספר[^\S\n]+עמותה|מספר[^\S\n]+תאגיד'
    r'|ע"ר|ח\.פ|ח"פ)'
    r'[^\S\n]*:?[^\S\n]*(\d{9})',
    re.UNICODE,
)
_EXPLICIT_REG_AFTER = re.compile(
    r'(\d{9})[^\S\n]*'
    r'(מלכ"ר|מלכ״ר|מלכר'
    r'|עמותה[^\S\n]+רשומה|עמותה'
    r'|התומע[^\S\n]+המושר|התומע'   # reversed forms produced by normalize_hebrew_text
    r'|מספר[^\S\n]+עמותה|מספר[^\S\n]+תאגיד'
    r'|ע"ר|ח\.פ|ח"פ)',
    re.UNICODE,
)
# "עמותה רשומה" may be separated from its number by a line break after the
# colon (e.g. "עמותה רשומה:\n580712348") — unlike the patterns above, \s here
# is allowed to cross newlines. Invisible RTL/LTR marks are stripped before
# matching since they can sit between the colon and the number.
_INVISIBLE_MARKS = re.compile(r'[‎‏]')
_AMUTA_RESHUMA = re.compile(r'עמותה\s+רשומה\s*:?\s*(\d{9})', re.MULTILINE | re.UNICODE)
# Anchors that designate a nonprofit (amuta/NGO) — require registration prefix 58.
# Includes reversed forms produced when normalize_hebrew_text processes already-logical-order text.
_NGO_ANCHOR_KEYWORDS = frozenset((
    'מלכ"ר', 'מלכ״ר', 'מלכר', 'עמותה', 'עמותה רשומה', 'ע"ר',
    'התומע', 'התומע המושר',  # reversed: עמותה → התומע, עמותה רשומה → התומע המושר
))

_ASSOC_ANCHOR = re.compile(
    r'(?:מספר\s+עמותה|מספר\s+תאגיד|עמותה|התומע|מוסד|ע"ר|ח\.פ|ח"פ|מלכ"ר|מלכ״ר|מלכר)',
    re.UNICODE,
)
# Valid Israeli corporate/nonprofit registration prefixes (51=company, 52=partnership, 55=cooperative, 58=amuta/NGO)
_VALID_ORG_PREFIXES = frozenset(("51", "52", "55", "58"))
# NGO context: when these anchors appear, prefer registration numbers starting with 58
_NGO_CONTEXT = re.compile(r'(?:עמותה|התומע|ע"ר|מלכ"ר|מלכ״ר|מלכר)', re.UNICODE)


def _is_valid_corporate_registration_number(value: str) -> bool:
    return len(value) == 9 and value.isdigit() and value[:2] in _VALID_ORG_PREFIXES

# Tax report number: three patterns handle the fragmented RTL layouts seen in real receipts.
# Pattern A: number appears before ": anchor" across lines  e.g. "5527\n: \nהמיסים"
_TAX_NUM_BEFORE = re.compile(r"(\d{3,8})\s*:\s*(?:המיסים|לרשות|דיווח)", re.UNICODE)
# Pattern B: anchor then colon then number on same stretch  e.g. "לרשות המיסים: 2188"
# Also handles compound labels: "רשות המיסים: ...", "דיווח התרומה: ...", "אישור דיווח: ..."
_TAX_ANCHOR_COLON = re.compile(
    r"(?:המיסים|לרשות|רשות\s+המיסים|דיווח\s+התרומה|אישור\s+דיווח)[^\n:]{0,60}:\s*(\d{3,8})\b",
    re.UNICODE,
)
# Pattern C: anchor on line, number on next line  e.g. "תושרל םיסימה\n32830"
# (reversed forms appear when the normalizer encounters already-correct PDF text)
_TAX_ANCHOR_NUM_AFTER = re.compile(
    r"(?:תושרל|םיסימה|המיסים|לרשות|רשות\s+המיסים|דיווח\s+התרומה|אישור\s+דיווח)[^\n]*\n\s*(\d{3,8})\b",
    re.UNICODE,
)
# Pattern D: reversed text where number precedes reversed "מספר" + tax-authority anchor
# e.g. "73982רפסמ רושיא חוויד המורתה תושרל םיסמה" ← reversed label+number layout
_TAX_NUM_BEFORE_LABEL = re.compile(
    r"(\d{3,8})\s*רפסמ\b[^\n]*(?:תושרל|יסמה)", re.UNICODE
)

_HEBREW_ONLY = re.compile(r'^[א-ת\s"\']+$', re.UNICODE)
_FILENAME_DATE = re.compile(r"^(\d{2})_(\d{2})_(\d{2})__")
_FILENAME_TRAILING_NUM = re.compile(r"_(\d+)(?:\.[^.]+)?$")
_FILENAME_MID_NUM = re.compile(r"_(\d{4,8})_")  # 4-8 digit segment between underscores
# Receipt number text fallbacks (used only when filename yields nothing)
# Specific phrase first: "קבלה תרומה 80806" / "מקור קבלה תרומה 80553"
_RECEIPT_NUM_DONATION = re.compile(r"(?:מקור\s+)?קבלה\s+תרומה\s+(\d{4,8})\b", re.UNICODE)
# Generic label fallback: "מספר קבלה: 80806" / "קבלה מס' 80806" / "קבלה מספר 80806"
_RECEIPT_NUM_LABEL = re.compile(
    r"(?:מספר\s+קבלה|קבלה\s+(?:מס'?|מספר))\s*:?\s*(\d{4,8})\b", re.UNICODE
)
# Reversed-text patterns (normalizer reverses already-correct PDF text)
# e.g. "62376 המורת תלבק רוקמ" ← reversed "מקור קבלה תרומה 62376"
_RECEIPT_NUM_REVERSED = re.compile(
    r"(\d{4,8})\s+המורת\s+(?:תלבק|הלבק)(?:\s+רוקמ)?", re.UNICODE
)
# e.g. "רוקמ260344הלבק רובע המורת" ← reversed "תרומה עבור קבלה 260344 מקור"
_RECEIPT_NUM_ADJACENT = re.compile(r"רוקמ(\d{4,8})(?:הלבק|תלבק|רובע)", re.UNICODE)

# Donor ID: explicit anchor required — bare 9-digit numbers are also registration numbers.
_DONOR_ID = re.compile(
    r'(?:ת"ז|ת\.ז\.?|תז|מספר\s+זהות|מס\'?\s*זהות|מ\.ז\.?|זהות)\s*:?\s*(\d{9})\b',
    re.UNICODE,
)
# Donor name: conservative anchors only; captures rest of line (best-effort).
_DONOR_NAME = re.compile(
    r'(?:שם\s+התורם|שם\s+המשלם|שם\s+מלא|לכבוד)\s*:?\s*(.+)',
    re.UNICODE,
)

_FIELD_HE = {
    "amount": "סכום",
    "receipt_date": "תאריך",
    "organization_name": "שם עמותה",
    "registration_number": "מספר עמותה",
    "receipt_number": "מספר קבלה",
    "tax_report_number": "מספר אישור דיווח",
}

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_NEEDS_REVIEW = "needs_review"


@dataclass
class ReceiptMetadata:
    file_name: str = ""
    organization_name: str = ""
    registration_number: str = ""
    receipt_number: str = ""
    tax_report_number: str = ""
    receipt_date: str = ""
    amount: str = ""
    donor_name: str = ""
    donor_id: str = ""
    donor_match: str = ""
    severity: str = ""
    extraction_status: str = STATUS_NEEDS_REVIEW
    notes: str = ""
    account: str = ""   # set by receipt_summary, not by extract_metadata


def _find_date_in_text(text: str) -> str | None:
    m = _DATE_PATTERN.search(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return None


def _find_date_in_filename(file_path: Path) -> tuple[str, str] | None:
    m = _FILENAME_DATE.match(file_path.name)
    if m:
        day, month, year_short = m.group(1), m.group(2), m.group(3)
        return f"{day}/{month}/20{year_short}", "תאריך מתוך שם הקובץ"
    return None


def _find_amount(text: str) -> str | None:
    m = _AMOUNT_SHEKEL_PREFIX.search(text)
    if m:
        return m.group(1).replace(",", "")
    m = _AMOUNT_TOTAL.search(text)
    if m:
        return m.group(1).replace(",", "")
    m = _AMOUNT_SHEKEL_WORD.search(text)
    if m:
        return m.group(1).replace(",", "")
    return None


def _is_ngo_anchor(anchor: str) -> bool:
    return re.sub(r'\s+', ' ', anchor).strip() in _NGO_ANCHOR_KEYWORDS


def _find_registration_number_by_explicit_anchor(text: str) -> str | None:
    """Return the first registration number adjacent to an explicit org anchor on the same line."""
    for m in _EXPLICIT_REG_BEFORE.finditer(text):
        anchor, number = m.group(1), m.group(2)
        if not _is_valid_corporate_registration_number(number):
            continue
        if _is_ngo_anchor(anchor) and not number.startswith("58"):
            continue
        return number
    for m in _EXPLICIT_REG_AFTER.finditer(text):
        number, anchor = m.group(1), m.group(2)
        if not _is_valid_corporate_registration_number(number):
            continue
        if _is_ngo_anchor(anchor) and not number.startswith("58"):
            continue
        return number
    for m in _AMUTA_RESHUMA.finditer(_INVISIBLE_MARKS.sub('', text)):
        number = m.group(1)
        if _is_valid_corporate_registration_number(number) and number.startswith("58"):
            return number
    return None


def _find_registration_number(text: str) -> str | None:
    # Explicit anchor match first — highest confidence, enforces NGO prefix 58.
    result = _find_registration_number_by_explicit_anchor(text)
    if result:
        return result

    is_ngo_text = bool(_NGO_CONTEXT.search(text))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _ASSOC_ANCHOR.search(line):
            window = "\n".join(lines[max(0, i - 5) : i + 6])
            candidates = [
                m.group(1) for m in _NINE_DIGITS.finditer(window)
                if _is_valid_corporate_registration_number(m.group(1))
            ]
            if not candidates:
                continue
            if _NGO_CONTEXT.search(window):
                ngo = [c for c in candidates if c.startswith("58")]
                if ngo:
                    return ngo[0]
                continue  # NGO context but no 58-prefix candidate — skip window
            return candidates[0]
    # Fallback: first valid 9-digit number; in NGO context, require 58 prefix.
    for m in _NINE_DIGITS.finditer(text):
        cand = m.group(1)
        if not _is_valid_corporate_registration_number(cand):
            continue
        if is_ngo_text and not cand.startswith("58"):
            continue
        return cand
    return None


def _find_receipt_number_from_filename(file_path: Path) -> str | None:
    m = _FILENAME_TRAILING_NUM.search(file_path.stem)
    if m:
        return m.group(1)
    m = _FILENAME_MID_NUM.search(file_path.stem)
    return m.group(1) if m else None


def _find_receipt_number_in_text(text: str) -> str | None:
    for pat in (_RECEIPT_NUM_DONATION, _RECEIPT_NUM_LABEL,
                _RECEIPT_NUM_REVERSED, _RECEIPT_NUM_ADJACENT):
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _find_tax_report_number(text: str) -> str | None:
    for pat in (_TAX_NUM_BEFORE, _TAX_ANCHOR_COLON,
                _TAX_ANCHOR_NUM_AFTER, _TAX_NUM_BEFORE_LABEL):
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _find_organization_name(text: str) -> str | None:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines[:15]:
        if _HEBREW_ONLY.match(line) and len(line) >= 4:
            return line
    return None


def _find_donor_id(text: str) -> str | None:
    m = _DONOR_ID.search(text)
    return m.group(1) if m else None


def _find_donor_name(text: str) -> str | None:
    m = _DONOR_NAME.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    if not name:
        return None
    if "@" in name:
        return None
    if re.fullmatch(r'[\d\s\-]+', name):
        return None
    name = re.sub(r'\s+\d{7,}.*$', '', name).strip()
    return name if name else None


def _first_match(finder, normalized: str, raw: str):
    """Try finder on normalized text; if not found, try raw text."""
    return finder(normalized) or (finder(raw) if raw else None)


def extract_metadata(normalized_text: str, file_path: Path, raw_text: str = "") -> ReceiptMetadata:
    meta = ReceiptMetadata(file_name=file_path.name)
    notes: list[str] = []

    meta.organization_name = _find_organization_name(normalized_text) or ""
    meta.registration_number = _first_match(_find_registration_number, normalized_text, raw_text) or ""
    meta.receipt_number = (
        _find_receipt_number_from_filename(file_path)
        or _first_match(_find_receipt_number_in_text, normalized_text, raw_text)
        or ""
    )
    meta.tax_report_number = _first_match(_find_tax_report_number, normalized_text, raw_text) or ""
    meta.amount = _first_match(_find_amount, normalized_text, raw_text) or ""
    meta.donor_name = _first_match(_find_donor_name, normalized_text, raw_text) or ""
    meta.donor_id = _first_match(_find_donor_id, normalized_text, raw_text) or ""

    if meta.donor_id and meta.registration_number and meta.donor_id == meta.registration_number:
        meta.registration_number = ""

    date = _first_match(_find_date_in_text, normalized_text, raw_text)
    if date:
        meta.receipt_date = date
    else:
        fallback = _find_date_in_filename(file_path)
        if fallback:
            meta.receipt_date, note = fallback
            notes.append(note)

    missing_critical: list[str] = []
    if not meta.amount:
        missing_critical.append(_FIELD_HE["amount"])
    if not meta.receipt_date:
        missing_critical.append(_FIELD_HE["receipt_date"])
    # organization_name is only critical when registration_number is also absent
    if not meta.organization_name and not meta.registration_number:
        missing_critical.append(_FIELD_HE["organization_name"])

    missing_noncritical: list[str] = []
    if not meta.organization_name and meta.registration_number:
        missing_noncritical.append(_FIELD_HE["organization_name"])
    if not meta.registration_number:
        missing_noncritical.append(_FIELD_HE["registration_number"])
    if not meta.receipt_number:
        missing_noncritical.append(_FIELD_HE["receipt_number"])
    if not meta.tax_report_number:
        missing_noncritical.append(_FIELD_HE["tax_report_number"])

    if missing_critical:
        meta.extraction_status = STATUS_NEEDS_REVIEW
        notes.insert(0, f"חסר: {', '.join(missing_critical)}")
    elif missing_noncritical:
        meta.extraction_status = STATUS_PARTIAL
    else:
        meta.extraction_status = STATUS_OK

    meta.notes = "; ".join(notes)
    return meta
