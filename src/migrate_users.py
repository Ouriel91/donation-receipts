"""Copy a quiescent single-user installation to a new user-scoped project."""
import argparse
import hashlib
from io import BytesIO
import json
import os
import re
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, unescape
from zipfile import ZipFile

from src.private_runtime import file_lock, new_bytes, safe_path
from src.user_scope import UserScope, identifier, mailbox

_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'


def _files(root):
    root = safe_path(root)
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        safe_path(entry)
        if entry.is_dir():
            yield from _files(entry)
        elif entry.is_file():
            yield entry
        else:
            raise ValueError('Unexpected entry in legacy data')


_FORMULA_ELEMENT = re.compile(r'(<(?:[\w.-]+:)?f(?:\s[^<>]*?)?>)([^<]*)(</(?:[\w.-]+:)?f>)')
_HYPERLINK = re.compile(r'(^\s*(?:_xlfn\.)?HYPERLINK\s*\(\s*)"((?:""|[^"])*)"(?=\s*[,;)])', re.IGNORECASE)


class LinkMappingError(ValueError):
    def __init__(self, reason, context, target):
        self.target = target
        digest = hashlib.sha256(target.encode('utf-8')).hexdigest()
        super().__init__(f'{reason}; location={context}; target_sha256={digest}')


def _local_candidates(target, directory):
    # Native paths can contain a literal percent or #. A file URI encodes
    # those characters and must be decoded exactly once.
    raw = target.replace('\\', '/')
    if raw.startswith('#'):
        return ()
    if raw.lower().startswith('file:'):
        uri = urlsplit(raw)
        value = unquote(uri.path, errors='strict')
        if uri.netloc and uri.netloc.lower() != 'localhost':
            value = (uri.netloc + value if re.fullmatch('[A-Za-z]:', uri.netloc)
                     else '//' + uri.netloc + value)
        if uri.query or uri.fragment:
            if value.casefold().endswith('.pdf'):
                raise ValueError('file_uri_query_or_fragment')
            return ()
        values = (value,)
    elif re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', raw) and not re.match(r'^[A-Za-z]:', raw):
        return ()
    else:
        try:
            decoded = unquote(raw, errors='strict')
        except UnicodeError:
            decoded = raw  # A native path may contain literal percent characters.
        values = (raw, decoded)
    candidates = []
    for value in values:
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', value):
            value = value[1:]
        path = Path(value)
        if path.suffix.casefold() == '.pdf':
            if os.name == 'nt' and path.drive and not path.is_absolute():
                raise ValueError('drive_relative_pdf_path')
            candidates.append(Path(os.path.abspath(directory / path)))
    return tuple(dict.fromkeys(candidates))


def _legacy_hebrew_names(path):
    """Reverse known single-byte readings of a UTF-8 Hebrew basename."""
    for codec in ('latin1', 'cp1252', 'cp1255'):
        try:
            name = path.name.encode(codec).decode('utf-8')
        except UnicodeError:
            continue
        if (name != path.name and re.search(r'[\u0590-\u05ff]', name)
                and '/' not in name and '\\' not in name):
            yield path.with_name(name)


def _map_pdf(target, source, copies, roots, overrides, context):
    try:
        paths = _local_candidates(target, source.parent)
    except (ValueError, UnicodeError) as exc:
        raise LinkMappingError('invalid_local_pdf_address', context, target) from exc
    if not paths:
        return target
    destinations = {copies[path] for path in paths if path in copies}
    if len(destinations) == 1:
        return str(destinations.pop())
    if len(destinations) > 1:
        raise LinkMappingError('ambiguous_local_pdf_address', context, target)
    key = hashlib.sha256(target.encode('utf-8')).hexdigest()
    if (context, key) in overrides:
        return str(overrides[(context, key)])
    repaired = {copies[candidate] for path in paths for candidate in _legacy_hebrew_names(path)
                if candidate in copies}
    if len(repaired) == 1:
        return str(repaired.pop())
    if len(repaired) > 1:
        raise LinkMappingError('ambiguous_legacy_filename_encoding', context, target)
    reason = ('receipt_path_missing_from_snapshot' if any(path.is_relative_to(root)
              for root in roots for path in paths) else 'pdf_outside_mapped_receipt_roots')
    raise LinkMappingError(reason, context, target)


