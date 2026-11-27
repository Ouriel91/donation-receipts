"""Read-only evidence from the installed Windows PDF pipeline. No staging or publishing."""
import argparse
from contextlib import closing
import csv
import json
from pathlib import Path
import re

from src.account_config import load_account_config
from src.config import ACCOUNTS_DIR, RECEIPTS_DIR, REPORTS_DIR
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.pdf_text_extractor import extract_text_from_pdf, extract_text_from_pdf_pymupdf
from src.private_runtime import registry_root, safe_path
from src.receipt_metadata_extractor import extract_metadata, receipt_numbers_in_text, receipt_amounts_in_text
from src.receipt_summary import _extract_row
from src.report_layout import _report_field_columns
from src.sync_state import SyncError, digest, document_paths
from src.sync_workbook import ARCHIVE, ID_HEADER, INDEX, donation_sheets, headers, load, metadata, reviewed_oc_pair

# Retain known label words in both orders; redact names/free text and replace
# numbers consistently within each source, including across reader outputs.
LABELS = set('\u05e7\u05d1\u05dc\u05d4 \u05e7\u05d1\u05dc\u05ea \u05e2\u05dc \u05e2\u05d1\u05d5\u05e8 \u05ea\u05e8\u05d5\u05de\u05d4 \u05d4\u05ea\u05e8\u05d5\u05de\u05d4 \u05d0\u05d9\u05e9\u05d5\u05e8 \u05d3\u05d9\u05d5\u05d5\u05d7 \u05e8\u05e9\u05d5\u05ea \u05dc\u05e8\u05e9\u05d5\u05ea \u05d4\u05de\u05e1\u05d9\u05dd \u05d4\u05de\u05d9\u05e1\u05d9\u05dd \u05de\u05e1\u05e4\u05e8 \u05de\u05e1 \u05e1\u05db\u05d5\u05dd \u05e1\u05da \u05db\u05d5\u05dc\u05dc \u05e1\u05d4\u05db \u05dc\u05ea\u05e9\u05dc\u05d5\u05dd \u05d1\u05e9\u05d7 \u05e9\u05e7\u05dc \u05ea\u05d0\u05e8\u05d9\u05da \u05de\u05e7\u05d5\u05e8 \u05d4\u05e2\u05ea\u05e7'.split())
LABELS |= {word[::-1] for word in LABELS}
WORDS = re.compile(r'[^\W\d_]+|\d+')
ANCHOR = re.compile(r'\u05e7\u05d1\u05dc|\u05dc\u05d1\u05e7|\u05d3\u05d9\u05d5\u05d5\u05d7|\u05d7\u05d5\u05d5\u05d9\u05d3|\u05d0\u05d9\u05e9\u05d5\u05e8|\u05e8\u05d5\u05e9\u05d9\u05d0|\u05de\u05e1\u05d9\u05dd|\u05dd\u05d9\u05e1\u05de|\u05e1\u05db\u05d5\u05dd|\u05dd\u05d5\u05db\u05e1|\u20aa|'
                    r'\u05ea\u05e8\u05d5\u05de|\u05de\u05d5\u05e8[\u05ea]|receipt|report|total', re.IGNORECASE)
NUMBER_LINE = re.compile(r'\s*[:\uff1a#\u20aa() .,+/\-]*\d[\d :\uff1a#\u20aa() .,+/\-]*\Z')
FIELDS = ('receipt_number', 'tax_report_number', 'amount')


class Redaction:
    def __init__(self, raw):
        self.raw = raw
        self.numbers = {}

    def text(self, value):
        if self.raw:
            return value

        def replace(match):
            text = match.group()
            if text.isdecimal():
                key = self.numbers.setdefault(text, len(self.numbers) + 1)
                return f'<n{key}:{len(text)}d>'
            return text if text in LABELS or text.lower() in {'receipt', 'report', 'total'} else '<word>'

        return WORDS.sub(replace, value)

    def fields(self, meta):
        return {field: self.text(str(getattr(meta, field) or '')) for field in FIELDS}


