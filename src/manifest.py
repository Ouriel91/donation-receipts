import json
from pathlib import Path


DEFAULT_MANIFEST_PATH = Path("data/processed_messages.json")


def load_manifest(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> list[dict]:
    if not manifest_path.exists():
        return []

    with manifest_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_manifest(
    entries: list[dict],
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with manifest_path.open("w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)


def was_processed(
    message_id: str,
    account: str,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> bool:
    entries = load_manifest(manifest_path)

    return any(
        entry["message_id"] == message_id
        and entry["account"] == account
        for entry in entries
    )


def mark_processed(
    account: str,
    message_id: str,
    saved_files: list[str],
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> None:
    entries = load_manifest(manifest_path)

    if was_processed(message_id, account, manifest_path):
        return

    entries.append({
        "account": account,
        "message_id": message_id,
        "saved_files": saved_files,
    })

    save_manifest(entries, manifest_path)