from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
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
_SHEKEL_ABBREVIATION = r'(?:ש["״]ח|ח["״]ש)'
_MONETARY_DECIMAL = r"([\d,]+\.\d{2})"
_AMOUNT_ABBREVIATION = (
    re.compile(rf"(?<!\w){_SHEKEL_ABBREVIATION}[^\S\n]*{_MONETARY_DECIMAL}(?![\d./-])"),
    re.compile(rf"(?<![\w./-]){_MONETARY_DECIMAL}[^\S\n]*{_SHEKEL_ABBREVIATION}(?!\w)"),
)
_AMOUNT_PATTERNS = (_AMOUNT_SHEKEL_PREFIX, _AMOUNT_TOTAL, _AMOUNT_SHEKEL_WORD, *_AMOUNT_ABBREVIATION)


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
    r'|מספר[^\S\n]+מוסד|רפסמ[^\S\n]+דסומ|דסומ[^\S\n]+רפסמ|מוסד[^\S\n]+מספר'
    r'|ע"ר|ח\.פ|ח"פ)'
    r'[^\S\n]*[:：]?[^\S\n]*(\d{9})(?!\d)',
    re.UNICODE,
)
_EXPLICIT_REG_AFTER = re.compile(
    r'(?<!\d)(\d{9})[^\S\n]*[:：]?[^\S\n]*'
    r'(מלכ"ר|מלכ״ר|מלכר'
    r'|עמותה[^\S\n]+רשומה|עמותה'
    r'|התומע[^\S\n]+המושר|התומע'   # reversed forms produced by normalize_hebrew_text
    r'|מספר[^\S\n]+עמותה|מספר[^\S\n]+תאגיד'
    r'|מספר[^\S\n]+מוסד|רפסמ[^\S\n]+דסומ|דסומ[^\S\n]+רפסמ|מוסד[^\S\n]+מספר'
    r'|ע"ר|ח\.פ|ח"פ)',
    re.UNICODE,
)
# "עמותה רשומה" may be separated from its number by a line break after the
# colon (e.g. "עמותה רשומה:\n580000001") — unlike the patterns above, \s here
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
    r'(?:מספר\s+עמותה|מספר\s+תאגיד|מספר\s+מוסד|רפסמ\s+דסומ|דסומ\s+רפסמ|מוסד\s+מספר|עמותה|התומע|מוסד|ע"ר|ח\.פ|ח"פ|מלכ"ר|מלכ״ר|מלכר)',
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
# Receipt numbers require a receipt-specific label before filename fallback.
# Specific phrase first: "קבלה תרומה 80806" / "מקור קבלה תרומה 80553"
_RECEIPT_NUM_DONATION = re.compile(
    r"(?:מקור\s+)?קבלה\s+תרומה\s+(\d{4,})(?![\w/_.-])", re.UNICODE
)
# Normal and normalizer-reversed labels; keep Latin IDs and separators intact.
_RECEIPT_NUM_LABEL = re.compile(
    r"(?:מספר\s+קבלה|רפסמ\s+הלבק|"
    r"(?:קבלה|הלבק|תלבק)\s*(?:מספר|רפסמ|מס['׳’]?|סמ['׳’]?|׳סמ|#))"
    r"\s*:?\s*([A-Za-z0-9]+(?:[-/_.][A-Za-z0-9]+)*)(?![\w/_.-])",
    re.UNICODE,
)
# Visual PDF order: the complete ID precedes "# קבלה" (possibly reversed).
_RECEIPT_NUM_LABEL_REVERSED = re.compile(
    r"(?<![\w/_.-])([A-Za-z0-9]+(?:[-/_.][A-Za-z0-9]+)*)(?![\w/_.-])"
    r"\s*:?\s*#\s*:?\s*(?:קבלה|קבלת|הלבק|תלבק)(?!\w)", re.UNICODE,
)
# "קבלה על תרומה" and its character-normalized form, with a complete ID token.
_RECEIPT_NUM_ON_DONATION = re.compile(
    r"(?<!\w)(?:(?:קבלה|קבלת)\s+על\s+תרומה|(?:הלבק|תלבק)\s+לע\s+המורת)"
    r"(?:\s*[:#]){0,2}\s*([A-Za-z0-9]+(?:[-/_.][A-Za-z0-9]+)*)(?![\w/_.-])",
    re.UNICODE,
)
# Visual PDF order can place the identifier before the reversed phrase.
_RECEIPT_NUM_ON_DONATION_REVERSED = re.compile(
    r"(?<![\w/_.-])([A-Za-z0-9]+(?:[-/_.][A-Za-z0-9]+)*)(?![\w/_.-])"
    r"(?:\s*[:#]){0,2}\s*(?:המורת\s+לע\s+(?:הלבק|תלבק)|תרומה\s+על\s+(?:קבלה|קבלת))(?!\w)",
    re.UNICODE,
)
# Reversed-text patterns (normalizer reverses already-correct PDF text)
# e.g. "62376 המורת תלבק רוקמ" ← reversed "מקור קבלה תרומה 62376"
# Require the start of a complete token, never a suffix of a longer identifier.
_RECEIPT_NUM_REVERSED = re.compile(
    r"(?<![\w/_.-])(\d{4,})\s+המורת\s+(?:תלבק|הלבק)(?:\s+רוקמ)?", re.UNICODE
)
# e.g. "רוקמ260344הלבק רובע המורת" ← reversed "תרומה עבור קבלה 260344 מקור"
_RECEIPT_NUM_ADJACENT = re.compile(r"רוקמ(\d{4,})(?:הלבק|תלבק|רובע)", re.UNICODE)

