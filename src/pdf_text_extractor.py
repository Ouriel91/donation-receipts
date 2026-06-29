import io
from pathlib import Path

import pypdf


class PdfExtractionError(Exception):
    pass


def extract_text_from_pdf(pdf_path: Path | str) -> str:
    path = Path(pdf_path)

    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:
        raise PdfExtractionError(f"Could not read PDF file {path}: {exc}") from exc

    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)

    return "\n".join(pages_text).strip()


def extract_text_from_pdf_bytes(content: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception as exc:
        raise PdfExtractionError(f"Could not read PDF from bytes: {exc}") from exc

    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)

    return "\n".join(pages_text).strip()


def extract_text_from_pdf_pymupdf(pdf_path: Path | str) -> str:
    """Extract text using PyMuPDF (fitz). Returns '' if PyMuPDF is not installed."""
    try:
        import fitz  # type: ignore[import]
    except ImportError:
        return ""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")
    try:
        doc = fitz.open(str(path))
        pages_text = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(t for t in pages_text if t).strip()
    except Exception as exc:
        raise PdfExtractionError(f"PyMuPDF could not read PDF {path}: {exc}") from exc


def extract_text_from_pdf_pymupdf_bytes(content: bytes) -> str:
    """Extract text using PyMuPDF from bytes. Returns '' if PyMuPDF is not installed."""
    try:
        import fitz  # type: ignore[import]
    except ImportError:
        return ""
    try:
        doc = fitz.open(stream=content, filetype="pdf")
        pages_text = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(t for t in pages_text if t).strip()
    except Exception as exc:
        raise PdfExtractionError(f"PyMuPDF could not read PDF from bytes: {exc}") from exc
