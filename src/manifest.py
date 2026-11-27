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

    from src.private_runtime import atomic_json
    atomic_json(manifest_path, entries)


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

def attachment_origins_path(manifest_path, account):
    import re
    from src.private_runtime import safe_path
    if not re.fullmatch(r'[A-Za-z0-9_-]+', account):
        raise ValueError('Invalid attachment account')
    return safe_path(Path(manifest_path).parent / f'attachment-origins-{account}.json')


def load_attachment_origins(manifest_path, account):
    import re
    path = attachment_origins_path(manifest_path, account)
    if not path.exists():
        return {}
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or any(
        not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(names, list)
        or any(not isinstance(name, str) or not name or len(name) > 255
               or '/' in name or chr(92) in name or not name.lower().endswith('.pdf')
               for name in names) for key, names in data.items()
    ):
        raise ValueError('Invalid private attachment origin index')
    return data


def remember_attachment_origin(manifest_path, account, source, content, original_name):
    # The original Gmail attachment name is provenance, never a receipt ID.
    # Bind only to verified saved bytes, and keep it in the user-private runtime.
    import hashlib
    from src.private_runtime import safe_path, atomic_json
    if (not original_name or len(original_name) > 255 or '/' in original_name
            or chr(92) in original_name or not original_name.lower().endswith('.pdf')):
        return
    source = safe_path(source)
    if source.read_bytes() != content:
        raise ValueError('Attachment bytes differ from saved source')
    sha = hashlib.sha256(content).hexdigest()
    path = attachment_origins_path(manifest_path, account)
    data = load_attachment_origins(manifest_path, account)
    names = data.setdefault(sha, [])
    if original_name not in names:
        names.append(original_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, data)


def recover_attachment_origins(provider, manifest_path, account, receipts_dir, email):
    """Backfill missing O/C provenance from the same processed Gmail message.

    No PDF is recreated or renamed. Only role-like PDF attachments are fetched,
    and a name is recorded only when the bytes match an existing saved source.
    """
    import hashlib
    import re
    from src.private_runtime import safe_path
    rows = [row for row in load_manifest(manifest_path)
            if row.get('account') == account and row.get('message_id') == email['message_id']]
    if len(rows) != 1:
        return
    base = safe_path(Path(receipts_dir) / account)
    sources = {}
    for value in rows[0].get('saved_files', []):
        source = safe_path(value)
        if source.is_relative_to(base) and source.is_file() and source.suffix.lower() == '.pdf':
            sources[hashlib.sha256(source.read_bytes()).hexdigest()] = source
    if not sources:
        return
    data = load_attachment_origins(manifest_path, account)
    attachments = email.get('attachments', [])
    for attachment in attachments:
        name = attachment.get('filename', '')
        if not re.search(r'(?:[0-9]+[oc]|[ _-](?:original|copy))\.pdf$', name, re.I):
            continue
        if (sum(a.get('filename') == name for a in attachments) == 1
                and any(name in data.get(sha, []) for sha in sources)):
            continue
        raw = provider.download_attachment(email['message_id'], attachment.get('attachment_id', ''))
        key = hashlib.sha256(raw).hexdigest()
        if key in sources:
            remember_attachment_origin(manifest_path, account, sources[key], raw, name)