# Additional labelled identifiers. Keep these separate from the legacy search.
_ADDITIONAL_ID = r"([A-Za-z0-9]+(?:[-/_.][A-Za-z0-9]+)*)(?![\w/-]|[_.-]\w|,\d)"
_DASH = r"[-־‐‑‒–—−]"
_LABEL_SEPARATOR = rf"\s*(?:[:：#.,;]|{_DASH})?\s*"
_NUMBER_MARKER = r"(?:(?:מספר|רפסמ|מס[׳’']?|סמ[׳’']?|׳סמ)(?![א-ת])\s*)?"
_DONATION_HEADING = (
    rf"(?:קבל[הת](?:\s+(?:(?:עבור|על)\s+)?|\s*{_DASH}\s*)תרומה|"
    rf"(?:הלבק|תלבק)(?:\s+(?:(?:רובע|לע)\s+)?|\s*{_DASH}\s*)המורת)"
)
_ADDITIONAL_RECEIPT = re.compile(
    rf"(?<!\w){_DONATION_HEADING}(?!\w){_LABEL_SEPARATOR}"
    rf"{_NUMBER_MARKER}{_LABEL_SEPARATOR}{_ADDITIONAL_ID}"
)
_DASHED_RECEIPT_LABEL = re.compile(
    rf"(?<!\w)(?:קבלה\s+מספר|הלבק\s+רפסמ|רפסמ\s+הלבק|מספר\s+קבלה)"
    rf"\s*{_DASH}\s*{_ADDITIONAL_ID}"
)
_VISUAL_MARKED_RECEIPT = re.compile(
    rf"^[^\S\n]*{_ADDITIONAL_ID}[^\S\n]*"
    rf"(?:[:：#]|{_DASH})?[^\S\n]*"
    rf"(?:מספר\s+תרומה\s+על\s+קבלה|רפסמ\s+המורת\s+לע\s+הלבק|"
    rf"(?:{_DASH}[^\S\n]*)?(?:מספר\s+קבלה|רפסמ\s+הלבק)|"
    rf"(?:תרומה|המורת)[^\S\n]*{_DASH}[^\S\n]*(?:קבלה|הלבק))[^\S\n]*$", re.MULTILINE,
)
# PDF readers can put the original marker and number before either order
# of the dashed donation heading. Only match a complete header line.
_VISUAL_ORIGINAL_DONATION_RECEIPT = re.compile(
    rf"^[^\S\n]*(?:[:：]|{_DASH})?[^\S\n]*(?:מקור|רוקמ)"
    rf"[^\S\n]*(?:[:：#]|{_DASH})?[^\S\n]*([0-9]+)[^\S\n]+"
    rf"(?:קבלה[^\S\n]*{_DASH}[^\S\n]*תרומה|תרומה[^\S\n]*{_DASH}[^\S\n]*קבלה|"
    rf"הלבק[^\S\n]*{_DASH}[^\S\n]*המורת|המורת[^\S\n]*{_DASH}[^\S\n]*הלבק)"
    rf"[^\S\n]*(?:[:：]|{_DASH})?[^\S\n]*$", re.MULTILINE,
)
_MARKED_DONATION_RECEIPT = re.compile(
    rf"(?<!\w){_DONATION_HEADING}(?!\w){_LABEL_SEPARATOR}"
    rf"(?:מספר|רפסמ|מס[׳’']?|סמ[׳’']?|׳סמ|#)(?![א-ת])"
    rf"{_LABEL_SEPARATOR}{_ADDITIONAL_ID}"
)
# Visual PDF order: the complete identifier sits immediately before the
# donation heading, sometimes glued to the original/copy marker by the reader.
_VISUAL_DONATION_RECEIPT = re.compile(
    rf"(?:(?<![\w/_.-])|(?<=מקור)|(?<=העתק))"
    rf"{_ADDITIONAL_ID}[^\S\n]*(?:[:：#][^\S\n]*)?"
    rf"(?:קבלה[^\S\n]+(?:על|עבור)[^\S\n]+תרומה|"
    rf"תרומה[^\S\n]+(?:על|עבור)[^\S\n]+קבלה|"
    rf"קבלת[^\S\n]+תרומה|תרומה[^\S\n]+קבלת)(?!\w)"
)
_RC_RECEIPT = re.compile(
    rf"(?<!\w)(?:קבלה|הלבק)(?!\w){_LABEL_SEPARATOR}"
    rf"([0-9]+\s*[Rr]\s*[Cc])(?![\w/-]|[_.-]\w)"
)
_RC_RECEIPT_VISUAL = re.compile(
    rf"(?<![\w/_.-])([Rr]\s*[Cc]\s*[0-9]+|[0-9]+\s*[Rr]\s*[Cc])"
    rf"{_LABEL_SEPARATOR}(?:קבלה|הלבק)(?!\w)"
)
_FULL_TAX_LABEL = re.compile(
    r"(?<!\w)(?:(?:מספר\s+)?אישור\s+דיווח\s+(?:ה)?תרומה\s+לרשות\s+המי?סים|"
    r"רפסמ\s+רושיא\s+חוויד\s+המורתה\s+תושרל\s+םיסי?מה|"
    r"(?:מספר\s+)?אישור\s+דיווח(?=\s*[:：]))(?!\w)"
    + _LABEL_SEPARATOR
)
# In visual extraction the copy marker can be on the same line as the ID.
_COPY_DASHED_RECEIPT = re.compile(
    rf"^[^\S\n]*(?:(?:עותק|קתוע|העתק|קתעה)[^\S\n]+)?{_ADDITIONAL_ID}"
    rf"[^\S\n]*{_DASH}[^\S\n]*(?:קבלה[^\S\n]+מספר|מספר[^\S\n]+קבלה|רפסמ[^\S\n]+הלבק|הלבק[^\S\n]+רפסמ)"
    rf"(?:[^\S\n]+(?:עותק|קתוע|העתק|קתעה))?[^\S\n]*$",
    re.MULTILINE,
)
_REPORTED_DONATION = (
    rf"(?:פרטי\s+התרומה\s+דווחו\s+לרשות\s+המי?סים"
    rf"{_LABEL_SEPARATOR}אישור\s+מספר|"
    rf"יטרפ\s+המורתה\s+וחווד\s+תושרל\s+םיסי?מה"
    rf"{_LABEL_SEPARATOR}רושיא\s+רפסמ)"
)
_REPORTED_DONATION_VISUAL = (
    rf"(?:מספר\s+אישור{_LABEL_SEPARATOR}המי?סים\s+לרשות\s+דווחו\s+התרומה\s+פרטי|"
    rf"רפסמ\s+רושיא{_LABEL_SEPARATOR}םיסי?מה\s+תושרל\s+וחווד\s+המורתה\s+יטרפ)"
)
_ALLOCATION_LABEL = r"(?:הקצאה\s+מספר|מספר\s+הקצאה|האצקה\s+רפסמ|רפסמ\s+האצקה)"
_NEW_TAX_LABEL = re.compile(rf"(?<!\w){_REPORTED_DONATION}(?!\w){_LABEL_SEPARATOR}")
_NEW_TAX_VISUAL = re.compile(
    rf"^[^\S\n]*{_ADDITIONAL_ID}{_LABEL_SEPARATOR}{_REPORTED_DONATION_VISUAL}(?!\w)",
    re.MULTILINE,
)
_ALLOCATION_AFTER = re.compile(rf"(?<!\w){_ALLOCATION_LABEL}(?!\w){_LABEL_SEPARATOR}")
_ALLOCATION_BEFORE = re.compile(
    rf"^[^\S\n]*{_ADDITIONAL_ID}[^\S\n]*(?:[:：]|{_DASH})[^\S\n]*"
    rf"{_ALLOCATION_LABEL}(?!\w)", re.MULTILINE,
)
_DONATION_RECEIPT_CONTEXT = re.compile(
    rf"(?<!\w)(?:{_DONATION_HEADING}|תרומה\s+על\s+קבלה|המורת\s+לע\s+הלבק)(?!\w)"
)
_OTHER_DOCUMENT_HEADING = re.compile(
    r"(?m)^[^\S\n]*(?:חשבונית|תינובשח|מס\s+חשבונית|סמ\s+תינובשח|invoice)(?!\w)|\f",
    re.IGNORECASE,
)
# Additional visual tax layouts are read only after receipt selection.
_INSTITUTION_LABEL = re.compile(r"(?:מספר[^\S\n]+מוסד|רפסמ[^\S\n]+דסומ|דסומ[^\S\n]+רפסמ|מוסד[^\S\n]+מספר)")
_INSTITUTION_SERIAL = re.compile(
    r"^[^\S\n]*(?:(?:מספר|רפסמ)[^\S\n]*[:：][^\S\n]*[A-Za-z0-9]{2}/(00[0-9]{4})|"
    r"[A-Za-z0-9]{2}/(00[0-9]{4})[^\S\n]*[:：][^\S\n]*(?:מספר|רפסמ))[^\S\n]*$",
    re.MULTILINE,
)
_RECEIPT_TITLE = re.compile(
    rf"^[^\S\n]*(?:{_DONATION_HEADING}|קבלה|הלבק|(?:donation |tax )?receipt(?: for (?:a )?donation)?)"
    rf"(?:[^\S\n]*(?:{_DASH}[^\S\n]*)?(?:מקור|רוקמ|עותק|קתוע|העתק|קתעה|original|copy))?[^\S\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


# Complete lines from the observed readers. Horizontal whitespace keeps
# these fields from taking an identifier from the next line.
_OBSERVED_INSTITUTION = re.compile(
    r"^[^\S\n]*(?:קבלת[^\S\n]+תרומה|תלבק[^\S\n]+המורת)"
    r"([0-9]{2}/(00[0-9]{4}))[^\S\n]*[:：][^\S\n]*"
    r"(?:מספר(?:מקור)?|רפסמ|רוקמרפסמ)[^\S\n]*$", re.MULTILINE,
)
_OBSERVED_INSTITUTION_PAYMENT = re.compile(
    r"^[^\S\n]*([0-9]{2}/(00[0-9]{4}))[^\S\n]*[:：][^\S\n]*"
    r"(?:פרטי[^\S\n]+תשלום[^\S\n]*[:：][^\S\n]*קבלת[^\S\n]+תרומה[^\S\n]+מספר|"
    r"יטרפ[^\S\n]+םולשת[^\S\n]*[:：][^\S\n]*תלבק[^\S\n]+המורת[^\S\n]+רפסמ)[^\S\n]*$",
    re.MULTILINE,
)
_OBSERVED_COPY = re.compile(
    rf"^[^\S\n]*(?:עותק|קתוע)[^\S\n]*([0-9]+)[^\S\n]*{_DASH}[^\S\n]*"
    r"(?:קבלה[^\S\n]+מספר(?:קבלה[^\S\n]+על[^\S\n]+תרומה)?|"
    r"הלבק[^\S\n]+(?:רפסמ|הלבקרפסמ[^\S\n]+לע[^\S\n]+המורת))[^\S\n]*$",
    re.MULTILINE,
)
_OBSERVED_VISUAL = re.compile(
    r"^[^\S\n]*([0-9]+)[^\S\n]+(?:קבלה[^\S\n]+על[^\S\n]+תרומה[^\S\n]+מספר|"
    r"הלבק[^\S\n]+לע[^\S\n]+המורת[^\S\n]+רפסמ)[^\S\n]*$", re.MULTILINE,
)
_OBSERVED_REPORTED = re.compile(
    rf"^[^\S\n]*([0-9]+)[^\S\n]+{_REPORTED_DONATION}[^\S\n]*$", re.MULTILINE,
)


def _institution_headers(text):
    for section in _OTHER_DOCUMENT_HEADING.split(text):
        if _INSTITUTION_LABEL.search(section) and _OBSERVED_INSTITUTION.search(section):
            yield from _OBSERVED_INSTITUTION.finditer(section)
            yield from _OBSERVED_INSTITUTION_PAYMENT.finditer(section)


def _observed_receipt_ids(text):
    return {match.group(2) for match in _institution_headers(text)} | {
        match.group(1) for pattern in (_OBSERVED_COPY, _OBSERVED_VISUAL)
        for match in pattern.finditer(text)}


def has_receipt_heading(text):
    cleaned = _IDENTIFIER_MARKS.sub("", text)
    return bool(_RECEIPT_TITLE.search(cleaned) or _observed_receipt_ids(cleaned))


def institution_receipt_numbers(text):
    """The slash is meaningful only for a standalone serial in this receipt template."""
    sections = _OTHER_DOCUMENT_HEADING.split(text)
    return {match.group(2) for match in _institution_headers(text)} | {match.group(1) or match.group(2) for section in sections
            if (_DONATION_RECEIPT_CONTEXT.search(section) or has_receipt_heading(section))
            and _INSTITUTION_LABEL.search(section)
            for match in _INSTITUTION_SERIAL.finditer(section)}


_REVERSED_TAX_LABEL = (
    r"(?:המי?סים\s+לרשות\s+התרומה\s+דיווח\s+אישור\s+מספר|"
    r"םיסי?מה\s+תושרל\s+המורתה\s+חוויד\s+רושיא\s+רפסמ)"
)
_TAX_LABEL_ANY_ORDER = (
    rf"(?:{_REVERSED_TAX_LABEL}|מספר\s+אישור\s+דיווח\s+התרומה\s+לרשות\s+המי?סים|"
    r"רפסמ\s+רושיא\s+חוויד\s+המורתה\s+תושרל\s+םיסי?מה)"
)
_TAX_INLINE = r"[^\S\n]*"
_TAX_WRAP = rf"{_TAX_INLINE}(?:\n{_TAX_INLINE})?"
_VISUAL_TAX_VALUE_AFTER = re.compile(
    rf"(?<!\w){_REVERSED_TAX_LABEL}(?!\w)"
    rf"{_TAX_INLINE}(?:[:：]{_TAX_WRAP}|\n{_TAX_INLINE}(?:[:：]{_TAX_WRAP})?|{_TAX_INLINE})"
    rf"{_ADDITIONAL_ID}"
)
# A preceding identifier must start its line. A wrap requires a colon.
_VISUAL_TAX_VALUE_BEFORE = re.compile(
    rf"^{_TAX_INLINE}{_ADDITIONAL_ID}(?:{_TAX_WRAP}[:：]{_TAX_WRAP}|[^\S\n]+)"
    rf"{_TAX_LABEL_ANY_ORDER}(?!\w)",
    re.MULTILINE,
)
_APPROVAL_LABEL = (
    r"(?:אישור\s+דיווח\s+התרומה\s+לרשות\s+המי?סים|"
    r"רושיא\s+חוויד\s+המורתה\s+תושרל\s+םיסי?מה|"
    r"המי?סים\s+לרשות\s+התרומה\s+דיווח\s+אישור|"
    r"םיסי?מה\s+תושרל\s+המורתה\s+חוויד\s+רושיא)"
)
_VISUAL_FULL_TAX_FIELD = re.compile(
    rf"^[^\S\n]*{_ADDITIONAL_ID}[^\S\n]*[:：][^\S\n]*"
    rf"(?:{_APPROVAL_LABEL}|{_TAX_LABEL_ANY_ORDER})[^\S\n]*$",
    re.MULTILINE,
)
_APPROVAL_VALUE = re.compile(
    rf"(?<!\w){_APPROVAL_LABEL}(?!\w){_TAX_WRAP}(?:[:：]{_TAX_WRAP})?{_ADDITIONAL_ID}"
)
_IDENTIFIER_DATE = re.compile(
    r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})\Z"
)
_IDENTIFIER_YEAR = re.compile(r"(?:19|20)\d{2}\Z")
_NON_IDENTIFIER_SUFFIX = re.compile(
    r'[^\S\n]*(?:₪|(?:שקל(?:ים)?|לקש|םילקש|ש["״]ח|ח["״]ש|עסקה|אסמכתא)(?!\w))'
)
_OTHER_NUMBER_LINE = re.compile(
    r"[^\S\n]*(?:[:#]\s*)?(?:קבלה|הלבק|תרומה|שנת|תאריך|סכום|אסמכתא|עסקה|עמותה)(?!\w)"
)

