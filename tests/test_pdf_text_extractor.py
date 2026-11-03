from pathlib import Path

import pytest
from fpdf import FPDF

from src.pdf_text_extractor import PdfExtractionError, extract_text_from_pdf


def _make_text_pdf(path: Path, text: str) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text=text)
    pdf.output(str(path))


def _make_empty_pdf(path: Path) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.output(str(path))


def test_extracts_text(tmp_path):
    pdf_path = tmp_path / "receipt.pdf"
    _make_text_pdf(pdf_path, "Hello Donation")

    result = extract_text_from_pdf(pdf_path)

    assert "Hello Donation" in result


def test_empty_pdf_returns_empty_string(tmp_path):
    pdf_path = tmp_path / "empty.pdf"
    _make_empty_pdf(pdf_path)

    result = extract_text_from_pdf(pdf_path)

    assert result == ""


def test_missing_file_raises_file_not_found(tmp_path):
    missing = tmp_path / "nonexistent.pdf"

    with pytest.raises(FileNotFoundError):
        extract_text_from_pdf(missing)


def test_corrupt_pdf_raises_extraction_error(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")

    with pytest.raises(PdfExtractionError):
        extract_text_from_pdf(corrupt)
