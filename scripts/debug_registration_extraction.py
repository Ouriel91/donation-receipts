"""Debug script: print receipts where registration_number is empty.

Usage (from repo root):
    python scripts/debug_registration_extraction.py

Scans receipts/primary/2026/**/*.pdf and for each PDF where the extracted
registration_number is empty, prints:
  - file name
  - raw lines containing registration-related keywords (58/51/52/55/מלכ/עמות/תאגיד)
  - normalized lines containing the same keywords in reversed RTL form
  - all 9-digit candidates in raw/normalized with prefix and validity
  - final metadata.registration_number
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hebrew_text_normalizer import normalize_hebrew_text
from pdf_text_extractor import extract_text_from_pdf, PdfExtractionError
from receipt_metadata_extractor import extract_metadata, _is_valid_corporate_registration_number

# Raw PDF text keywords (natural Hebrew + numeric prefixes)
_RAW_KEYWORDS = ["58", "51", "52", "55", "מלכ", "עמות", "תאגיד"]
# Normalized text keywords (normalizer reverses RTL runs, so Hebrew tokens are mirrored)
_NORM_KEYWORDS = ["58", "51", "52", "55", "כלמ", "התומע", "דיגאת"]

_NINE_DIGITS = re.compile(r"\b\d{9}\b")


def _matching_lines(text: str, keywords: list[str]) -> list[str]:
    return [line for line in text.splitlines() if any(kw in line for kw in keywords)]


def _nine_digit_candidates(text: str) -> list[tuple[str, str, bool]]:
    """Return unique (candidate, prefix, valid) tuples in order of first appearance."""
    seen: set[str] = set()
    results: list[tuple[str, str, bool]] = []
    for m in _NINE_DIGITS.finditer(text):
        cand = m.group(0)
        if cand in seen:
            continue
        seen.add(cand)
        results.append((cand, cand[:2], _is_valid_corporate_registration_number(cand)))
    return results


def _print_separator() -> None:
    print("=" * 70)


def main() -> None:
    receipts_root = Path("receipts/primary/2026")
    if not receipts_root.exists():
        print(f"Directory not found: {receipts_root.resolve()}")
        sys.exit(1)

    pdfs = sorted(receipts_root.rglob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found under {receipts_root.resolve()}")
        sys.exit(0)

    missing_count = 0

    for pdf_path in pdfs:
        try:
            raw_text = extract_text_from_pdf(pdf_path)
        except PdfExtractionError as exc:
            print(f"[ERROR] {pdf_path.name}: {exc}")
            continue

        normalized_text = normalize_hebrew_text(raw_text)
        meta = extract_metadata(normalized_text, pdf_path, raw_text)

        if meta.registration_number:
            continue

        missing_count += 1
        _print_separator()
        print(f"FILE: {pdf_path.name}")
        print(f"PATH: {pdf_path}")
        print()

        raw_lines = _matching_lines(raw_text, _RAW_KEYWORDS)
        print(f"--- Raw lines with {_RAW_KEYWORDS} ({len(raw_lines)}) ---")
        if raw_lines:
            for line in raw_lines:
                print(f"  {line!r}")
        else:
            print("  (none)")
        print()

        norm_lines = _matching_lines(normalized_text, _NORM_KEYWORDS)
        print(f"--- Normalized lines with {_NORM_KEYWORDS} ({len(norm_lines)}) ---")
        if norm_lines:
            for line in norm_lines:
                print(f"  {line!r}")
        else:
            print("  (none)")
        print()

        raw_candidates = _nine_digit_candidates(raw_text)
        print(f"--- 9-digit candidates in raw text ({len(raw_candidates)}) ---")
        if raw_candidates:
            for cand, prefix, valid in raw_candidates:
                verdict = "ACCEPTED" if valid else "rejected"
                print(f"  {cand!r}  prefix={prefix!r}  corporate-prefix={verdict}")
        else:
            print("  (none)")
        print()

        norm_candidates = _nine_digit_candidates(normalized_text)
        print(f"--- 9-digit candidates in normalized text ({len(norm_candidates)}) ---")
        if norm_candidates:
            for cand, prefix, valid in norm_candidates:
                verdict = "ACCEPTED" if valid else "rejected"
                print(f"  {cand!r}  prefix={prefix!r}  corporate-prefix={verdict}")
        else:
            print("  (none)")
        print()

        print(f"--- registration_number: {meta.registration_number!r} ---")
        print()

        print("--- Full metadata ---")
        print(f"  organization_name    : {meta.organization_name!r}")
        print(f"  registration_number  : {meta.registration_number!r}")
        print(f"  receipt_number       : {meta.receipt_number!r}")
        print(f"  tax_report_number    : {meta.tax_report_number!r}")
        print(f"  receipt_date         : {meta.receipt_date!r}")
        print(f"  amount               : {meta.amount!r}")
        print(f"  donor_name           : {meta.donor_name!r}")
        print(f"  donor_id             : {meta.donor_id!r}")
        print(f"  extraction_status    : {meta.extraction_status!r}")
        print(f"  notes                : {meta.notes!r}")
        print()

    _print_separator()
    print(f"Done. {missing_count} / {len(pdfs)} PDFs missing registration_number.")


if __name__ == "__main__":
    main()
