"""Local, read-only inspection. Run from the installed product directory."""
import argparse
import csv
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path

from src.config import ACCOUNTS_DIR, RECEIPTS_DIR, REPORTS_DIR
from src.account_config import load_account_config
from src.private_runtime import registry_root, safe_path
from src.receipt_deduplication import _identity
from src.receipt_metadata_extractor import IDENTIFIER_REVIEW_NOTES
from src.receipt_summary import _extract_row
from src.report_layout import _pdf_audit, _report_field_columns, _resolve_pdf_link, source_link_input
from src.sync_state import SyncError, document_paths, donation_date, effective_metadata
from src.sync_workbook import (
    ARCHIVE, ID_HEADER, INDEX, _original_copy_pairs, donation_sheets, headers,
    load, metadata, receipt_source_evidence, reviewed_oc_pair,
)
from src.workbook_maintenance import resolve_encoding_target


def read_locators(path):
    """Use only the CSV's file and cell columns, never suggested or prior values."""
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'\u05e7\u05d5\u05d1\u05e5', '\u05ea\u05d0'} <= set(reader.fieldnames or ()):
            raise ValueError('Locator CSV must contain the file and cell columns')
        for row in reader:
            name = (row.get('\u05e7\u05d5\u05d1\u05e5') or '').strip()
            cell = (row.get('\u05ea\u05d0') or '').strip()
            if not name or '/' in name or '\\' in name or Path(name).suffix.lower() != '.pdf':
                raise ValueError('Locator must be a PDF basename')
            yield cell, name


