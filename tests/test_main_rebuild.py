import io
import sys
from pathlib import Path
from unittest.mock import patch

from src.main import _rebuild_year_dir


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_rebuild_deletes_existing_dir(tmp_path):
    target = tmp_path / "primary" / "2026"
    target.mkdir(parents=True)
    (target / "receipt.pdf").write_bytes(b"fake")

    out = _capture(_rebuild_year_dir, tmp_path, "primary", 2026)

    assert not target.exists()
    assert "deleted" in out


def test_rebuild_no_op_when_missing(tmp_path):
    out = _capture(_rebuild_year_dir, tmp_path, "primary", 2026)

    assert "skipping" in out


def test_rebuild_deletes_only_year_dir(tmp_path):
    target = tmp_path / "primary" / "2026"
    sibling = tmp_path / "primary" / "2025"
    target.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (sibling / "old.pdf").write_bytes(b"keep")

    _rebuild_year_dir(tmp_path, "primary", 2026)

    assert not target.exists()
    assert sibling.exists()
    assert (sibling / "old.pdf").exists()


def test_rebuild_preserves_sibling_files(tmp_path):
    target = tmp_path / "primary" / "2026"
    target.mkdir(parents=True)
    manifest = tmp_path / "processed_messages.json"
    manifest.write_text("{}")

    _rebuild_year_dir(tmp_path, "primary", 2026)

    assert not target.exists()
    assert manifest.exists()


def test_rebuild_dry_run_skips_deletion(tmp_path):
    target = tmp_path / "primary" / "2026"
    target.mkdir(parents=True)
    receipt = target / "receipt.pdf"
    receipt.write_bytes(b"fake")

    out = _capture(_rebuild_year_dir, tmp_path, "primary", 2026, dry_run=True)

    assert target.exists()
    assert receipt.exists()
    assert "dry run" in out


def test_rebuild_dry_run_no_op_when_missing(tmp_path):
    out = _capture(_rebuild_year_dir, tmp_path, "primary", 2026, dry_run=True)

    assert "skipping" in out


def test_rebuild_warns_on_directory_permission_error(tmp_path):
    month_dir = tmp_path / "primary" / "2026" / "06_June"
    month_dir.mkdir(parents=True)

    original_rmdir = Path.rmdir

    def patched_rmdir(self):
        if self == month_dir:
            raise PermissionError(13, "Access is denied")
        original_rmdir(self)

    with patch.object(Path, "rmdir", patched_rmdir):
        out = _capture(_rebuild_year_dir, tmp_path, "primary", 2026)

    assert "warning" in out
    assert "06_June" in out


def test_rebuild_deletes_nested_files_before_dirs(tmp_path):
    month_dir = tmp_path / "primary" / "2026" / "06_June"
    month_dir.mkdir(parents=True)
    pdf = month_dir / "receipt.pdf"
    pdf.write_bytes(b"fake")

    _rebuild_year_dir(tmp_path, "primary", 2026)

    assert not (tmp_path / "primary" / "2026").exists()
