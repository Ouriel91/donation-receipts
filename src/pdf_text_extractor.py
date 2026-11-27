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


def extract_tax_layout(pdf_path: Path | str):
    """Read only complete labelled fields; return (joined digits, bold digits).

    All such physical fields must agree. Unsupported spans are retained as a
    conflicting sentinel, so a second unresolvable field cannot be ignored.
    No raw text or font details are logged or persisted.
    """
    import re
    from src.receipt_metadata_extractor import (_EXPLICIT_TAX_BEFORE,
        _OBSERVED_TAX_INLINE, _IDENTIFIER_MARKS)
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            return ()
    found = set()
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            for block in page.get_text('dict').get('blocks', ()):
                for line in block.get('lines', ()):
                    spans = line.get('spans', ())
                    chars = []
                    for span in spans:
                        txt = _IDENTIFIER_MARKS.sub('', span.get('text', ''))
                        bold = bool(span.get('flags', 0) & 16)
                        chars.extend((c, bold) for c in txt)
                    text = ''.join(c for c, _ in chars)
                    match = _EXPLICIT_TAX_BEFORE.fullmatch(text) or _OBSERVED_TAX_INLINE.fullmatch(text)
                    if not match:
                        continue
                    joined = match.group(1)
                    if not re.fullmatch(r'[0-9]{3,8}', joined):
                        found.add((joined, ''))
                        continue
                    styles = [b for _, b in chars[match.start(1):match.end(1)]]
                    # Accept an entirely bold value or exactly one ordinary
                    # leading digit followed by one contiguous bold run.
                    if all(styles):
                        found.add((joined, joined))
                    elif len(joined) >= 4 and not styles[0] and all(styles[1:]):
                        found.add((joined, joined[1:]))
                    else:
                        found.add((joined, ''))
    return tuple(sorted(found))
