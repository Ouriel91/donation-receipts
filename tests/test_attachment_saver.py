from pathlib import Path

import pytest

from src.attachment_saver import (
    build_receipt_directory,
    plan_receipt_path,
    sanitize_filename_part,
    save_attachment,
)


def test_build_receipt_directory():
    result = build_receipt_directory(
        base_dir=Path("receipts"),
        account="oriel",
        date_value="2026-05-24",
    )

    assert result == Path("receipts") / "oriel" / "2026" / "05_May"


def test_sanitize_filename_part():
    assert sanitize_filename_part("Donation Receipt!") == "donation_receipt"


def test_plan_receipt_path():
    result = plan_receipt_path(
        base_dir=Path("receipts"),
        account="oriel",
        date_value="2026-05-24",
        original_filename="receipt.pdf",
        label="Matan Beseter",
    )

    assert result == Path("receipts") / "oriel" / "2026" / "05_May" / "24_05_26__matan_beseter.pdf"


def test_plan_receipt_path_rejects_unsupported_extension():
    with pytest.raises(ValueError):
        plan_receipt_path(
            base_dir=Path("receipts"),
            account="oriel",
            date_value="2026-05-24",
            original_filename="receipt.exe",
        )


def test_save_attachment_dry_run_does_not_write_file(tmp_path):
    target = tmp_path / "receipt.pdf"

    result = save_attachment(
        content=b"fake content",
        target_path=target,
        dry_run=True,
    )

    assert result == target
    assert not target.exists()


def test_save_attachment_writes_file(tmp_path):
    target = tmp_path / "receipt.pdf"

    result = save_attachment(
        content=b"fake content",
        target_path=target,
        dry_run=False,
    )

    assert result == target
    assert target.exists()
    assert target.read_bytes() == b"fake content"


def test_save_attachment_never_overwrites_existing_file(tmp_path):
    target = tmp_path / "receipt.pdf"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        save_attachment(
            content=b"new content",
            target_path=target,
            dry_run=False,
        )

    assert target.read_bytes() == b"existing"


def test_plan_receipt_path_handles_collision(tmp_path):
    directory = tmp_path / "oriel" / "2026" / "05_May"
    directory.mkdir(parents=True)
    existing = directory / "24_05_26__receipt.pdf"
    existing.write_bytes(b"existing")

    result = plan_receipt_path(
        base_dir=tmp_path,
        account="oriel",
        date_value="2026-05-24",
        original_filename="receipt.pdf",
    )

    assert result == directory / "24_05_26__receipt__2.pdf"