# Donor ID: explicit anchor required — bare 9-digit numbers are also registration numbers.
_IDENTIFIER_MARKS = re.compile(r'[\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]')
_DONOR_ID = re.compile(
    r'(?<!\w)(?:ת[^\S\n]*[."״]?[^\S\n]*ז|ז[^\S\n]*[."״]?[^\S\n]*ת|'
    r'מספר\s+זהות|רפסמ\s+תוהז|מס\'?\s*זהות|מ\.ז|זהות|תוהז)'
    r'[^\S\n]*\.?\s*:?\s*([0-9]{9})(?!\w)',
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
    full_source_serial: str = ""
    payment_total_verified: bool = False
    tax_confirmation_candidate: str = ""
    receipt_scope_checked: bool = False  # Compatibility only; never infer scope proof.
    receipt_series: str = ""
    receipt_terminal: str = ""
    tax_layout_verified: bool = False


# Expanded formats are accepted only with a receipt/donation date label;
# unlabelled short dates and dashed numbers can be receipt identifiers.
_FLEX_DATE = r"(?<![\w./-])([0-9]{1,2})(?P<sep>[./-])([0-9]{1,2})(?P=sep)([0-9]{4}|[0-9]{2})(?![\w./-])"
_DATE_LABEL = r"(?:תאריך[ \t]+(?:ה?קבלה|ה?תרומה)|ךיראת[ \t]+(?:הלבק(?:ה)?|המורת(?:ה)?))"
_DATE_GENERIC = r"(?:תאריך|ךיראת)"
_DATE_MARKS = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DATE_LABELLED = tuple(re.compile(pattern, re.MULTILINE) for pattern in (
    rf"(?<!\w){_DATE_LABEL}[ \t]*[:：]?[ \t]*{_FLEX_DATE}",
    rf"{_FLEX_DATE}[ \t]*[:：]?[ \t]*{_DATE_LABEL}(?!\w)",
))
_DATE_GENERIC_LABELLED = tuple(re.compile(pattern, re.MULTILINE) for pattern in (
    rf"(?<!\w){_DATE_GENERIC}[ \t]*[:：][ \t]*{_FLEX_DATE}",
    rf"{_FLEX_DATE}[ \t]*[:：][ \t]*{_DATE_GENERIC}(?!\w)(?![ \t]+[א-ת])",
))


def _calendar_date(day, month, year):
    # Modern two-digit receipt years use 2000–2099, never Python's 1969 pivot.
    year = '20' + year if len(year) == 2 else year
    try:
        value = date(int(year), int(month), int(day))
    except ValueError:
        return None
    return value.strftime('%d/%m/%Y') if value.year >= 1900 else None


def _labelled_dates(text, patterns):
    return [_calendar_date(m.group(1), m.group(3), m.group(4))
            for pattern in patterns for m in pattern.finditer(text)]


def _date_candidates(text):
    """Conservative MANUAL gate: compare dates after canonical normalization."""
    text = _DATE_MARKS.sub('', text)
    values = {_calendar_date(*m.groups()) for m in _DATE_PATTERN.finditer(text)}
    values.update(_labelled_dates(text, _DATE_LABELLED))
    values.update(_labelled_dates(text, _DATE_GENERIC_LABELLED))
    return values


def _find_date_in_text(text: str) -> str | None:
    text = _DATE_MARKS.sub('', text)
    for patterns in (_DATE_LABELLED, _DATE_GENERIC_LABELLED):
        values = set(_labelled_dates(text, patterns))
        if values:
            return next(iter(values)) if len(values) == 1 and None not in values else None
    # Preserve the legacy unlabelled format; validate its calendar date.
    for match in _DATE_PATTERN.finditer(text):
        value = _calendar_date(*match.groups())
        if value:
            return value
    return None


def _find_date_in_filename(file_path: Path) -> tuple[str, str] | None:
    m = _FILENAME_DATE.match(file_path.name)
    if m:
        day, month, year_short = m.group(1), m.group(2), m.group(3)
        return f"{day}/{month}/20{year_short}", "תאריך מתוך שם הקובץ"
    return None


_AMOUNT_DATE_TOKEN = re.compile(
    r"(?<![\d./-])(?:\d{1,2}([./-])\d{1,2}\1(?:\d{4}|\d{2})"
    r"|\d{4}([./-])\d{1,2}\2\d{1,2})(?![\d./-])"
)


# Only a complete monetary grand-total line after the item-table headings.
# Never read the quantity column or the compact concatenated item row.
_TABLE_MONEY = r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?'
_TABLE_CURRENCY = r'(?:ש["״]ח|₪|שקל(?: חדש)?)'
_TABLE_TOTAL = r'ס[ה]?["״]כ'
_TABLE_GRAND_TOTAL = re.compile(
    rf'^[ \t]*{_TABLE_TOTAL}[ \t]*:?[ \t]*(?:\n[ \t]*)?'
    rf'{_TABLE_CURRENCY}[ \t]*:?[ \t]*(?P<after>{_TABLE_MONEY})[ \t]*$|'
    rf'^[ \t]*{_TABLE_TOTAL}[ \t]*:?[ \t]*(?:\n[ \t]*)?'
    rf'(?P<before>{_TABLE_MONEY})[ \t]*{_TABLE_CURRENCY}[ \t]*$', re.MULTILINE)
TABLE_AMOUNT_CONFLICT = 'סכומי סה״כ כספיים סותרים — דרוש אימות'


def table_payment_totals(text: str) -> set[Decimal]:
    text = _INVISIBLE_MARKS.sub('', text).replace('\r\n', '\n').replace('\r', '\n')
    # Require the actual item-table vocabulary in a nearby bounded header.
    # Compact headings and split-line PDF readers both satisfy the rule.
    values = set()
    for match in _TABLE_GRAND_TOTAL.finditer(text):
        preceding = text[:match.start()].splitlines()[-18:]
        header = ' '.join(preceding)
        if not all(word in header for word in ('קוד מוצר', 'תיאור מוצר', 'כמות', 'מחיר')):
            continue
        value = Decimal((match['after'] or match['before']).replace(',', ''))
        if value > 0:
            values.add(value)
    return values


def _find_amount(text: str) -> str | None:
    totals = table_payment_totals(text)
    if totals:
        return format(next(iter(totals)), 'f') if len(totals) == 1 else None
    text = _AMOUNT_DATE_TOKEN.sub("|", text)
    for pattern in _AMOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).replace(",", "")
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


