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