def excerpts(text, redact, hints, limit):
    lines = text.splitlines()
    positions = set()
    for i, line in enumerate(lines):
        if ANCHOR.search(line) or any(re.search(rf'(?<!\d){re.escape(hint)}(?!\d)', line)
                                      for hint in hints if hint and hint.isdigit()):
            positions.update(range(max(0, i-2), min(len(lines), i+3)))
    if not positions:
        positions.update(range(min(8, len(lines))))
    indices = sorted(positions)
    return {'line_count': len(lines), 'omitted_matching_lines': max(0, len(indices)-limit),
            'lines': [{'line': i+1, 'text': redact.text(lines[i])} for i in indices[:limit]]}


def page_layout(path, redact, hints, limit):
    try:
        import fitz
        evidence = []
        with fitz.open(str(path)) as pdf:
            for page in pdf:
                for block in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
                    lines = block.get('lines', [])
                    raw = [''.join(span['text'] for span in line['spans']) for line in lines]
                    window = excerpts('\n'.join(raw), redact, hints, limit)
                    if not any(ANCHOR.search(line) or NUMBER_LINE.fullmatch(line) or any(h and h.isdigit() and re.search(
                            rf'(?<!\d){re.escape(h)}(?!\d)', line) for h in hints) for line in raw):
                        continue
                    for item in window['lines']:
                        if len(evidence) >= limit:
                            return {'status': 'truncated', 'lines': evidence}
                        line = lines[item['line']-1]
                        evidence.append({'page': page.number+1, 'block': block.get('number'),
                                         'line_in_block': item['line'], 'direction': line['dir'],
                                         'spans': [{'x0': round(span['bbox'][0], 1),
                                                    'y0': round(span['bbox'][1], 1),
                                                    'text': redact.text(span['text'])} for span in line['spans']]})
        return {'status': 'ok', 'lines': evidence}
    except Exception as exc:
        return {'status': type(exc).__name__}


def inspect_source(path, sha, args, redaction, canonical, cached, config):
    hints = {getattr(meta, field) for meta in (canonical, cached) for field in FIELDS}
    readers = {}
    for key, reader in (('pypdf', extract_text_from_pdf), ('pymupdf', extract_text_from_pdf_pymupdf)):
        try:
            raw = reader(path)
        except Exception as exc:
            readers[key] = {'status': type(exc).__name__}
            continue
        if not raw:
            readers[key] = {'status': 'empty_or_reader_unavailable'}
            continue
        normal = normalize_hebrew_text(raw)
        extracted = extract_metadata(normal, path, raw)
        readers[key] = {'status': 'ok', 'raw': excerpts(raw, redaction, hints, args.max_lines),
                        'normalized': excerpts(normal, redaction, hints, args.max_lines),
                        'fields': redaction.fields(extracted),
                        'receipt_candidates_raw': sorted(redaction.text(v) for v in receipt_numbers_in_text(raw)),
                        'amount_candidates_raw': sorted(redaction.text(v) for v in receipt_amounts_in_text(raw))}
    storage = path.relative_to(safe_path(RECEIPTS_DIR / args.account)).parts[0]
    year = int(storage) if storage.isascii() and storage.isdigit() and len(storage) == 4 else args.year
    extracted = _extract_row(path, args.account, config, year)
    from src.receipt_metadata_extractor import IDENTIFIER_REVIEW_NOTES
    result = {'readers': readers, 'layout': page_layout(path, redaction, hints, args.max_lines),
              'product_fields': redaction.fields(extracted),
              'ambiguous_fields': [field for field, note in IDENTIFIER_REVIEW_NOTES.items() if note in extracted.notes],
              'product_unreadable': '\u05dc\u05d0 \u05e0\u05d9\u05ea\u05df \u05dc\u05e7\u05e8\u05d5\u05d0 \u05d0\u05ea \u05d4\u05e7\u05d5\u05d1\u05e5' in extracted.notes}
    if digest(path.read_bytes()) != sha:
        return {'error': 'source_changed_during_inspection'}
    return result