def _find_receipt_number_from_filename(file_path: Path, excluded: set[str]) -> str | None:
    """A receipt-labelled filename can supplement a corroborated donation."""
    candidates = set(_additional_receipt_numbers(file_path.stem.replace('_', ' '), excluded, filename=True))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _valid_receipt_number(value: str, excluded: set[str]) -> bool:
    compact = re.sub(r'[-/_.]', '', value)
    return any(ch.isdigit() for ch in value) and compact not in excluded


def receipt_numbers_in_text(text: str, excluded: set[str] | None = None) -> set[str]:
    """All explicitly labelled receipt IDs, using the existing extraction rules.

    Unlike the first-match extractor, this evidence check must see every labelled
    ID before treating one PDF as a single donation. It never uses the filename.
    """
    text = _without_full_tax_fields(text)
    additional_exclusions = set(excluded or ())
    registration = _find_registration_number_by_explicit_anchor(text)
    if registration:
        additional_exclusions.add(registration)
    numbers = set(_additional_receipt_numbers(text, additional_exclusions))
    observed = _observed_receipt_ids(text)
    numbers.update((observed | institution_receipt_numbers(text)) - additional_exclusions)
    numbers.update(
        match.group(1)
        for pattern in (_VISUAL_DONATION_RECEIPT, _VISUAL_ORIGINAL_DONATION_RECEIPT, _COPY_DASHED_RECEIPT)
        for match in pattern.finditer(text)
        if _additional_identifier_is_valid(match.group(1), text, match.end(1), additional_exclusions)
    )
    for pat in (_RECEIPT_NUM_LABEL, _RECEIPT_NUM_LABEL_REVERSED, _RECEIPT_NUM_ON_DONATION,
                _RECEIPT_NUM_ON_DONATION_REVERSED, _RECEIPT_NUM_DONATION,
                _RECEIPT_NUM_REVERSED, _RECEIPT_NUM_ADJACENT):
        for match in pat.finditer(text):
            value = match.group(1)
            if _DATE_PATTERN.fullmatch(value) or (
                pat not in (_RECEIPT_NUM_LABEL, _RECEIPT_NUM_LABEL_REVERSED)
                and (_IDENTIFIER_DATE.fullmatch(value) or _IDENTIFIER_YEAR.fullmatch(value))
            ):
                continue
            if _valid_receipt_number(value, excluded or set()):
                numbers.add(value)
    serials = {match.group(1) for match in _institution_headers(text)}
    return numbers - serials