def _pdf_links(raw, source, copies, roots, overrides, report_label):
    """Rewrite literal PDF links while preserving workbook cells and caches."""
    output = BytesIO()
    changed = False
    with ZipFile(BytesIO(raw)) as original, ZipFile(output, 'w') as revised:
        for info in original.infolist():
            data = original.read(info)
            if info.filename.endswith('.rels'):
                xml = ET.fromstring(data)
                updated = False
                for rel in xml.iter(_REL):
                    if not rel.get('Type', '').endswith('/hyperlink'):
                        continue
                    url = rel.get('Target', '')
                    context = f'{report_label}|{info.filename}|{rel.get("Id", "?")}'
                    target = _map_pdf(url, source, copies, roots, overrides, context)
                    if target != url:
                        rel.set('Target', target)
                        updated = True
                if updated:
                    data = ET.tostring(xml, encoding='utf-8', xml_declaration=True)
                    changed = True
            elif re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', info.filename):
                def rewrite_formula(element):
                    text = unescape(element[2], {'&quot;': '"', '&apos;': "'"})
                    match = _HYPERLINK.match(text)
                    if not match:
                        if 'hyperlink' in text.lower() and '.pdf' in text.lower():
                            raise LinkMappingError('dynamic_pdf_formula', f'{report_label}|{info.filename}', text)
                        return element[0]
                    url = match[2].replace('""', '"')
                    # The XML offset distinguishes equal formula targets in different cells.
                    context = f'{report_label}|{info.filename}|formula@{element.start()}'
                    target = _map_pdf(url, source, copies, roots, overrides, context)
                    if target == url:
                        return element[0]
                    replacement = text[:match.start(2)] + target.replace('"', '""') + text[match.end(2):]
                    return element[1] + escape(replacement) + element[3]

                revised_data = _FORMULA_ELEMENT.sub(rewrite_formula, data.decode('utf-8')).encode('utf-8')
                if revised_data != data:
                    changed = True
                    data = revised_data
            revised.writestr(info, data)
    return output.getvalue() if changed else raw


