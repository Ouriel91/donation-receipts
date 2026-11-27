from pathlib import Path

import pytest

from src import receipt_summary
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_metadata_extractor import (
    IDENTIFIER_REVIEW_NOTES,
    STATUS_NEEDS_REVIEW,
    extract_metadata,
)


@pytest.mark.parametrize(("text", "receipt", "approval"), [
    ("קבלה על תרומה מספר 00123AB", "00123AB", ""),
    ("קבלה עבור תרומה 00124", "00124", ""),
    ("אישור דיווח התרומה לרשות המסים: 012", "", "012"),
    ("מספר אישור דיווח התרומה לרשות המסים: 013", "", "013"),
    ("קבלה על\nתרומה מס׳ —\n00014Q.", "00014Q", ""),
    ("אישור דיווח\nהתרומה לרשות המיסים\n： 014A", "", "014A"),
    ("המסים לרשות התרומה דיווח אישור: 015", "", "015"),
    ("קבלה עבור תרומה 00124\nאישור דיווח התרומה לרשות המסים: 012", "00124", "012"),
    ("קבלה על תרומה 2", "", ""),
    ("קבלה על תרומה 500 ₪", "", ""),
    ("קבלה על תרומה 2026", "", ""),
    ("אישור דיווח התרומה לרשות המסים: 2026", "", ""),
    ("אישור דיווח התרומה לרשות המסים:\n14581 # קבלה", "14581", ""),
])
def test_identifier_labels(text, receipt, approval):
    meta = extract_metadata(normalize_hebrew_text(text), Path("sample.pdf"), text)
    assert (meta.receipt_number, meta.tax_report_number) == (receipt, approval)


@pytest.mark.parametrize(("text", "field"), [
    ("מספר קבלה: 03501\nקבלה על תרומה מספר 03502", "receipt_number"),
    ("קבלה עבור תרומה 03501\nקבלה עבור תרומה 03502", "receipt_number"),
    ("אישור דיווח התרומה לרשות המסים: 051\nאישור דיווח התרומה לרשות המסים: 052", "tax_report_number"),
    ("051 : המסים לרשות התרומה דיווח אישור מספר\n052 : המסים לרשות התרומה דיווח אישור מספר", "tax_report_number"),
])
def test_conflicting_values_require_review(text, field):
    meta = extract_metadata(text, Path("sample_12345.pdf"))
    assert getattr(meta, field) == ""
    assert meta.extraction_status == STATUS_NEEDS_REVIEW
    assert IDENTIFIER_REVIEW_NOTES[field] in meta.notes


def test_reader_conflict_is_preserved_by_summary(monkeypatch):
    monkeypatch.setattr(receipt_summary, "extract_text_from_pdf", lambda _: "מספר קבלה: 03501")
    monkeypatch.setattr(receipt_summary, "extract_text_from_pdf_pymupdf", lambda _: "מספר קבלה: 03502")
    meta = receipt_summary._extract_row(Path("sample_12345.pdf"), "test", None, 2026)
    assert meta.receipt_number == ""
    assert meta.extraction_status == STATUS_NEEDS_REVIEW
    assert IDENTIFIER_REVIEW_NOTES["receipt_number"] in meta.notes


def test_explicit_receipt_has_priority_over_weak_heading():
    text = "קבלה עבור תרומה 03502\nמספר קבלה: 03501"
    assert extract_metadata(text, Path("sample.pdf")).receipt_number == "03501"
