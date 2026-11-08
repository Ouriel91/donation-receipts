"""Enrich existing Excel reports using only locally supplied cache evidence."""

import argparse
import hashlib
from collections import Counter
from datetime import date, datetime
import os
from pathlib import Path
import re
import sys
import tempfile
from zipfile import BadZipFile

import openpyxl
from openpyxl.styles import Font

from src.organization_cache import OrganizationCache

HEADERS = (
    "Organization existence (cached)",
    "Section 46 on donation date (cached)",
    "Cache freshness",
    "Enrichment notes",
)
DISCLAIMER = "Section 46 uses cached dated intervals; evidence origin is recorded in the notes. This output makes no claim of live tax verification. Registry identity does not establish tax eligibility."


def _safe_path(value):
    from src.private_runtime import safe_path
    return safe_path(value)


def _number(cell):
    if cell.data_type in {"f", "e", "b"}:
        return None
    value = cell.value
    if isinstance(value, str):
        value = value.strip()
        return value if re.fullmatch(r"[0-9]{9}", value) else None
    # Nine digit integers are lossless; never guess zeros lost in numeric storage.
    if type(value) in (int, float) and 100000000 <= value <= 999999999 and value == int(value):
        return str(int(value))
    return None


def _donation_date(cell):
    if cell.data_type in {"f", "e", "b"}:
        return None
    value = cell.value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        for pattern, fmt in ((r"[0-9]{2}/[0-9]{2}/[0-9]{4}", "%d/%m/%Y"),
                             (r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "%Y-%m-%d")):
            if re.fullmatch(pattern, value):
                try:
                    return datetime.strptime(value, fmt).date()
                except ValueError:
                    return None
    return None


def _enrichment(number, donated, cache):
    if number is None or donated is None:
        reasons = []
        if number is None:
            reasons.append("missing/invalid registration number")
        if donated is None:
            reasons.append("missing/invalid donation date")
        return "unknown", "unknown", "not_verified", "; ".join(reasons)
    evidence = cache.lookup(number, donated)
    tax = cache.lookup_tax(number, donated)
    exists = {True: "exists", False: "does_not_exist", None: "unknown"}[evidence["exists"]]
    status = tax["section46_status"]
    kinds = {entry["verification"] for entry in tax["provenance"]}
    reasons = (["tax provenance: " + ", ".join(sorted(kinds))] if kinds else
               ["no verified tax provenance; legacy periods retained but not used"])
    if tax["conflict"]:
        reasons.append("conflicting tax evidence or tax-file binding; unknown")
    if exists == "unknown":
        reasons.append("no usable cached existence evidence")
    if status == "unknown":
        reasons.append("no cached approval/denial covering donation date (or missing entry)")
    if evidence["stale"]:
        reasons.append("legacy existence evidence stale or future-dated")
    if tax["stale"]:
        reasons.append("tax evidence stale; not verified")
    freshness = ("stale" if evidence["stale"] or tax["stale"] else
                 "not_verified" if exists == status == "unknown" else "fresh")
    return exists, status, freshness, "; ".join(reasons) or "explicit cached evidence"


