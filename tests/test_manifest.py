from pathlib import Path

from src.manifest import (
    load_manifest,
    mark_processed,
    save_manifest,
    was_processed,
)


def test_load_manifest_returns_empty_when_missing(tmp_path):
    manifest_path = tmp_path / "manifest.json"

    result = load_manifest(manifest_path)

    assert result == []


def test_save_and_load_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"

    entries = [
        {
            "account": "oriel",
            "message_id": "msg_001",
            "saved_files": ["receipt.pdf"],
        }
    ]

    save_manifest(entries, manifest_path)

    loaded = load_manifest(manifest_path)

    assert loaded == entries


def test_was_processed_returns_true(tmp_path):
    manifest_path = tmp_path / "manifest.json"

    save_manifest([
        {
            "account": "oriel",
            "message_id": "msg_001",
            "saved_files": [],
        }
    ], manifest_path)

    assert was_processed(
        "msg_001",
        "oriel",
        manifest_path,
    )


def test_was_processed_returns_false(tmp_path):
    manifest_path = tmp_path / "manifest.json"

    save_manifest([], manifest_path)

    assert not was_processed(
        "missing",
        "oriel",
        manifest_path,
    )


def test_mark_processed_adds_entry(tmp_path):
    manifest_path = tmp_path / "manifest.json"

    mark_processed(
        account="oriel",
        message_id="msg_001",
        saved_files=["receipt.pdf"],
        manifest_path=manifest_path,
    )

    entries = load_manifest(manifest_path)

    assert len(entries) == 1
    assert entries[0]["message_id"] == "msg_001"


def test_mark_processed_does_not_duplicate(tmp_path):
    manifest_path = tmp_path / "manifest.json"

    mark_processed(
        account="oriel",
        message_id="msg_001",
        saved_files=["receipt.pdf"],
        manifest_path=manifest_path,
    )

    mark_processed(
        account="oriel",
        message_id="msg_001",
        saved_files=["receipt.pdf"],
        manifest_path=manifest_path,
    )

    entries = load_manifest(manifest_path)

    assert len(entries) == 1