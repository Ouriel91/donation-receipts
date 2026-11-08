"""Synthetic, offline workbook enrichment and safe publication checks."""
from datetime import date, datetime, timedelta, timezone
import os
from unittest.mock import mock_open, patch

import openpyxl
from openpyxl.styles import Font, PatternFill
import pytest

from src.organization_cache import OrganizationCache
from src.organization_enrichment import HEADERS, enrich_workbook, main


@pytest.fixture
def demo(tmp_path):
    source, cache_path, output = (tmp_path / name for name in ("input.xlsx", "cache.json", "output.xlsx"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ינואר"
    ws.append(["מספר עמותה", "תאריך", "סטטוס", "חומרה", "סכום", "קישור"])
    for number, donated in [
        ("012345678", "15/01/2020"), ("012345678", date(2021, 1, 15)),
        ("999999999", "2020-01-15"), (None, "2020-01-15"),
        ("bad", "31/02/2020"), ("123456789", "2020-01-15"),
    ]:
        ws.append([number, donated, "לבדיקה", "דורש תשומת לב", 100, "source"])
    ws["F2"].hyperlink = "#'קובצי מקור'!A1"
    ws["A2"].font = Font(bold=True, color="FF0000")
    ws["A2"].fill = PatternFill("solid", fgColor="FFFF00")
    ws["A2"].number_format = "@"
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = "A1:F7"
    ws.column_dimensions["A"].width = 24
    ws.sheet_view.rightToLeft = True
    ws2 = wb.create_sheet("פברואר")
    ws2.append(["תאריך", "מספר עמותה"])
    ws2.append([datetime(2020, 1, 15), "012345678"])
    wb.create_sheet("סיכום")["A1"] = "=SUM('ינואר'!E2:E7)"
    wb.create_sheet("קובצי מקור")["A1"] = "synthetic source"
    wb.save(source)
    wb.close()
    cache = OrganizationCache(cache_path)
    cache.put("012345678", exists=True, approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))],
              denial_periods=[(date(2021, 1, 1), date(2021, 12, 31))])
    cache.save()
    cache.store_tax_evidence({
        "registration_number": "012345678", "tax_file_number": "987654321",
        "source_url": "https://www.gov.il/he/service/confirmation-of-donations",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "evidence_sha256": "a" * 64,
        "verification": "operator_attested_official_intervals",
        "approval_periods": [["2020-01-01", "2020-12-31"]],
        "denial_periods": [["2021-01-01", "2021-12-31"]],
    })
    old = OrganizationCache(cache_path, clock=lambda: datetime.now(timezone.utc) - timedelta(days=3))
    old.put("123456789", exists=True, approval_periods=[(date(2020, 1, 1), date(2020, 12, 31))])
    old.save()
    return source, cache_path, output


def test_demo_preserves_original_and_reports_explicit_evidence(demo, capsys):
    source, cache, output = demo
    original, original_cache = source.read_bytes(), cache.read_bytes()
    main(["--input", str(source), "--cache", str(cache), "--output", str(output)])
    assert "worksheets=2, rows=7, approved=2, denied=1, unknown=4, stale=1" in capsys.readouterr().out
    assert source.read_bytes() == original and cache.read_bytes() == original_cache
    before, after = openpyxl.load_workbook(source), openpyxl.load_workbook(output)
    assert before.sheetnames == after.sheetnames
    for ws in before:
        for row in ws:
            for cell in row:
                result = after[ws.title][cell.coordinate]
                assert (result.value, result.data_type, result._style, result.hyperlink) == (
                    cell.value, cell.data_type, cell._style, cell.hyperlink)
        assert after[ws.title].freeze_panes == ws.freeze_panes
        assert after[ws.title].auto_filter.ref == ws.auto_filter.ref
        assert after[ws.title].column_dimensions["A"].width == ws.column_dimensions["A"].width
    ws = after["ינואר"]
    assert [ws.cell(1, c).value for c in range(7, 11)] == list(HEADERS)
    assert [ws.cell(r, 8).value for r in range(2, 8)] == ["approved", "denied", "unknown", "unknown", "unknown", "unknown"]
    assert ws["I7"].value == "stale"
    assert "missing/invalid" in ws["J5"].value
    assert "provenance" in ws["J2"].value
    before.close()
    after.close()


@pytest.mark.parametrize("kind", ["source", "cache", "existing", "symlink", "hardlink"])
def test_output_collisions(demo, kind):
    source, cache, output = demo
    if kind == "source":
        output = source
    elif kind == "cache":
        output = cache
    elif kind == "existing":
        output.write_bytes(b"keep")
    elif kind == "symlink":
        try:
            output.symlink_to(source)
        except OSError as exc:
            # Windows without Developer Mode or the create-symlink privilege.
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows account lacks symbolic-link creation privilege")
            raise
    else:
        os.link(source, output)
    original = source.read_bytes()
    with pytest.raises(ValueError):
        enrich_workbook(source, cache, output)
    assert source.read_bytes() == original


