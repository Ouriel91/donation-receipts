"""Debug script: trace registration-number extraction internals for a target PDF.

Usage:
    python debug_registration.py <path-to-pdf>

Prints:
  - Every raw line that contains 58 / מלכ / עמותה
  - Every normalized line that contains 58 / כלמ / התומע  (reversed anchor fragments)
  - Every 9-digit candidate with its prefix, nearby anchor, and validity result
  - The final registration_number selected by _find_registration_number
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Ensure src/ is importable when run from the project root.
sys.path.insert(0, str(Path(__file__).parent))

from src.hebrew_text_normalizer import normalize_hebrew_text
from src.pdf_text_extractor import extract_text_from_pdf
from src.receipt_metadata_extractor import (
    _ASSOC_ANCHOR,
    _EXPLICIT_REG_AFTER,
    _EXPLICIT_REG_BEFORE,
    _NINE_DIGITS,
    _find_registration_number,
    _find_registration_number_by_explicit_anchor,
    _is_valid_corporate_registration_number,
)

# Patterns for lines of interest
_RAW_INTEREST = re.compile(r'58|מלכ|עמותה', re.UNICODE)
_NORM_INTEREST = re.compile(r'58|כלמ|התומע', re.UNICODE)


def _nearby_anchor(line: str) -> str | None:
    m = _ASSOC_ANCHOR.search(line)
    return m.group(0) if m else None


def _debug_candidates(label: str, text: str) -> None:
    print(f"\n--- 9-digit candidates in {label} ---")
    for m in _NINE_DIGITS.finditer(text):
        cand = m.group(1)
        prefix = cand[:2]
        valid = _is_valid_corporate_registration_number(cand)
        # Find line containing this match
        start = text.rfind('\n', 0, m.start()) + 1
        end = text.find('\n', m.end())
        line = text[start: end if end != -1 else len(text)]
        anchor = _nearby_anchor(line)
        print(f"  candidate={cand!r}  prefix={prefix!r}  valid={valid}  anchor={anchor!r}")
        print(f"    line: {line!r}")


def _debug_explicit_matches(label: str, text: str) -> None:
    print(f"\n--- Explicit anchor matches in {label} ---")
    found_any = False
    for m in _EXPLICIT_REG_BEFORE.finditer(text):
        anchor, number = m.group(1), m.group(2)
        valid = _is_valid_corporate_registration_number(number)
        print(f"  BEFORE  anchor={anchor!r}  number={number!r}  valid={valid}")
        found_any = True
    for m in _EXPLICIT_REG_AFTER.finditer(text):
        number, anchor = m.group(1), m.group(2)
        valid = _is_valid_corporate_registration_number(number)
        print(f"  AFTER   number={number!r}  anchor={anchor!r}  valid={valid}")
        found_any = True
    if not found_any:
        print("  (none)")


def main(pdf_path: str) -> None:
    path = Path(pdf_path)
    print(f"=== debug_registration: {path.name} ===\n")

    raw = extract_text_from_pdf(path)
    normalized = normalize_hebrew_text(raw)

    # --- Raw lines of interest ---
    print("=== RAW lines containing 58 / מלכ / עמותה ===")
    for i, line in enumerate(raw.splitlines(), 1):
        if _RAW_INTEREST.search(line):
            print(f"  raw[{i:3d}]: {line!r}")

    # --- Normalized lines of interest ---
    print("\n=== NORMALIZED lines containing 58 / כלמ / התומע ===")
    for i, line in enumerate(normalized.splitlines(), 1):
        if _NORM_INTEREST.search(line):
            print(f"  norm[{i:3d}]: {line!r}")

    # --- Candidate enumeration ---
    _debug_candidates("raw", raw)
    _debug_candidates("normalized", normalized)

    # --- Explicit anchor pattern hits ---
    _debug_explicit_matches("raw", raw)
    _debug_explicit_matches("normalized", normalized)

    # --- Explicit-anchor helper result ---
    anchor_result_raw = _find_registration_number_by_explicit_anchor(raw)
    anchor_result_norm = _find_registration_number_by_explicit_anchor(normalized)
    print(f"\n--- _find_registration_number_by_explicit_anchor ---")
    print(f"  raw:        {anchor_result_raw!r}")
    print(f"  normalized: {anchor_result_norm!r}")

    # --- Final result (mirrors _first_match logic) ---
    final = _find_registration_number(normalized) or _find_registration_number(raw)
    print(f"\n=== FINAL registration_number: {final!r} ===")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python debug_registration.py <path-to-pdf>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
