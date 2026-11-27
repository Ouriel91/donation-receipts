"""Read-only Windows check for a copied QA3 acceptance project."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from zipfile import ZipFile
from xml.etree import ElementTree

from src.user_scope import UserScope

HYPERLINK = re.compile(r'\bHYPERLINK\s*\(\s*"((?:[^"]|"")*)"\s*[,;]', re.I)
DRIVE = re.compile(r'^[A-Za-z]:[\\/]')


def target_path(value, parent):
    if DRIVE.match(value):
        return Path(unquote(value))
    url = urlsplit(value)
    if url.scheme == 'file' and not url.netloc:
        return Path(url2pathname(url.path))
    if url.scheme or url.netloc or value.startswith(('\\', '/')):
        raise ValueError('external path')
    return parent / unquote(value)


def json_strings(value):
    if isinstance(value, str):
        yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from json_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from json_strings(v)


def check(root, user):
    scope = UserScope.open(user, root)
    scope.require()
    scope.registry.read_bytes()  # Verify the copied registry is present.
    accounts = [p.name for p in scope.accounts.iterdir() if p.is_dir() and not p.name.startswith('.')]
    if not accounts:
        raise ValueError('no configured accounts')
    for account in accounts:
        scope.require_account(account)
    for required in (scope.receipts, scope.reports, scope.runtime, scope.registry):
        if not required.resolve(strict=True).is_relative_to(root):
            raise ValueError('state outside acceptance tree')
    blockers = set()
    quotes = lambda value: value.replace('""', '"')
    for book in scope.reports.rglob('*.xlsx'):
        if book.name.startswith('~$'):
            blockers.add('Excel owner file')
            continue
        account = book.relative_to(scope.reports).parts[0]
        permissible = scope.receipts if account == 'combined' else scope.receipts / account
        targets = []
        with ZipFile(book) as archive:
            for member in archive.namelist():
                if member.startswith(('xl/externalLinks/', 'xl/connections')):
                    blockers.add('external Excel data connection')
                if member.endswith('.rels'):
                    for relation in ElementTree.fromstring(archive.read(member)):
                        if relation.get('TargetMode') == 'External':
                            targets.append(relation.get('Target', ''))
                elif member.startswith('xl/worksheets/') and member.endswith('.xml'):
                    for formula in ElementTree.fromstring(archive.read(member)).iter():
                        if not formula.tag.endswith('}f') or not formula.text:
                            continue
                        literal = HYPERLINK.findall(formula.text)
                        if len(literal) != len(re.findall(r'\bHYPERLINK\s*\(', formula.text, re.I)):
                            blockers.add('dynamic hyperlink formula')
                        targets.extend(map(quotes, literal))
        for target in targets:
            if target.startswith('#') or re.match(r'^https://mail\.google\.com/', target, re.I):
                continue
            try:
                path = target_path(target, book.parent).resolve(strict=True)
                if path.suffix.lower() != '.pdf' or not path.is_file() or not path.is_relative_to(permissible):
                    blockers.add('workbook link outside copied account or missing PDF')
            except (OSError, ValueError):
                blockers.add('workbook link outside copied account or missing PDF')
    for state in scope.runtime.rglob('*.json'):
        for value in json_strings(json.loads(state.read_bytes())):
            if DRIVE.match(value) or value.lower().startswith('file:') or value.startswith('\\\\'):
                try:
                    if not target_path(value, root).resolve().is_relative_to(root):
                        blockers.add('copied runtime contains absolute reference outside copy')
                except (OSError, ValueError):
                    blockers.add('copied runtime contains absolute reference outside copy')
    for reason in sorted(blockers):
        print('STOP: ' + reason)
    return not blockers


if __name__ == '__main__':
    try:
        verified = os.name == 'nt' and check(Path.cwd().resolve(strict=True), sys.argv[1])
    except Exception:
        print('STOP: copied state could not be verified')
        verified = False
    if verified:
        print('PASS: local account bindings, runtime and workbook paths verified')
    sys.exit(0 if verified else 1)
