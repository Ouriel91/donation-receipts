"""Synthetic CLI smoke test; run from the repository root, without personal files."""

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import openpyxl
from openpyxl.styles import Font

from src.organization_cache import OrganizationCache
from src.organization_enrichment import DISCLAIMER, HEADERS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path,
                        help="New directory on a local, non-OneDrive filesystem")
    directory = parser.parse_args().directory.absolute()
    directory.mkdir(parents=True, exist_ok=False)
    source, cache_path, output = (directory / name for name in
                                  ("synthetic-input.xlsx", "synthetic-cache.json", "synthetic-output.xlsx"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ינואר"
    ws.append(["מספר עמותה", "תאריך", "סטטוס", "חומרה", "סכום", "קישור"])
    for number, donated in [
        ("012345678", "15/01/2020"), ("012345678", date(2021, 1, 15)),
        ("999999999", "2020-01-15"), (None, "31/02/2020"),
        ("123456789", "2020-01-15"),
    ]:
        ws.append([number, donated, "לבדיקה", "דורש תשומת לב", 100, "synthetic"])
    ws["A2"].font = Font(bold=True)
    ws["F2"].hyperlink = "#'קובצי מקור'!A1"
    ws.freeze_panes = "A2"
    second = wb.create_sheet("פברואר")
    second.append(["תאריך", "מספר עמותה"])
    second.append(["2020-01-15", "012345678"])
    wb.create_sheet("סיכום")["A1"] = "=SUM('ינואר'!E2:E6)"
    wb.create_sheet("קובצי מקור")["A1"] = "synthetic source only"
    wb.save(source)
    wb.close()

    cache = OrganizationCache(cache_path)
    cache.put("012345678", exists=True,
              approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))],
              denial_periods=[(date(2021, 1, 1), date(2021, 12, 31))])
    cache.save()
    stale = OrganizationCache(cache_path, clock=lambda: datetime.now(timezone.utc) - timedelta(days=3))
    stale.put("123456789", exists=True,
              approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))])
    stale.save()

    original, original_cache = source.read_bytes(), cache_path.read_bytes()
    command = [sys.executable, "-m", "src.organization_enrichment", "--input", str(source),
               "--cache", str(cache_path), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "worksheets=2, rows=6, approved=2, denied=1, unknown=3, stale=1" in result.stdout
    assert DISCLAIMER in result.stdout
    assert not result.stderr, result.stderr
    assert source.read_bytes() == original and cache_path.read_bytes() == original_cache
    before, after = openpyxl.load_workbook(source), openpyxl.load_workbook(output)
    try:
        assert before.sheetnames == after.sheetnames
        for sheet in before:
            for row in sheet:
                for cell in row:
                    enriched = after[sheet.title][cell.coordinate]
                    assert (enriched.value, enriched.data_type, enriched._style, enriched.hyperlink) == (
                        cell.value, cell.data_type, cell._style, cell.hyperlink)
        sheet = after["ינואר"]
        assert [sheet.cell(1, c).value for c in range(7, 11)] == list(HEADERS)
        assert [sheet.cell(r, 8).value for r in range(2, 7)] == [
            "approved", "denied", "unknown", "unknown", "unknown"]
        assert sheet["I6"].value == "stale"
        assert sheet["A2"].value == "012345678"
        assert sheet.freeze_panes == "A2"
        assert all(DISCLAIMER in sheet.cell(r, 10).value for r in range(2, 7))
    finally:
        before.close()
        after.close()
    published = output.read_bytes()
    refused = subprocess.run(command, capture_output=True, text=True)
    assert refused.returncode == 2
    assert output.read_bytes() == published and source.read_bytes() == original
    assert not list(directory.glob(".enrichment-*"))
    print(result.stdout, end="")
    print("PASS: synthetic CLI, workbook preservation, unknown/stale results, and no-overwrite check")
    print(f"Platform: {sys.platform}; Python: {sys.version.split()[0]}")
    print(f"Synthetic artifacts: {directory}")


if __name__ == "__main__":
    main()
