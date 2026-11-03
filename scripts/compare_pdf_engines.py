#!/usr/bin/env python3
"""
Spike: compare pypdf vs PyMuPDF for Hebrew receipt text extraction.

Usage:
    python scripts/compare_pdf_engines.py [PDF_PATH ...] [--raw]

If no paths are given, all receipts/**/*.pdf are processed.
--raw prints the first 600 chars of raw extracted text from each engine.
"""

import argparse
import sys
from pathlib import Path

# Force UTF-8 output so box-drawing and Hebrew characters render on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Make src/ importable when running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.hebrew_text_normalizer import normalize_hebrew_text
from src.pdf_text_extractor import PdfExtractionError, extract_text_from_pdf
from src.receipt_metadata_extractor import ReceiptMetadata, extract_metadata

try:
    import pymupdf as _fitz
except ImportError:
    try:
        import fitz as _fitz  # type: ignore[no-redef]
    except ImportError:
        sys.exit(
            "PyMuPDF not installed.\n"
            "Run: .venv\\Scripts\\python -m pip install -r requirements-dev.txt"
        )

_SECTION_46_SIGNALS = ("סעיף 46", "section 46")
_COL = 22


def _extract_pymupdf(pdf_path: Path) -> str:
    doc = _fitz.open(str(pdf_path))
    pages = [doc.load_page(i).get_text("text") for i in range(doc.page_count)]
    doc.close()
    return "\n".join(p for p in pages if p).strip()


def _has_section_46(combined_lower: str) -> bool:
    return any(sig.lower() in combined_lower for sig in _SECTION_46_SIGNALS)


def _field_score(meta: ReceiptMetadata) -> int:
    fields = [
        meta.registration_number,
        meta.receipt_number,
        meta.tax_report_number,
        meta.receipt_date,
        meta.amount,
        meta.donor_name or meta.donor_id,
    ]
    return sum(1 for f in fields if f)


def _compare_one(pdf_path: Path, show_raw: bool) -> tuple[int, int]:
    """Returns (pypdf_score, pymupdf_score) out of 7."""

    # ── pypdf ─────────────────────────────────────────────────────────────
    try:
        pypdf_raw = extract_text_from_pdf(pdf_path)
    except Exception as exc:
        print(f"  [pypdf error] {exc}")
        pypdf_raw = ""

    pypdf_norm = normalize_hebrew_text(pypdf_raw)
    pypdf_meta = extract_metadata(pypdf_norm, pdf_path, pypdf_raw)
    pypdf_combined = (pypdf_raw + "\n" + pypdf_norm).lower()
    pypdf_46 = _has_section_46(pypdf_combined)

    # ── PyMuPDF ───────────────────────────────────────────────────────────
    try:
        mu_raw = _extract_pymupdf(pdf_path)
    except Exception as exc:
        print(f"  [pymupdf error] {exc}")
        mu_raw = ""

    mu_norm = normalize_hebrew_text(mu_raw)
    mu_meta = extract_metadata(mu_norm, pdf_path, mu_raw)
    mu_combined = (mu_raw + "\n" + mu_norm).lower()
    mu_46 = _has_section_46(mu_combined)

    pypdf_score = _field_score(pypdf_meta) + (1 if pypdf_46 else 0)
    mu_score = _field_score(mu_meta) + (1 if mu_46 else 0)

    # ── Print table ───────────────────────────────────────────────────────
    print(f"\n── {pdf_path.name} " + "─" * max(0, 70 - len(pdf_path.name)))
    print(f"  {'Field':<26} {'pypdf':<{_COL}} {'PyMuPDF':<{_COL}} Match")
    print("  " + "─" * 72)

    rows = [
        ("registration_number", pypdf_meta.registration_number, mu_meta.registration_number),
        ("receipt_number",      pypdf_meta.receipt_number,      mu_meta.receipt_number),
        ("tax_report_number",   pypdf_meta.tax_report_number,   mu_meta.tax_report_number),
        ("receipt_date",        pypdf_meta.receipt_date,        mu_meta.receipt_date),
        ("amount",              pypdf_meta.amount,              mu_meta.amount),
        ("donor_name",          pypdf_meta.donor_name,          mu_meta.donor_name),
        ("donor_id",            pypdf_meta.donor_id,            mu_meta.donor_id),
        ("section_46",          "✓" if pypdf_46 else "✗",       "✓" if mu_46 else "✗"),
    ]

    for field, pv, mv in rows:
        if pv == mv:
            match = "✓"
        elif pv and not mv:
            match = "✗  ← pypdf only"
        elif mv and not pv:
            match = "✗  ← PyMuPDF only"
        else:
            match = "✗  differ"
        print(f"  {field:<26} {str(pv):<{_COL}} {str(mv):<{_COL}} {match}")

    winner = (
        "" if pypdf_score == mu_score
        else ("← pypdf wins" if pypdf_score > mu_score else "← PyMuPDF wins")
    )
    print(f"\n  Score  pypdf: {pypdf_score}/7   PyMuPDF: {mu_score}/7  {winner}")

    if show_raw:
        print("\n  ── pypdf raw (first 600 chars) ─────────────────────────────────")
        print(pypdf_raw[:600] or "(empty)")
        print("\n  ── PyMuPDF raw (first 600 chars) ───────────────────────────────")
        print(mu_raw[:600] or "(empty)")

    return pypdf_score, mu_score


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare pypdf vs PyMuPDF extraction on Hebrew donation receipts"
    )
    parser.add_argument("pdfs", nargs="*", help="PDF paths (default: receipts/**/*.pdf)")
    parser.add_argument("--raw", action="store_true", help="Print raw text snippets")
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    if args.pdfs:
        paths = [Path(p) for p in args.pdfs]
    else:
        paths = sorted((root / "receipts").glob("**/*.pdf"))

    if not paths:
        sys.exit("No PDF files found.")

    total_pypdf = total_mu = 0
    pypdf_wins = mu_wins = ties = 0

    for path in paths:
        if not path.exists():
            print(f"[skip] not found: {path}")
            continue
        p, m = _compare_one(path, args.raw)
        total_pypdf += p
        total_mu += m
        if p > m:
            pypdf_wins += 1
        elif m > p:
            mu_wins += 1
        else:
            ties += 1

    n = pypdf_wins + mu_wins + ties
    if n == 0:
        return

    print(f"\n{'═' * 74}")
    print(f"SUMMARY  {n} PDFs  |  pypdf wins: {pypdf_wins}  PyMuPDF wins: {mu_wins}  ties: {ties}")
    print(f"Avg score  pypdf: {total_pypdf/n:.1f}/7   PyMuPDF: {total_mu/n:.1f}/7")

    if mu_wins > pypdf_wins:
        print("RECOMMENDATION: switch to PyMuPDF (or use as primary with pypdf fallback)")
    elif pypdf_wins > mu_wins:
        print("RECOMMENDATION: keep pypdf")
    else:
        print("RECOMMENDATION: engines equivalent — keep pypdf (simpler dependency)")


if __name__ == "__main__":
    main()