def receipt_amounts_in_text(text: str) -> set[str]:
    """Prefer proven grand totals over quantity-like legacy matches."""
    totals = table_payment_totals(text)
    if totals:
        return {format(value, 'f') for value in totals}
    text = _AMOUNT_DATE_TOKEN.sub("|", text)
    return {match.group(1) for pattern in _AMOUNT_PATTERNS
            for match in pattern.finditer(text)}


_INSTITUTION_DECIMAL = re.compile(r"(?<![\w.,/-])(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\.[0-9]{2}(?![\w.,/-])")


_PAYMENT_HEADING = re.compile(r"פרטי[^\S\n]+תשלום[^\S\n]*[:：][^\S\n]*קבלת[^\S\n]+תרומה|"
                              r"יטרפ[^\S\n]+םולשת[^\S\n]*[:：][^\S\n]*תלבק[^\S\n]+המורת")
_PAYMENT_CURRENCY = r'(?:שקל[^\S\n]+חדש|לקש[^\S\n]+שדח|₪)'
_PAYMENT_TOTAL = r'(?:סה["״]כ|הס["״]כ|כ["״]הס)'
# Rivhit monetary totals include cents. A bare integer can be a quantity
# or the adjacent tax confirmation.
_PAYMENT_VALUE = r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\.[0-9]{2}'
_PAYMENT_GAP = r'[^\S\n]*(?:\n[^\S\n]*)?'
_PAYMENT_BOUND_TOTAL = re.compile(
    # PyMuPDF emits the Rivhit grand total as a complete ":labelVALUE" line.
    rf'^[^\S\n]*[:：]{_PAYMENT_TOTAL}(?P<compact>{_PAYMENT_VALUE})[^\S\n]*$|'
    rf'(?<![\w"״]){_PAYMENT_TOTAL}(?![\w"״])[^\S\n]*[:：]?{_PAYMENT_GAP}'
    rf'(?:{_PAYMENT_CURRENCY}{_PAYMENT_GAP})?(?P<after>{_PAYMENT_VALUE})'
    rf'(?=[^\S\n]*$|[^\S\n]{{2,}}|[^\S\n]*{_PAYMENT_CURRENCY})'
    rf'(?!{_PAYMENT_GAP}[0-9]+(?:[,.][0-9]+)*[^\S\n]*$)'
    rf'|(?<![\w.,/-])(?P<before>{_PAYMENT_VALUE}){_PAYMENT_GAP}'
    rf'(?:{_PAYMENT_CURRENCY}{_PAYMENT_GAP})?[:：]?[^\S\n]*{_PAYMENT_TOTAL}(?![\w"״])',
    re.MULTILINE,
)
_PAYMENT_REPORTING_FIELD = re.compile(rf'(?:(?<![\w.,/-])[0-9]+[^\S\n]+)?{_REPORTED_DONATION}')


def _payment_totals(texts, serial):
    totals = set()
    for text in texts:
        for section in _OTHER_DOCUMENT_HEADING.split(text):
            for heading in _PAYMENT_HEADING.finditer(section):
                # Keep the serial on the heading line, but read totals only
                # after the heading and before another payment or tax field.
                start = section.rfind("\n", 0, heading.start()) + 1
                block = "\n".join(section[start:].splitlines()[:12])
                offset = heading.end() - start
                stop = _PAYMENT_HEADING.search(block, offset)
                if stop:
                    block = block[:stop.start()]
                reporting = _PAYMENT_REPORTING_FIELD.search(block, offset)
                if reporting:
                    block = block[:reporting.start()]
                if not (re.search(rf'(?<![\w/]){re.escape(serial)}(?![\w/])', block)
                        and re.search(_PAYMENT_CURRENCY, block)):
                    continue
                for match in _PAYMENT_BOUND_TOTAL.finditer(block[offset:]):
                    value = Decimal((match['compact'] or match['after'] or match['before']).replace(',', ''))
                    if value > 0:
                        totals.add(value)
    return totals


def institution_amount_candidates(*texts):
    amounts = set()
    for text in texts:
        values = receipt_amounts_in_text(text) | set(_INSTITUTION_DECIMAL.findall(text))
        for value in values:
            try:
                amounts.add(Decimal(value.replace(',', '')))
            except InvalidOperation:
                continue
    return amounts