@pytest.mark.parametrize("failure", ["save", "publish", "race"])
def test_failure_leaves_no_partial_output(demo, failure):
    source, cache, output = demo
    def publish(*args):
        if failure == "race":
            output.write_bytes(b"concurrent")
            raise FileExistsError()
        raise OSError("synthetic failure")
    target = "openpyxl.workbook.workbook.Workbook.save" if failure == "save" else "src.organization_enrichment.os.link"
    with patch(target, side_effect=publish), pytest.raises(OSError):
        enrich_workbook(source, cache, output)
    assert list(output.parent.glob(".enrichment-*")) == []
    if failure == "race":
        assert output.read_bytes() == b"concurrent"
    else:
        assert not output.exists()


@pytest.mark.parametrize("number,donated", [
    (12345678, "2020-01-15"), (True, "2020-01-15"), ("=123456789", "2020-01-15"),
    ("012345678", "01/02/20"), ("012345678", 12345), ("012345678", "=TODAY()"),
    ("１２３４５６７８９", "2020-01-15"), ("012345678", None),
])
def test_invalid_fields_never_lookup(demo, number, donated):
    source, cache, output = demo
    wb = openpyxl.Workbook()
    wb.active.append(["מספר עמותה", "תאריך"])
    wb.active.append([number, donated])
    wb.save(source)
    with patch.object(OrganizationCache, "lookup", side_effect=AssertionError("must not lookup")):
        enrich_workbook(source, cache, output)
    result = openpyxl.load_workbook(output)
    assert [result.active.cell(2, c).value for c in (3, 4, 5)] == ["unknown", "unknown", "not_verified"]
    result.close()


@pytest.mark.parametrize("kind", ["missing_cache", "bad_cache", "no_headers", "partial", "duplicate", "enriched", "bad_workbook"])
def test_unusable_inputs_fail_without_output(demo, kind):
    source, cache, output = demo
    if kind == "missing_cache":
        cache = cache.with_name("missing.json")
    elif kind == "bad_cache":
        cache.write_text("{}")
    elif kind == "bad_workbook":
        source.write_bytes(b"invalid")
    else:
        wb = openpyxl.Workbook()
        wb.active.append({"no_headers": ["other"], "partial": ["תאריך"],
                          "duplicate": ["תאריך", "מספר עמותה", "תאריך"],
                          "enriched": ["תאריך", "מספר עמותה", HEADERS[0]]}[kind])
        wb.save(source)
    with pytest.raises(Exception):
        enrich_workbook(source, cache, output)
    assert not output.exists()


def test_numeric_number_and_uncovered_date_are_not_inferred(demo):
    source, cache_path, output = demo
    cache = OrganizationCache(cache_path)
    cache.put("234567890", exists=False)
    cache.save()
    wb = openpyxl.Workbook()
    wb.active.append(["מספר עמותה", "תאריך"])
    wb.active.append([234567890, "2020-01-15"])
    wb.active.append(["012345678", "2022-01-15"])
    wb.save(source)
    enrich_workbook(source, cache_path, output)
    result = openpyxl.load_workbook(output)
    assert [result.active.cell(2, c).value for c in (3, 4, 5)] == ["does_not_exist", "unknown", "fresh"]
    assert [result.active.cell(3, c).value for c in (3, 4, 5)] == ["exists", "unknown", "fresh"]
    result.close()


def test_temporary_workbook_flushes_with_write_capable_handle(demo):
    source, cache, output = demo
    opened = mock_open()
    stream = opened.return_value
    with patch("src.organization_enrichment.open", opened), patch(
        "src.organization_enrichment.os.fsync"
    ) as sync:
        enrich_workbook(source, cache, output)
    opened.assert_called_once()
    assert opened.call_args.args[1] == "r+b"
    stream.flush.assert_called_once_with()
    sync.assert_called_once_with(stream.fileno.return_value)
    assert output.is_file()


def test_cleanup_failure_after_publication_preserves_cli_success(demo, capsys):
    source, cache, output = demo
    original = source.read_bytes()
    with patch("src.organization_enrichment.os.unlink", side_effect=PermissionError("sensitive detail")):
        main(["--input", str(source), "--cache", str(cache), "--output", str(output)])
    captured = capsys.readouterr()
    assert "rows=7" in captured.out
    assert captured.err == "Warning: output published successfully; temporary workbook cleanup failed.\n"
    assert source.read_bytes() == original
    wb = openpyxl.load_workbook(output)
    assert wb["ינואר"]["H2"].value == "approved"
    wb.close()
    leftovers = list(output.parent.glob(".enrichment-*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == output.read_bytes()
    with pytest.raises(ValueError, match="new path"):
        enrich_workbook(source, cache, output)
    leftovers[0].unlink()
