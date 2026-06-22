import io
from pathlib import Path

from src.main import _compute_gmail_stats, _print_summary_stats


# --- _compute_gmail_stats ---

def test_compute_stats_empty():
    stats = _compute_gmail_stats([])
    assert stats["emails_fetched"] == 0
    assert stats["processed"] == 0
    assert stats["saved_receipts"] == 0
    assert stats["skipped_duplicate"] == 0
    assert stats["skipped_low_confidence"] == 0
    assert stats["skipped_no_supported_attachments"] == 0
    assert stats["errors"] == 0


def test_compute_stats_emails_fetched():
    results = [
        {"status": "processed", "planned_paths": [], "skipped_attachments": []},
        {"status": "skipped_duplicate"},
    ]
    assert _compute_gmail_stats(results)["emails_fetched"] == 2


def test_compute_stats_mixed():
    results = [
        {"status": "processed", "planned_paths": ["a.pdf"], "skipped_attachments": []},
        {"status": "skipped_duplicate"},
        {"status": "skipped_low_confidence", "planned_paths": []},
        {"status": "skipped_no_supported_attachments", "skipped_attachments": ["unsupported ext"]},
    ]
    stats = _compute_gmail_stats(results)
    assert stats["processed"] == 1
    assert stats["skipped_duplicate"] == 1
    assert stats["skipped_low_confidence"] == 1
    assert stats["skipped_no_supported_attachments"] == 1


def test_compute_stats_saved_receipts():
    results = [
        {"status": "processed", "planned_paths": ["a.pdf"], "skipped_attachments": []},
        {"status": "processed", "planned_paths": ["b.pdf", "c.pdf"], "skipped_attachments": []},
    ]
    assert _compute_gmail_stats(results)["saved_receipts"] == 3


def test_compute_stats_errors():
    results = [
        {
            "status": "processed",
            "planned_paths": ["a.pdf"],
            "skipped_attachments": ["download failed for x.pdf: API error", "unsupported ext: y.exe"],
        },
    ]
    assert _compute_gmail_stats(results)["errors"] == 2


def test_compute_stats_errors_across_multiple_results():
    results = [
        {"status": "processed", "planned_paths": [], "skipped_attachments": ["err1"]},
        {"status": "skipped_no_supported_attachments", "skipped_attachments": ["err2", "err3"]},
    ]
    assert _compute_gmail_stats(results)["errors"] == 3


# --- _print_summary_stats accuracy ---

def _capture_summary(counts, account="test", year=2026) -> str:
    import sys
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        _print_summary_stats(Path("report.xlsx"), counts, account=account, year=year)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def test_accuracy_full():
    output = _capture_summary({"ok": 20, "partial": 3, "needs_review": 1})
    assert "83.3%" in output


def test_accuracy_all_ok():
    output = _capture_summary({"ok": 5})
    assert "100.0%" in output


def test_accuracy_zero_total():
    output = _capture_summary({})
    assert "N/A" in output


def test_summary_shows_account_and_year():
    output = _capture_summary({"ok": 1}, account="primary", year=2026)
    assert "primary" in output
    assert "2026" in output


def test_summary_shows_all_status_counts():
    output = _capture_summary({"ok": 10, "partial": 3, "needs_review": 2})
    assert "10" in output
    assert "3" in output
    assert "2" in output