def migrate(source, legacy_runtime, destination, user, account, address, *, apply=False, legacy_link_root=None, link_map=None):
    source = safe_path(source)
    legacy_runtime = safe_path(legacy_runtime)
    scope = UserScope.open(user, destination)
    account, address = identifier(account), mailbox(address)
    if source.is_relative_to(scope.project) or scope.project.is_relative_to(source):
        raise ValueError('Use a separate destination project')
    if not safe_path(source / 'accounts' / account).is_dir():
        raise ValueError('Legacy account directory missing')
    # Use a destination-local private runtime so the copy is self-contained.
    if scope.runtime != scope.project / 'runtime' / 'users' / scope.user:
        raise ValueError('Unset the external private runtime override during migration')
    roots = {
        source / 'accounts' / account: scope.account(account),
        source / 'receipts' / account: scope.receipts / account,
        source / 'receipts' / 'manual': scope.receipts / 'manual',
        source / 'reports' / account: scope.reports / account,
        source / 'reports' / 'manual': scope.reports / 'manual',
        legacy_runtime: scope.runtime,
    }
    if scope.project.exists():
        raise FileExistsError('Use a new destination project path')
    copies = {path: new / path.relative_to(old)
              for old, new in roots.items() for path in _files(old)}
    manifest = safe_path(source / 'data' / 'processed_messages.json')
    if manifest.is_file():
        copies[manifest] = scope.manifest

    def report_target(old, new):
        if old.name.startswith('potential_receipts_') and new.is_relative_to(scope.reports / account):
            return scope.reports / 'manual' / new.parent.name / new.name
        return new

    copies = {old: report_target(old, new) for old, new in copies.items()}
    # Alias original roots lexically. All file reads use the copied inventory.
    if isinstance(legacy_link_root, (str, os.PathLike)):
        legacy_link_root = (legacy_link_root,)
    aliases = [source]
    for value in legacy_link_root or ():
        alias = Path(value)
        if not alias.is_absolute() or '..' in alias.parts:
            raise ValueError('Original legacy link roots must be absolute paths')
        aliases.append(alias)
    receipt_roots = [root / 'receipts' / folder for root in aliases for folder in (account, 'manual')]
    receipt_files = {old: new for old, new in copies.items()
                     if old.suffix.casefold() == '.pdf' and any(
                         old.is_relative_to(root) for root in receipt_roots[:2])}
    link_paths = {}
    manifest_paths = {}
    for alias in aliases:
        for old, new in receipt_files.items():
            key = alias / old.relative_to(source)
            if key in link_paths and link_paths[key] != new:
                raise ValueError('Conflicting legacy receipt root aliases')
            link_paths[key] = new
        for old, new in copies.items():
            if old.is_relative_to(source):
                key = alias / old.relative_to(source)
                if key in manifest_paths and manifest_paths[key] != new:
                    raise ValueError('Conflicting legacy manifest root aliases')
                manifest_paths[key] = new
    if len(set(copies.values())) != len(copies):
        raise ValueError('Legacy paths map to the same destination')
    overrides = {}
    if link_map is not None:
        rows = json.loads(safe_path(link_map).read_text(encoding='utf-8-sig'))
        for row in rows:
            key = (row['location'], row['target_sha256'])
            relative = Path(row['snapshot_receipt'])
            if not re.fullmatch('[0-9a-f]{64}', key[1]) or relative.anchor or '..' in relative.parts:
                raise ValueError('Invalid verified link mapping')
            receipt = safe_path(source / relative)
            if receipt not in receipt_files or hashlib.sha256(receipt.read_bytes()).hexdigest() != row['receipt_sha256']:
                raise ValueError('Verified link mapping does not match copied receipt bytes')
            if key in overrides:
                raise ValueError('Duplicate verified link mapping')
            overrides[key] = receipt_files[receipt]

    def rewritten_manifest(raw):
        entries = json.loads(raw)
        for entry in entries:
            for index, value in enumerate(entry['saved_files']):
                path = Path(os.path.abspath(source / value))
                if path not in manifest_paths:
                    raise LinkMappingError('message_manifest_source_missing',
                                           'data/processed_messages.json', value)
                entry['saved_files'][index] = str(manifest_paths[path])
        return json.dumps(entries, ensure_ascii=False).encode()

    def report_bytes(old, raw):
        label = old.relative_to(source).as_posix()
        return _pdf_links(raw, old, link_paths, receipt_roots, overrides, label)

    if not apply:
        for old in copies:
            if old.is_relative_to(source / 'reports') and old.suffix.lower() == '.xlsx':
                report_bytes(old, old.read_bytes())
            elif old == manifest:
                rewritten_manifest(old.read_bytes())
        return len(copies)

    scope.project.parent.mkdir(parents=True, exist_ok=True)
    lock = safe_path(scope.project.parent / f'.{scope.project.name}.migration.lock')
    with file_lock(lock):
        if scope.project.exists():
            raise FileExistsError('Use a new destination project path')
        with TemporaryDirectory(prefix='.receipt-migration-', dir=scope.project.parent) as temporary:
            stage = UserScope.open(user, Path(temporary) / 'data')
            for old, new in copies.items():
                raw = old.read_bytes()
                if old.is_relative_to(source / 'reports') and old.suffix.lower() == '.xlsx':
                    raw = report_bytes(old, raw)
                if old == manifest:
                    raw = rewritten_manifest(raw)
                if old == legacy_runtime / f'sync-{account}.json':
                    # Full intermediate bases bind old source paths and canonical bytes.
                    # The original catalog and all approved decisions remain available.
                    state = json.loads(raw)
                    new_bytes(stage.runtime / 'migration-backup' / old.name, raw)
                    if 'full_processing' in state:
                        state.pop('full_processing')
                        raw = json.dumps(state, ensure_ascii=False).encode()
                path = stage.project / new.relative_to(scope.project)
                new_bytes(path, raw)
                if path.read_bytes() != raw:
                    raise OSError('Migration copy verification failed')
            stage.claim_mailbox(account, address)
            new_bytes(stage.account(account) / 'mailbox.txt', (address + '\n').encode())
            new_bytes(stage.account(account) / '.legacy-identity', b'1\n')
            stage.create()
            os.rename(stage.project, scope.project)

    return len(copies)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source', 'legacy-runtime', 'destination', 'user', 'account', 'mailbox'):
        parser.add_argument(f'--{flag}', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--legacy-link-root', type=Path, action='append')
    parser.add_argument('--link-map', type=Path)
    parser.add_argument('--show-link', action='store_true',
                        help='Print the first failing target locally; it can contain private paths')
    args = parser.parse_args()
    try:
        count = migrate(args.source, args.legacy_runtime, args.destination,
                        args.user, args.account, args.mailbox, apply=args.apply,
                        legacy_link_root=args.legacy_link_root, link_map=args.link_map)
    except LinkMappingError as exc:
        print(f'Migration stopped: {exc}', file=sys.stderr)
        if args.show_link:
            print(f'Local link target: {exc.target!r}', file=sys.stderr)
        return 1
    print(f'{count} files {"copied" if args.apply else "planned"}')


if __name__ == '__main__':
    raise SystemExit(main())