def run(args, workbook, state, wb):
    entries = {key: json.loads(raw) for key, raw in wb[INDEX].iter_rows(
        min_row=2, max_col=2, values_only=True) if key and raw}
    rows = {}
    for ws in donation_sheets(wb):
        cols = headers(ws)
        if ID_HEADER not in cols:
            continue
        for r in range(2, ws.max_row + 1):
            key = ws.cell(r, cols[ID_HEADER]).value
            if key in entries and not (ARCHIVE in cols and ws.cell(r, cols[ARCHIVE]).value):
                if key in rows:
                    raise ValueError('Duplicate active publication ID')
                rows[key] = ws, r

    selected = []
    if args.case:
        with args.csv.open(encoding='utf-8-sig', newline='') as stream:
            locators = {r['\u05ea\u05d0']: r['\u05e7\u05d5\u05d1\u05e5'] for r in csv.DictReader(stream)}
        for case in args.case:
            name = locators.get(case)
            if not name:
                print(json.dumps({'case': case, 'error': 'locator_not_found'}))
                continue
            matches = [sha for sha, item in state['documents'].items()
                       if any(Path(p).name.casefold() == name.casefold() for p in item.get('paths', []))]
            selected.append((case, matches, None, True))
    for sheet, address in args.amount_cell:
        ws = wb[sheet]
        cell = ws[address]
        if cell.column != _report_field_columns(ws).get('amount'):
            raise ValueError('Selected cell is not the canonical amount column')
        key = ws.cell(cell.row, headers(ws)[ID_HEADER]).value
        selected.append((f'{sheet}!{address}', entries.get(key, {}).get('documents', []), key, False))
    if args.oc:
        original, copied = args.oc
        try:
            reviewed_oc_pair(rows, entries, state, safe_path(RECEIPTS_DIR / args.account), original, copied)
        except SyncError as exc:
            outcome = exc.code
        else:
            outcome = 'eligible_for_explicit_review'
        print(json.dumps({'case': 'O/C', 'result': outcome}))
        for role, key in zip(('O', 'C'), args.oc):
            selected.append((role, entries.get(key, {}).get('documents', []), key, False))

    config = load_account_config(ACCOUNTS_DIR, args.account)
    for case, hashes, owner, unique in selected:
        if not hashes or (unique and len(hashes) != 1):
            print(json.dumps({'case': case, 'error': 'source_missing_or_ambiguous', 'matches': len(hashes)}))
            continue
        for index, sha in enumerate(hashes, 1):
            owners = [key for key in rows if sha in entries[key]['documents']]
            paths = document_paths(RECEIPTS_DIR, args.account, state, [sha])
            if len(owners) != 1 or (owner and owner not in owners) or not paths:
                print(json.dumps({'case': case, 'source': index, 'error': 'owner_or_pdf_not_verified'}))
                continue
            key = owners[0]
            canonical = metadata(*rows[key])
            item = state['documents'][sha]
            cached = type(canonical)(**item['metadata'])
            redact = Redaction(args.private_raw)
            rec = {'case': case, 'source': index, 'canonical': redact.fields(canonical),
                   'catalog': redact.fields(cached), 'refresh_version': item.get('identifier_refresh', {}).get('version'),
                   'pending_fields': sorted(item.get('identifier_refresh', {}).get('proposals', {})),
                   'approved_fields': sorted(item.get('verified_identifier_updates', {})),
                   'applied_fields': [field for field in FIELDS if f'{sha}:{field}' in entries[key].get('identifier_approvals', {})]}
            if args.private_raw:
                rec.update(sha=sha, publication_id=key, file_name=paths[0].name)
            rec.update(inspect_source(paths[0], sha, args, redact, canonical, cached, config))
            print(json.dumps(rec, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', default='primary', choices=('primary', 'secondary'))
    parser.add_argument('--year', type=int, default=2026)
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--amount-cell', nargs=2, action='append', default=[], metavar=('SHEET', 'CELL'))
    parser.add_argument('--oc', nargs=2, metavar=('O_PUBLICATION_ID', 'C_PUBLICATION_ID'))
    parser.add_argument('--max-lines', type=int, default=30, choices=range(5, 101))
    parser.add_argument('--private-raw', action='store_true', help='Local only: include unredacted label context and IDs')
    args = parser.parse_args()
    if (args.case and not args.csv) or not (args.case or args.amount_cell or args.oc):
        parser.error('Select a case with its CSV, an amount cell or an O/C pair.')
    root = registry_root()
    catalog = root / f'sync-{args.account}.json'
    workbook = REPORTS_DIR / args.account / str(args.year) / f'donation_summary_{args.year}.xlsx'
    before = catalog.read_bytes(), digest(workbook.read_bytes())
    with closing(load(workbook)) as wb:
        run(args, workbook, json.loads(before[0]), wb)
    if before != (catalog.read_bytes(), digest(workbook.read_bytes())):
        raise SystemExit('Workbook or catalog changed during inspection. Discard the output.')


if __name__ == '__main__':
    main()
