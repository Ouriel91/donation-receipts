"""Tests for PyMuPDF fallback in dual-engine PDF extraction."""
from pathlib import Path
from unittest.mock import patch

from src.receipt_summary import _build_row

# Fake path — both extraction functions are patched, so no real file is needed.
_PATH = Path("receipts/primary/2026/06_June/05_06_26__receipt_1.pdf")

# pypdf text: has date and amount but no registration number.
_PYPDF_TEXT = "קרן דוגמה\n05/06/2026\n₪200.00"

# PyMuPDF text: has registration number + date + amount (score 3 vs pypdf's 2).
_PYMUPDF_TEXT = "קרן דוגמה\n580794683 :עמותה רשומה\n05/06/2026\n₪200.00"


def test_pymupdf_fallback_provides_registration_number():
    with (
        patch("src.receipt_summary.extract_text_from_pdf", return_value=_PYPDF_TEXT),
        patch("src.receipt_summary.extract_text_from_pdf_pymupdf", return_value=_PYMUPDF_TEXT),
    ):
        meta = _build_row(_PATH, "test_account", None, 2026)

    assert meta.registration_number == "580794683"


def test_pypdf_wins_when_pymupdf_empty():
    with (
        patch("src.receipt_summary.extract_text_from_pdf", return_value=_PYPDF_TEXT),
        patch("src.receipt_summary.extract_text_from_pdf_pymupdf", return_value=""),
    ):
        meta = _build_row(_PATH, "test_account", None, 2026)

    assert meta.amount == "200.00"
    assert meta.receipt_date == "05/06/2026"


def test_pypdf_wins_when_pymupdf_unavailable():
    with (
        patch("src.receipt_summary.extract_text_from_pdf", return_value=_PYPDF_TEXT),
        patch("src.receipt_summary.extract_text_from_pdf_pymupdf", return_value=""),
    ):
        meta = _build_row(_PATH, "test_account", None, 2026)

    assert meta.registration_number == ""
    assert meta.amount == "200.00"


def test_pypdf_preferred_when_scores_equal():
    with (
        patch("src.receipt_summary.extract_text_from_pdf", return_value=_PYMUPDF_TEXT),
        patch("src.receipt_summary.extract_text_from_pdf_pymupdf", return_value=_PYMUPDF_TEXT),
    ):
        meta = _build_row(_PATH, "test_account", None, 2026)

    assert meta.registration_number == "580794683"
