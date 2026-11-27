"""Offline maintenance of an existing workbook; never regenerate receipt data."""
import argparse
from copy import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import openpyxl
from openpyxl.utils import quote_sheetname
from openpyxl.styles import Font

PRIMARY = (
    ('תאריך', ('תאריך',)), ('עמותה', ('שם עמותה',)),
    ('סכום', ('סכום',)), ('שם תורם', ('שם תורם שזוהה',)),
    ('סעיף 46', ('סעיף 46 בתאריך התרומה', 'Section 46 on donation date (cached)')),
    ('מצב דיווח לרשות המסים', ('מצב דיווח לרשות המסים',)),
    ('סטטוס קבלה', ('חומרה', 'סטטוס')), ('קישור PDF', ('שם קובץ',)),
)
SECONDARY = (
    ('מספר עמותה', ('מספר עמותה',)), ('מספר קבלה', ('מספר קבלה',)),
    ('אישור דיווח', ('מספר אישור דיווח',)),
    ('התאמת זהות', ('התאמת תורם',)), ('הערות', ('הערות',)),
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize(target):
    # Decode URI escaping, not '+' (a valid filename character).
    return unquote(target).replace('\\', '/')


def resolve_encoding_target(target, workbook_dir, *, display_name=None, permitted_root=None):
    """Repair only the basename, in the same directory, by reversible decoding."""
    def allowed(path):
        if permitted_root is None:
            return True
        from os.path import normpath
        from src.private_runtime import safe_path
        try:
            candidate = safe_path(Path(normpath(path)))
        except ValueError:
            return False
        return candidate.is_relative_to(permitted_root)

    if target.lower().startswith('file:'):
        uri = urlsplit(target.replace('\\', '/'))
        if uri.query or uri.fragment:
            return None, 'uri_query_or_fragment'
        value = (('//' + uri.netloc if uri.netloc else '') + url2pathname(uri.path)).replace('\\', '/')
        if __import__('os').name == 'nt' and len(value) > 3 and value[0] == '/' and value[2] == ':':
            value = value[1:]
    elif '://' in target:
        return None, 'not_local_file'
    else:
        value = target.replace('\\', '/')
        # Prefer a literal native path when it exists (percent can be literal).
        literal = workbook_dir / value
        if not allowed(literal):
            return None, 'source_outside_account'
        if not literal.is_file():
            value = unquote(value, errors='strict')
    path = Path(value)
    if not path.is_absolute():
        path = workbook_dir / path
    if not allowed(path):
        return None, 'source_outside_account'
    names = {path.name}
    repaired_names=set()
    # A caption can retain the original Hebrew basename even when the URI has
    # lost UTF-8 bytes. Restrict it to this exact directory; never glob or guess.
    if (isinstance(display_name,str) and '/' not in display_name and '\\' not in display_name
            and display_name not in ('.','..') and Path(display_name).suffix.lower()=='.pdf'):
        names.add(display_name)

    for codec in ('latin1', 'cp1252', 'cp1255'):
        try:
            repaired = path.name.encode(codec).decode('utf-8')
            if (repaired != path.name and any('\u0590' <= ch <= '\u05ff' for ch in repaired)
                    and '/' not in repaired and '\\' not in repaired):
                names.add(repaired)
                repaired_names.add(repaired)
        except UnicodeError:
            pass
    candidates = {path.with_name(name).absolute() for name in names
                  if Path(name).suffix.lower() == '.pdf' and allowed(path.with_name(name)) and path.with_name(name).is_file()
                  and not path.with_name(name).is_symlink()}
    if len(candidates) > 1 and len({sha(p) for p in candidates}) == 1:
        return sorted(candidates, key=lambda p:(p.name not in repaired_names,p.name!=display_name,str(p)))[0], None
    if len(candidates) != 1:
        return None, 'ambiguous_existing_names' if candidates else 'pdf_not_found_in_same_directory'
    return candidates.pop(), None


def add_month_views(wb, *, compact=False, front=True, sheets=None):
    created = 0
    for ws in list(wb.worksheets) if sheets is None else sheets:
        headers = [c.value for c in ws[1]]
        if not all(headers.count(h) == 1 for h in ('תאריך', 'שם קובץ', 'סכום')):
            continue
        title = 'תצוגה ' + ws.title
        if len(title) > 31 or title in wb.sheetnames:
            raise ValueError('View name exists or is too long; no sheets replaced')
        view = wb.create_sheet(title, 0 if front else None)
        view.sheet_view.rightToLeft = True
        view.freeze_panes = 'A2'
        for col, (label, aliases) in enumerate(PRIMARY + SECONDARY, 1):
            view.cell(1, col, label).font = Font(bold=True, size=11)
            candidates = [headers.index(h) + 1 for h in aliases if h in headers]
            origin = candidates[0] if candidates else None
            if any(headers.count(h) > 1 for h in aliases):
                raise ValueError('Ambiguous headers')
            for row in range(2, ws.max_row + 1):
                cell = view.cell(row, col)
                if origin is None:
                    cell.value = 'לא ידוע' if col in (5, 6) else None
                    continue
                original = ws.cell(row, origin)
                cell._style = copy(original._style)
                font = copy(cell.font)
                font.sz = 11
                cell.font = font
                ref = f'{quote_sheetname(ws.title)}!{original.coordinate}'
                cell.value = f'=IF({ref}="","",{ref})'
                if original.hyperlink:
                    cell.hyperlink = copy(original.hyperlink)
                    cell.hyperlink.ref = cell.coordinate
            view.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 24
        if compact:
            for col in range(9, len(PRIMARY + SECONDARY) + 1):
                view.column_dimensions[openpyxl.utils.get_column_letter(col)].hidden = True
        view.auto_filter.ref = view.dimensions
        created += 1
    return created


def maintain(source, mappings, output=None, *, views=True, compact=False, exceptions=None):
    """Plan by default; output must be a new explicit path. Mappings are local evidence.

    Each entry: old_target, new_path, sha256. The digest must come from a
    previously verified source/manifest, not a guess based on a similar name.
    """
    source = Path(source)
    before = sha(source)
    if source.suffix.lower() != '.xlsx':
        raise ValueError('Only xlsx supported')
    if output is not None and Path(output).exists():
        raise FileExistsError(output)
    targets = {}
    for entry in mappings or ():
        old = normalize(entry['old_target'])
        dest = Path(entry['new_path']).resolve(strict=True)
        if dest.suffix.lower() != '.pdf' or sha(dest) != entry['sha256']:
            raise ValueError('PDF identity mismatch')
        value = (dest.as_uri(), dest.name)
        for key in (old, normalize(dest.as_uri())):
            if key in targets and targets[key] != value:
                raise ValueError('Ambiguous mapping')
            targets[key] = value
    wb = openpyxl.load_workbook(source, data_only=False)
    counts = dict(links=0, names=0, unresolved=0, views=0)
    if mappings is None:
        counts['repaired_cells'] = 0
    try:
        for ws in list(wb.worksheets):
            headers = [c.value for c in ws[1]]
            name_col = (headers.index('שם קובץ') + 1 if 'שם קובץ' in headers else
                        3 if ws.title == 'קובצי מקור' else
                        headers.index('קישור PDF') + 1 if 'קישור PDF' in headers else None)
            if name_col is None:
                continue
            for row in range(2, ws.max_row + 1):
                cell = ws.cell(row, name_col)
                if not cell.hyperlink or not cell.hyperlink.target or cell.hyperlink.target.startswith('#'):
                    continue
                if mappings is None:
                    try:
                        path, reason = resolve_encoding_target(cell.hyperlink.target, source.parent.resolve())
                    except (OSError, ValueError, UnicodeError):
                        path, reason = None, 'invalid_or_unreadable_path'
                    if path is None:
                        counts['unresolved'] += 1
                        if exceptions is not None:
                            exceptions.append({'sheet': ws.title, 'cell': cell.coordinate,
                                               'target': cell.hyperlink.target, 'reason': reason})
                        continue
                    uri, name = path.as_uri(), path.name
                else:
                    target = targets.get(normalize(cell.hyperlink.target))
                    if target is None:
                        counts['unresolved'] += 1
                        continue
                    uri, name = target
                if mappings is None and (cell.hyperlink.target != uri or (cell.data_type != 'f' and cell.value != name)):
                    counts['repaired_cells'] += 1
                if cell.hyperlink.target != uri:
                    cell.hyperlink = uri
                    counts['links'] += 1
                if cell.data_type != 'f' and cell.value != name:
                    cell.value = name
                    cell.data_type = 's'
                    counts['names'] += 1
        if views:
            counts['views'] = add_month_views(wb, compact=compact)
        if compact and counts['views']:
            wb.active = 0
        if sha(source) != before:
            raise ValueError('Source changed during planning')
        if output is not None:
            # Exclusive create: never replace a canonical or existing file.
            with Path(output).open('xb') as handle:
                wb.save(handle)
        return counts
    finally:
        wb.close()


def donor_visibility(source):
    """Read-only aggregate check: stored donor names versus view/cache display."""
    wb = openpyxl.load_workbook(source, data_only=False)
    cached = openpyxl.load_workbook(source, data_only=True)
    counts = dict(donations=0, stored_names=0, blank_source_names=0,
                  names_only_in_view=0, view_formulas_without_cached_value=0)
    try:
        for ws in wb:
            headers = [c.value for c in ws[1]]
            if not all(headers.count(h) == 1 for h in ('שם קובץ', 'תאריך', 'סכום')):
                continue
            donor = next((headers.index(h) + 1 for h in ('שם התורם', 'שם תורם שזוהה', 'שם תורם') if headers.count(h) == 1), None)
            view_title = 'תצוגה ' + ws.title
            view = wb[view_title] if view_title in wb else None
            for row in range(2, ws.max_row + 1):
                if not ws.cell(row, headers.index('שם קובץ') + 1).value:
                    continue
                counts['donations'] += 1
                name = ws.cell(row, donor).value if donor else None
                counts['stored_names' if name else 'blank_source_names'] += 1
                if view is not None:
                    shown = view.cell(row, 4)
                    if shown.data_type == 'f':
                        counts['view_formulas_without_cached_value'] += bool(name and cached[view_title].cell(row, 4).value in (None, ''))
                    elif shown.value and not name:
                        counts['names_only_in_view'] += 1
        return counts
    finally:
        wb.close()
        cached.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--mapping', type=Path)
    mode.add_argument('--repair-encoding', action='store_true')
    parser.add_argument('--exceptions', type=Path)
    parser.add_argument('--compact', action='store_true')
    parser.add_argument('--output', type=Path, help='Explicit NEW review copy; omission plans only')
    parser.add_argument('--links-only', action='store_true')
    args = parser.parse_args(argv)
    exceptions = []
    mappings = None if args.repair_encoding else json.loads(args.mapping.read_text(encoding='utf-8'))
    counts = maintain(args.input, mappings, args.output, views=not args.links_only,
                      compact=args.compact, exceptions=exceptions)
    if args.exceptions:
        with args.exceptions.open('x', encoding='utf-8') as handle:
            json.dump(exceptions, handle, ensure_ascii=False, indent=2)
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