def _find_receipt_number_in_text(text: str, excluded: set[str] | None = None) -> str | None:
    text = _without_full_tax_fields(text)
    for pat in (_RECEIPT_NUM_LABEL, _RECEIPT_NUM_LABEL_REVERSED, _RECEIPT_NUM_ON_DONATION,
                _RECEIPT_NUM_ON_DONATION_REVERSED, _RECEIPT_NUM_DONATION,
                _RECEIPT_NUM_REVERSED, _RECEIPT_NUM_ADJACENT):
        for m in pat.finditer(text):
            candidate = m.group(1)
            # A donation heading may be followed by the date rather than an ID.
            if (pat in (_RECEIPT_NUM_ON_DONATION, _RECEIPT_NUM_ON_DONATION_REVERSED)
                    and _IDENTIFIER_DATE.fullmatch(candidate)):
                continue
            if (pat not in (_RECEIPT_NUM_LABEL, _RECEIPT_NUM_LABEL_REVERSED)
                    and (len(candidate) == 1 or _IDENTIFIER_YEAR.fullmatch(candidate)
                         or _NON_IDENTIFIER_SUFFIX.match(text, m.end(1)))):
                continue
            if _valid_receipt_number(candidate, excluded or set()):
                return candidate
    return None


def _additional_identifier_is_valid(
    value: str, text: str, end: int, excluded: set[str], *, allow_year: bool = False,
) -> bool:
    return (
        _valid_receipt_number(value, excluded)
        and not _IDENTIFIER_DATE.fullmatch(value)
        and (allow_year or not _IDENTIFIER_YEAR.fullmatch(value))
        and not re.fullmatch(r"\d+\.\d{1,2}", value)
        and not _NON_IDENTIFIER_SUFFIX.match(text, end)
    )


def _additional_receipt_numbers(text: str, excluded: set[str], *, filename=False):
    patterns = (_ADDITIONAL_RECEIPT, _RC_RECEIPT, _RC_RECEIPT_VISUAL)
    if not filename:
        patterns += (_DASHED_RECEIPT_LABEL, _VISUAL_MARKED_RECEIPT)
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group(1)
            if pattern in (_RC_RECEIPT, _RC_RECEIPT_VISUAL):
                value = re.sub(r"\s+", "", value)
                if value[:2].lower() == "rc":
                    value = value[2:] + value[:2]
            if pattern is _ADDITIONAL_RECEIPT:
                heading = text[match.start():match.start(1)]
                if len(value) == 1 or (re.fullmatch(r"קבלה\s+תרומה\s+", heading)
                                       and value[0].isdigit() and not value.isdigit()):
                    continue
            if not _additional_identifier_is_valid(value, text, match.end(1), excluded):
                continue
            if (pattern is _ADDITIONAL_RECEIPT and "\n" in text[match.start():match.start(1)]
                    and _OTHER_NUMBER_LINE.match(text, match.end(1))):
                continue
            yield value


_OBSERVED_TAX_INLINE = re.compile(
    rf"^{_TAX_INLINE}{_TAX_LABEL_ANY_ORDER}{_TAX_INLINE}[:：]{_TAX_INLINE}{_ADDITIONAL_ID}{_TAX_INLINE}$",
    re.MULTILINE,
)
_OBSERVED_TAX_ALLOCATION = re.compile(
    rf"^{_TAX_INLINE}{_TAX_LABEL_ANY_ORDER}{_TAX_INLINE}[:：]?{_TAX_INLINE}\n"
    rf"{_TAX_INLINE}{_ADDITIONAL_ID}{_TAX_INLINE}[:：]{_TAX_INLINE}{_ALLOCATION_LABEL}{_TAX_INLINE}$",
    re.MULTILINE,
)


_OBSERVED_TAX_REPORTED_AFTER = re.compile(
    rf"^{_TAX_INLINE}(?:{_REPORTED_DONATION}(?:{_TAX_INLINE}[:：]{_TAX_INLINE}|[^\S\n]+)|"
    rf"(?:אישור{_TAX_INLINE}דיווח|רושיא{_TAX_INLINE}חוויד){_TAX_INLINE}[:：]{_TAX_INLINE})"
    rf"{_ADDITIONAL_ID}{_TAX_INLINE}$", re.MULTILINE,
)


_EXPLICIT_TAX_BEFORE = re.compile(
    rf"^{_TAX_INLINE}{_ADDITIONAL_ID}{_TAX_INLINE}[:：]{_TAX_INLINE}{_TAX_LABEL_ANY_ORDER}{_TAX_INLINE}$",
    re.MULTILINE,
)


def _observed_tax_fields(text):
    patterns = (_EXPLICIT_TAX_BEFORE, _OBSERVED_TAX_INLINE, _VISUAL_FULL_TAX_FIELD, _OBSERVED_TAX_ALLOCATION,
                _OBSERVED_TAX_REPORTED_AFTER)
    if any(_institution_headers(text)):
        patterns += (_OBSERVED_REPORTED,)
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group(1)
            if pattern is _VISUAL_FULL_TAX_FIELD and _EXPLICIT_TAX_BEFORE.fullmatch(match.group()):
                continue
            if _additional_identifier_is_valid(
                value, text, match.end(1), set(),
                allow_year=pattern in (_EXPLICIT_TAX_BEFORE, _OBSERVED_TAX_INLINE),
            ):
                yield match.start(), match.end(), value


def _full_tax_fields(text: str):
    """Yield full-label matches and their immediately associated valid identifier."""
    if _observed_receipt_ids(text):
        yield from _observed_tax_fields(text)
        return

    def in_donation_section(position):
        preceding = text[max(0, position - 800):position]
        return any(not _OTHER_DOCUMENT_HEADING.search(preceding, heading.end())
                   for heading in _DONATION_RECEIPT_CONTEXT.finditer(preceding))

    visual_fields = [
        (match.start(), match.end(), match.group(1))
        for pattern in (_VISUAL_FULL_TAX_FIELD, _NEW_TAX_VISUAL, _ALLOCATION_BEFORE)
        for match in pattern.finditer(text)
        if (pattern is not _ALLOCATION_BEFORE or in_donation_section(match.start()))
        and _additional_identifier_is_valid(match.group(1), text, match.end(1), set())
    ]
    # An exact, full, explicitly labelled year-shaped identifier is not a date.
    for pattern in (_EXPLICIT_TAX_BEFORE, _OBSERVED_TAX_INLINE):
        for match in pattern.finditer(text):
            value = match.group(1)
            if (_IDENTIFIER_YEAR.fullmatch(value)
                    and _additional_identifier_is_valid(value, text, match.end(1), set(), allow_year=True)):
                visual_fields.append((match.start(), match.end(), value))
    yield from visual_fields
    labels = sorted((match for pattern in (_FULL_TAX_LABEL, _NEW_TAX_LABEL, _ALLOCATION_AFTER)
                     for match in pattern.finditer(text)), key=lambda match: match.start())
    for label in labels:
        if label.re is _ALLOCATION_AFTER and not in_donation_section(label.start()):
            continue
        if any(start <= label.start() < end for start, end, _ in visual_fields):
            continue
        match = re.match(_ADDITIONAL_ID, text[label.end():])
        value = match.group(1) if match else ""
        end = label.end() + len(value)
        wrapped = "\n" in label.group()
        line_start = text.rfind("\n", label.start(), label.end()) + 1 if wrapped else label.end()
        visual_receipt = wrapped and label.re is not _FULL_TAX_LABEL and (
            _VISUAL_MARKED_RECEIPT.match(text, line_start)
        )
        if (not _additional_identifier_is_valid(value, text, end, set())
                or (wrapped and _OTHER_NUMBER_LINE.match(text, end)) or visual_receipt):
            value = ""
            end = line_start if visual_receipt else label.end()
        yield label.start(), end, value