def enrich_workbook(input_path, cache_path, output_path):
    """Append cached evidence, returning aggregate counts without donor data."""
    source, cache_file, output = map(_safe_path, (input_path, cache_path, output_path))
    if source.suffix.lower() != ".xlsx" or output.suffix.lower() != ".xlsx":
        raise ValueError("Input and output must use .xlsx")
    if not source.is_file() or not cache_file.is_file():
        raise ValueError("Input workbook and cache must be existing files")
    if output in (source, cache_file) or os.path.lexists(output):
        raise ValueError("Output must be a new path distinct from input and cache")
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must exist")
    cache = OrganizationCache(cache_file)
    wb = openpyxl.load_workbook(source, data_only=False, keep_links=True)
    if "Registry identity audit" in wb.sheetnames:
        wb.close()
        raise ValueError("Workbook already contains registry audit")
    audit = wb.create_sheet("Registry identity audit")
    audit.append(["Sheet", "Row", "Registration ID", "Original extracted name",
                  "Official name", "Name differs", "Source", "Import date", "File SHA256",
                  "Original workbook SHA256"])
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    audit_next_row = 2
    def literal(cell, value):
        cell.value = value
        if isinstance(value, str):
            cell.data_type = "s"
    temporary = None
    published = False
    counts = Counter()
    try:
        for ws in list(wb.worksheets):
            if ws is audit:
                continue
            headers = [cell.value for cell in ws[1]]
            required = ("מספר עמותה", "תאריך")
            if not any(header in headers for header in required):
                continue
            if any(headers.count(header) != 1 for header in required):
                raise ValueError("Sheet has missing or ambiguous enrichment headers")
            if any(header in headers for header in HEADERS):
                raise ValueError("Workbook already contains enrichment columns")
            if headers.count("שם עמותה") > 1:
                raise ValueError("Ambiguous organization-name header")
            name_col = headers.index("שם עמותה") + 1 if "שם עמותה" in headers else None
            number_col, date_col = (headers.index(header) + 1 for header in required)
            first = ws.max_column + 1
            last_row = ws.max_row
            for offset, header in enumerate(HEADERS):
                ws.cell(1, first + offset, header).font = Font(bold=True)
            counts["worksheets"] += 1
            for row in range(2, last_row + 1):
                if all(ws.cell(row, col).value is None for col in range(1, first)):
                    continue
                number = _number(ws.cell(row, number_col))
                identity = cache.official_identity(number) if number else None
                if identity is not None and name_col is not None:
                    original = ws.cell(row, name_col).value
                    official = identity["official_name"]
                    provenance = identity["provenance"]
                    audit_values = [ws.title, row, number, original, official,
                                    original != official, provenance["source"],
                                    provenance["imported_at"], provenance["file_sha256"], source_sha256]
                    audit_row = audit_next_row
                    audit_next_row += 1
                    for column, value in enumerate(audit_values, 1):
                        literal(audit.cell(audit_row, column), value)
                    literal(ws.cell(row, name_col), official)
                    counts["official_names"] += 1
                elif name_col is not None:
                    counts["unmatched_names"] += 1
                else:
                    counts["missing_name_column"] += 1
                values = _enrichment(_number(ws.cell(row, number_col)),
                                     _donation_date(ws.cell(row, date_col)), cache)
                for offset, value in enumerate(values):
                    ws.cell(row, first + offset, value + "; " + DISCLAIMER if offset == 3 else value)
                counts["rows"] += 1
                counts[values[1]] += 1
                counts["stale"] += values[2] == "stale"
        if not counts["worksheets"]:
            raise ValueError("No worksheets with donation headers found")
        if audit.max_row == 1:
            wb.remove(audit)
        descriptor, temporary = tempfile.mkstemp(prefix=".enrichment-", suffix=".xlsx", dir=output.parent)
        os.close(descriptor)
        wb.save(temporary)
        with open(temporary, "r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(output)
        # Atomic no-clobber publication. Unlike replace(), link() refuses a
        # destination created by another process while the workbook was saved.
        os.link(temporary, output)
        published = True
        return dict(counts)
    finally:
        wb.close()
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                if not published:
                    raise
                print("Warning: output published successfully; temporary workbook cleanup failed.",
                      file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        counts = enrich_workbook(args.input, args.cache, args.output)
    except (OSError, ValueError, BadZipFile) as exc:
        parser.exit(2, f"Enrichment failed ({type(exc).__name__}); check paths, workbook and cache.\n")
    print("Offline enrichment: " + ", ".join(f"{key}={counts.get(key, 0)}" for key in
          ("worksheets", "rows", "approved", "denied", "unknown", "stale",
           "official_names", "unmatched_names")))
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