def inspect(args, wb, workbook, state):
    if INDEX not in wb:
        raise ValueError('The workbook has no publication index')
    entries = {key: json.loads(raw) for key, raw in wb[INDEX].iter_rows(
        min_row=2, max_col=2, values_only=True) if key and raw}
    rows = {}
    for sheet in donation_sheets(wb):
        cols = headers(sheet)
        if ID_HEADER not in cols:
            continue
        for r in range(2, sheet.max_row + 1):
            key = sheet.cell(r, cols[ID_HEADER]).value
            if key in entries and not (ARCHIVE in cols and sheet.cell(r, cols[ARCHIVE]).value):
                if key in rows:
                    raise ValueError('Publication ID is ambiguous')
                rows[key] = sheet, r

    docs = set(args.sha)
    locators = [(None, name) for name in args.pdf]
    if args.locator_csv:
        locators.extend(read_locators(args.locator_csv))
    for cell_address, name in locators:
        matches = {sha for sha, entry in state['documents'].items()
                   if any(Path(path).name.casefold() == name.casefold() for path in entry.get('paths', []))}
        print(json.dumps({'requested_pdf': name, 'locator_cell': cell_address,
                          'matching_catalog_sha': sorted(matches),
                          'result': 'located' if len(matches) == 1 else
                          'ambiguous' if matches else 'not_in_catalog'}, ensure_ascii=True))
        if len(matches) == 1:
            docs.update(matches)
    for sheet, address in args.cell:
        cell = wb[sheet][address]
        if cell.column != _report_field_columns(wb[sheet]).get('amount'):
            raise ValueError('Requested cell is not the canonical amount column')
        key = wb[sheet].cell(cell.row, headers(wb[sheet])[ID_HEADER]).value
        if key not in rows:
            raise ValueError('Requested cell is not an active publication')
        docs.update(entries[key]['documents'])
    if args.oc_ids:
        for key in args.oc_ids:
            if key in rows:
                docs.update(entries[key]['documents'])
    config = load_account_config(ACCOUNTS_DIR, args.account)
    owners = set()
    for sha in sorted(docs):
        item = state['documents'].get(sha)
        if item is None:
            print(json.dumps({'sha': sha, 'result': 'not_in_catalog'}, ensure_ascii=True))
            continue
        paths = document_paths(RECEIPTS_DIR, args.account, state, (sha,))
        keys = [key for key in rows if sha in entries[key]['documents']]
        owners.update(keys)
        record = {'sha': sha, 'owners': keys, 'source_verified': bool(paths),
                  'refresh_version': item.get('identifier_refresh', {}).get('version'),
                  'review_action': item.get('review_decision', {}).get('action')}
        if paths:
            cached = effective_metadata(item)
            source = paths[0]
            base = safe_path(RECEIPTS_DIR / args.account)
            if not source.is_relative_to(base):
                raise ValueError('Source is outside the receipt account')
            storage_year = source.relative_to(base).parts[0]
            if len(storage_year) != 4 or not storage_year.isascii() or not storage_year.isdigit():
                raise ValueError('Source is outside an annual receipt directory')
            fresh = _extract_row(source, args.account, config, int(storage_year))
            proof = receipt_source_evidence(source, cached)
            fields = ('receipt_number', 'tax_report_number', 'amount')
            record.update(catalog={field: getattr(cached, field) for field in fields},
                          extracted={field: getattr(fresh, field) for field in fields},
                          ambiguous_fields=[field for field, note in IDENTIFIER_REVIEW_NOTES.items()
                                            if note in fresh.notes],
                          unreadable='לא ניתן לקרוא את הקובץ' in fresh.notes,
                          source_receipt_numbers=proof['receipt_numbers'], source_amounts=proof['amounts'],
                          row_identity_matches_catalog={}, canonical={})
            for key in keys:
                sheet, row = rows[key]
                actual = metadata(sheet, row)
                record['canonical'][key] = {field: getattr(actual, field) for field in fields}
                record['row_identity_matches_catalog'][key] = dict(zip(
                    ('organization', 'receipt', 'date', 'amount'),
                    (left is not None and left == right for left, right in zip(_identity(actual), _identity(cached)))))
        print(json.dumps(record, ensure_ascii=True, sort_keys=True))
    if args.pdf and len(owners) >= 2:
        pairs = _original_copy_pairs(rows, entries, state, Path(RECEIPTS_DIR) / args.account)
        print(json.dumps({'eligible_oc_pairs_in_selection': {key: pairs[key] for key in owners if key in pairs}},
                         ensure_ascii=True))

    if args.oc_ids:
        original, copied = args.oc_ids
        try:
            reviewed_oc_pair(rows, entries, state, safe_path(RECEIPTS_DIR / args.account), original, copied)
        except SyncError as exc:
            result = exc.code
        else:
            result = 'eligible_for_explicit_review'
        print(json.dumps({'oc_review_result': result, 'original': original, 'copy': copied}))

    if args.links_month:
        totals = Counter()
        source_owners = defaultdict(set)
        for key in rows:
            for sha in entries[key]['documents']:
                source_owners[sha].add(key)
        for location, (_, audit) in _pdf_audit(wb).items():
            owner = audit.get('owner')
            if not audit.get('pending') or owner not in rows:
                continue
            sheet, row = rows[owner]
            date = donation_date(metadata(sheet, row))
            if not date or date.month != args.links_month:
                continue
            title, addr = location.rsplit('!', 1)
            cell = wb[title][addr]
            name, target = source_link_input(wb, cell)
            try:
                candidate, reason = (resolve_encoding_target(target, workbook.parent, display_name=name)
                                     if target else (None, 'no_target'))
            except (ValueError, OSError):
                candidate, reason = None, 'unreadable_target'
            reason = reason or 'local_file_exists'
            paths = document_paths(RECEIPTS_DIR, args.account, state, entries[owner]['documents'])
            verified, status, resolver_reason, *_ = _resolve_pdf_link(
                cell, paths, state, RECEIPTS_DIR, args.account, workbook, config, source_owners, owner)
            totals[(audit['decision'], reason)] += 1
            print(json.dumps({'publication_id': owner, 'location': location,
                              'audit_reason': audit['decision'], 'local_result': reason,
                              'local_target_exists': candidate is not None,
                              'resolver_status': status, 'resolver_reason': resolver_reason,
                              'verified_source': verified is not None,
                              'prior_manual_comment': bool(audit.get('previous_comment'))}, ensure_ascii=True))
        print(json.dumps({'pending_owned_links_by_reason': [
            {'audit_reason': a, 'local_result': b, 'count': n} for (a,b), n in sorted(totals.items())
        ]}, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', default='primary', choices=('primary', 'secondary'))
    parser.add_argument('--year', type=int, default=2026)
    parser.add_argument('--sha', action='append', default=[])
    parser.add_argument('--pdf', action='append', default=[])
    parser.add_argument('--cell', nargs=2, action='append', default=[], metavar=('SHEET', 'ADDRESS'))
    parser.add_argument('--locator-csv', type=Path)
    parser.add_argument('--oc-ids', nargs=2, metavar=('ORIGINAL_ID', 'COPY_ID'))
    parser.add_argument('--links-month', type=int, choices=range(1, 13))
    args = parser.parse_args()
    if not (args.sha or args.pdf or args.cell or args.links_month or args.locator_csv or args.oc_ids):
        parser.error('Choose a document, amount cell or month')
    root = registry_root()
    state = json.loads((root / f'sync-{args.account}.json').read_bytes())
    workbook = REPORTS_DIR / args.account / str(args.year) / f'donation_summary_{args.year}.xlsx'
    with closing(load(workbook)) as wb:
        inspect(args, wb, workbook.absolute(), state)


if __name__ == '__main__':
    main()