def _short_tax_values(*texts: str) -> set[str]:
    # A short legacy tax label does not independently confirm a receipt ID.
    return {match.group(1) for text in texts
            for match in re.finditer(
                r"^\s*(?:מספר[ 	]+)?אישור[ 	]+דיווח[ 	]*[:：][ 	]*(\d{3,8})[ 	]*$",
                _IDENTIFIER_MARKS.sub("", text), re.MULTILINE,
            )}


def _find_full_tax_number(text: str) -> str | None:
    text = _IDENTIFIER_MARKS.sub("", text)
    return next((value for _, _, value in _full_tax_fields(text) if value), None)


def _without_full_tax_fields(text: str, *, strip_marks: bool = True) -> str:
    cleaned = _IDENTIFIER_MARKS.sub("", text)
    fields = list(_full_tax_fields(cleaned))
    if not fields and not strip_marks:
        return text
    text = cleaned
    masked = list(text)
    for start, end, _ in fields:
        # Preserve line breaks and offsets even if evidence patterns overlap.
        masked[start:end] = (char if char == "\n" else "|" for char in text[start:end])
    return "".join(masked)


def _visual_tax_numbers(text: str, receipt_numbers: set[str]):
    text = _IDENTIFIER_MARKS.sub("", text)
    for pattern in (_VISUAL_TAX_VALUE_AFTER, _VISUAL_TAX_VALUE_BEFORE):
        for match in pattern.finditer(text):
            value = match.group(1)
            end = match.end(1)
            if value in receipt_numbers:
                continue
            if not _additional_identifier_is_valid(value, text, end, set()):
                continue
            if (
                pattern is _VISUAL_TAX_VALUE_AFTER
                and "\n" in text[match.start():match.start(1)]
                and _OTHER_NUMBER_LINE.match(text, end)
            ):
                continue
            yield value


def _find_tax_report_number(text: str) -> str | None:
    for pat in (_TAX_NUM_BEFORE, _TAX_ANCHOR_COLON,
                _TAX_ANCHOR_NUM_AFTER, _TAX_NUM_BEFORE_LABEL):
        for match in pat.finditer(text):
            if not _IDENTIFIER_YEAR.fullmatch(match.group(1)):
                return match.group(1)
    return None


def _find_organization_name(text: str) -> str | None:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines[:15]:
        if _HEBREW_ONLY.match(line) and len(line) >= 4:
            return line
    return None


def _find_donor_id(text: str) -> str | None:
    m = _DONOR_ID.search(_IDENTIFIER_MARKS.sub('', text))
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


INSTITUTION_AMOUNT_REVIEW = "סכומים שונים במסמך — דרוש אימות הסכום הכולל"


IDENTIFIER_REVIEW_NOTES = {
    "receipt_number": "מספרי קבלה סותרים — דרוש אימות",
    "tax_report_number": "מספרי אישור דיווח סותרים — דרוש אימות",
}


def resolve_identifier_evidence(meta: ReceiptMetadata, *sources: str, tax_layout=()) -> None:
    """Use adjacent labelled values and flag conflicts within the same field."""
    texts = tuple(dict.fromkeys(
        _IDENTIFIER_MARKS.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        for text in sources if text
    ))
    meta.tax_layout_verified = False
    if not texts:
        return
    observed = set().union(*(_observed_receipt_ids(text) for text in texts))
    labelled = observed | {match.group(1) for text in texts
                          for match in _MARKED_DONATION_RECEIPT.finditer(_without_full_tax_fields(text))
                          if _additional_identifier_is_valid(match.group(1), text, match.end(1), set())}
    if meta.donor_id in labelled:
        meta.donor_id = next((match.group(1) for text in texts for match in _DONOR_ID.finditer(text)
                              if match.group(1) not in labelled and match.group(1) != meta.registration_number), '')
    excluded = {meta.registration_number, meta.donor_id}
    institution_serials = {match.group(1) for text in texts for match in _institution_headers(text)}
    if institution_serials:
        meta.full_source_serial = next(iter(institution_serials)) if len(institution_serials) == 1 else ''
    receipt_exclusions = excluded | _short_tax_values(*texts)
    explicit_receipts = observed - receipt_exclusions
    receipt_headings = set()
    for text in texts:
        receipt_text = _without_full_tax_fields(text)
        for pattern in (_RECEIPT_NUM_LABEL, _RECEIPT_NUM_LABEL_REVERSED, _MARKED_DONATION_RECEIPT,
                        _VISUAL_DONATION_RECEIPT, _VISUAL_ORIGINAL_DONATION_RECEIPT,
                        _DASHED_RECEIPT_LABEL, _VISUAL_MARKED_RECEIPT, _COPY_DASHED_RECEIPT):
            explicit_receipts.update(
                match.group(1) for match in pattern.finditer(receipt_text)
                if _additional_identifier_is_valid(match.group(1), receipt_text, match.end(1), receipt_exclusions)
            )
        receipt_headings.update(_additional_receipt_numbers(receipt_text, receipt_exclusions))
        explicit_receipts.update(institution_receipt_numbers(receipt_text) - receipt_exclusions)
    receipt_candidates = (explicit_receipts or receipt_headings) - institution_serials

    tax_candidates = set()
    receipt_values_after_tax_heading = set()
    for text in texts:
        if observed:
            tax_candidates.update(value for _, _, value in _observed_tax_fields(text) if value)
            continue
        tax_candidates.update(value for _, _, value in _full_tax_fields(text) if value)
        tax_candidates.update(
            _visual_tax_numbers(text, receipt_candidates | {meta.receipt_number} | excluded)
        )
        for match in _APPROVAL_VALUE.finditer(text):
            value = match.group(1)
            prefix = text[match.start():match.start(1)]
            if "\n" in prefix and _OTHER_NUMBER_LINE.match(text, match.end(1)):
                if value in explicit_receipts:
                    receipt_values_after_tax_heading.add(value)
                continue
            if _additional_identifier_is_valid(
                value, text, match.end(1), receipt_candidates | {meta.receipt_number} | excluded,
            ):
                tax_candidates.add(value)

    # Physical spans may split a leading ordinary digit from the bold value.
    # The exact full label, a single bold run, and corroborating text are required.
    # Do not remove any unrelated candidate or resolve two different physical fields.
    layout_pairs = set(tax_layout)
    if len(layout_pairs) == 1:
        joined, bold = next(iter(layout_pairs))
        all_tax = set(tax_candidates)
        for text in texts:
            all_tax.update(value for _, _, value in _full_tax_fields(text) if value)
            all_tax.update(value for _, _, value in _observed_tax_fields(text) if value)
            all_tax.update(_visual_tax_numbers(text, receipt_candidates | excluded))
        # A mixed-font identifier must be independently corroborated as the
        # bold value in a text reader, not inferred solely from typography.
        if (bold in all_tax and joined in all_tax and all_tax <= {joined, bold}
                and bold not in excluded and len(bold) >= 3
                and (joined != bold or _IDENTIFIER_YEAR.fullmatch(bold))):
            tax_candidates = {bold}
            meta.tax_layout_verified = True
            meta.tax_confirmation_candidate = ''
    if (meta.tax_report_number in receipt_values_after_tax_heading
            and meta.tax_report_number not in tax_candidates):
        meta.tax_report_number = ""
    if not meta.receipt_number and meta.tax_report_number not in tax_candidates:
        # A66 may already have excluded a legacy tax value from receipt search.
        receipt_candidates.discard(meta.tax_report_number)
    notes = [note for note in meta.notes.split("; ") if note]
    if meta.tax_layout_verified:
        note = IDENTIFIER_REVIEW_NOTES['tax_report_number']
        notes = [n for n in notes if n != note]
    if observed:
        # The full fields supersede unbound page counters and legacy guesses.
        if IDENTIFIER_REVIEW_NOTES['tax_report_number'] in notes:
            notes.remove(IDENTIFIER_REVIEW_NOTES['tax_report_number'])
        meta.tax_report_number = ""
        meta.tax_confirmation_candidate = ""
        if len(tax_candidates) == 1:
            candidate = next(iter(tax_candidates))
            if not institution_serials and candidate.isascii() and candidate.isdigit() and len(candidate) < 3:
                meta.tax_confirmation_candidate = candidate
                tax_candidates.clear()
    if institution_serials:
        if len(institution_serials) > 1:
            notes.append(IDENTIFIER_REVIEW_NOTES['receipt_number'])
        totals = _payment_totals(texts, meta.full_source_serial) if meta.full_source_serial else set()
        meta.payment_total_verified = False
        if len(totals) == 1:
            meta.amount = format(next(iter(totals)), 'f')
            meta.payment_total_verified = True
            notes = [note for note in notes if note != INSTITUTION_AMOUNT_REVIEW]
        elif len(totals) > 1 or len(institution_amount_candidates(*texts)) > 1 or INSTITUTION_AMOUNT_REVIEW in notes:
            meta.amount = ""
            if INSTITUTION_AMOUNT_REVIEW not in notes:
                notes.append(INSTITUTION_AMOUNT_REVIEW)
    if not institution_serials:
        table_totals = set().union(*(table_payment_totals(text) for text in texts))
        if len(table_totals) == 1 and TABLE_AMOUNT_CONFLICT not in notes:
            meta.amount = format(next(iter(table_totals)), 'f')
            meta.payment_total_verified = True
        elif len(table_totals) > 1 or TABLE_AMOUNT_CONFLICT in notes:
            meta.amount = ''
            meta.payment_total_verified = False
            meta.extraction_status = STATUS_NEEDS_REVIEW
            if TABLE_AMOUNT_CONFLICT not in notes:
                notes.append(TABLE_AMOUNT_CONFLICT)
    for field, candidates in (
        ("receipt_number", receipt_candidates), ("tax_report_number", tax_candidates),
    ):
        note = IDENTIFIER_REVIEW_NOTES[field]
        if len(candidates) > 1 or note in notes:
            setattr(meta, field, "")
            if note not in notes:
                notes.append(note)
            meta.extraction_status = STATUS_NEEDS_REVIEW
        elif candidates and (field == "tax_report_number" or explicit_receipts or not meta.receipt_number):
            setattr(meta, field, next(iter(candidates)))
    meta.notes = "; ".join(notes)


def extract_metadata(normalized_text: str, file_path: Path, raw_text: str = "") -> ReceiptMetadata:
    meta = ReceiptMetadata(file_name=file_path.name)
    notes: list[str] = []

    meta.organization_name = _find_organization_name(normalized_text) or ""
    meta.registration_number = _first_match(_find_registration_number, normalized_text, raw_text) or ""
    full_tax_number = _first_match(_find_full_tax_number, normalized_text, raw_text)
    legacy_tax_number = None
    if not full_tax_number:
        legacy_tax_number = _first_match(
            lambda text: _find_tax_report_number(_without_full_tax_fields(text, strip_marks=False)),
            normalized_text, raw_text,
        )
    meta.tax_report_number = full_tax_number or legacy_tax_number or ""
    meta.donor_id = _first_match(_find_donor_id, normalized_text, raw_text) or ""
    excluded = {value for value in (meta.registration_number, meta.donor_id,
                                    legacy_tax_number) if value}
    excluded.update(_short_tax_values(normalized_text, raw_text))
    meta.receipt_number = (
        _first_match(lambda text: _find_receipt_number_in_text(
                         _without_full_tax_fields(text, strip_marks=False), excluded),
                     normalized_text, raw_text)
        or ""
    )
    if not meta.receipt_number:
        # The full tax field is masked by location. An explicit receipt label
        # elsewhere may legitimately carry the same value.
        meta.receipt_number = _first_match(
            lambda text: next(
                _additional_receipt_numbers(_without_full_tax_fields(text), excluded), None,
            ),
            normalized_text, raw_text,
        ) or ""
    if (not meta.receipt_number and meta.registration_number
            and _first_match(_find_date_in_text, normalized_text, raw_text)
            and _first_match(_find_amount, normalized_text, raw_text)):
        meta.receipt_number = _find_receipt_number_from_filename(
            file_path, excluded | {meta.tax_report_number},
        ) or ""
    if not full_tax_number:
        # Keep the original receipt text, exclusions and filename priority.
        receipt_numbers = {meta.receipt_number} | receipt_numbers_in_text(
            normalized_text, excluded,
        ) | receipt_numbers_in_text(raw_text, excluded)
        meta.tax_report_number = _first_match(
            lambda text: next(_visual_tax_numbers(text, receipt_numbers), None), normalized_text, raw_text,
        ) or meta.tax_report_number
    meta.amount = _first_match(_find_amount, normalized_text, raw_text) or ""
    meta.donor_name = _first_match(_find_donor_name, normalized_text, raw_text) or ""

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

    resolve_identifier_evidence(meta, normalized_text, raw_text)
    identifier_review = meta.notes
    if identifier_review:
        notes.append(identifier_review)

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
    elif identifier_review:
        meta.extraction_status = STATUS_NEEDS_REVIEW
    elif missing_noncritical:
        meta.extraction_status = STATUS_PARTIAL
    else:
        meta.extraction_status = STATUS_OK

    meta.notes = "; ".join(notes)
    return meta
