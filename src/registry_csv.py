"""Offline Ministry of Justice CSV parsing; never performs organization lookups.

Content-quarantine ``line`` is the one-based END physical line of the parsed
record, including quoted newlines. It is not the record's starting line.
For an audit requiring the start, verify the retained source's file_sha256 and
encoding, then replay csv.reader(strict=True) over text opened with newline=''.
After each yielded row, its end is reader.line_num; its start is the prior
completed row's (or header's) end plus one. Process EVERY row, including [] for
an empty physical row; do not skip rows or split quoted fields. With recovery, replay
each retained segment separately and add its physical-line offset. Opaque
malformed spans instead have explicit start/end lines and byte offsets in the
recovery audit; never infer record boundaries inside those spans.
"""
import csv
import hashlib
import io
import re
import unicodedata
from datetime import datetime, timezone

SOURCE = 'https://data.gov.il/dataset/moj-amutot/resource/be5b7935-3922-45d4-9638-08871b17ec95'
ID = 'מספר עמותה'
NAME = 'שם עמותה בעברית'

# A reviewed exception, not a heuristic for finding records inside free text.
APPROVED_RECOVERY = (
    '01ba0d773ee484e950f44be2094896fe6dd5fb3832414c150e9bcf35d21dc917',
    ((66380, 66382), (121896, 121896), (132796, 132798)), 23,
)


def _recovery_segments(raw, codec):
    digest, spans, columns = APPROVED_RECOVERY
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Recovery is authorized only for the reviewed CSV SHA256')
    starts = [0] + [match.end() for match in re.finditer(rb'\r\n|\r|\n', raw)]
    # Only the beginning of the file may contain a UTF-8 BOM.
    tail_codec = 'utf-8' if codec == 'utf-8-sig' else codec
    segments, recoveries = [], []
    position, offset = 0, 0
    if not spans:
        raise ValueError('Recovery requires approved spans')
    for first, last in spans:
        if (type(first) is not int or type(last) is not int
                or not (offset + 1 < first <= last < len(starts))
                or starts[last] >= len(raw)):
            raise ValueError('Invalid recovery range or missing resume record')
        begin, end = starts[first - 1], starts[last]
        payload = raw[begin:end]
        # Refuse to quarantine an otherwise well-formed span. Never repair quotes.
        try:
            list(csv.reader(io.StringIO(payload.decode(tail_codec), newline=''), strict=True))
        except csv.Error:
            pass
        else:
            raise ValueError('Authorized quarantine span is not malformed CSV')
        recoveries.append({'start_line': first, 'end_line': last, 'resume_line': last + 1,
                           'byte_start': begin, 'byte_end': end,
                           'sha256': hashlib.sha256(payload).hexdigest(),
                           'artifact': f'registry-quarantine-{digest}-{first}-{last}.bin',
                           'reason': 'malformed_csv', 'expected_columns': columns})
        segments.append((raw[position:begin].decode(codec if position == 0 else tail_codec), offset))
        position, offset = end, last
    segments.append((raw[position:].decode(tail_codec), offset))
    return segments, recoveries


def _parse_csv(raw, *, imported_at, encoding=None, recover_malformed=False):
    """Return a complete snapshot and quarantine; reject unknown file schemas."""
    if not isinstance(raw, bytes):
        raise TypeError('CSV must be bytes')
    if not isinstance(imported_at, datetime) or imported_at.utcoffset() is None:
        raise ValueError('Import date must be timezone aware')
    if encoding not in (None, 'utf-8-sig', 'cp1255'):
        raise ValueError('Unsupported encoding')
    candidates = (encoding,) if encoding else ('utf-8-sig', 'cp1255')
    for codec in candidates:
        try:
            text = raw.decode(codec)
        except UnicodeError:
            continue
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        headers = next(reader, [])
        if len(headers) == len(set(headers)) and ID in headers and NAME in headers:
            break
    else:
        raise ValueError('Invalid encoding or registry headers')
    recovery = None
    if recover_malformed:
        segments, recovery = _recovery_segments(raw, codec)
        if len(headers) != APPROVED_RECOVERY[2]:
            raise ValueError('Recovery header column count mismatch')
    else:
        segments = [(text, 0)]
    positions = headers.index(ID), headers.index(NAME)
    records, quarantine, seen, blocked = {}, [], {}, set()
    def rows():
        for index, (segment, offset) in enumerate(segments):
            reader = csv.reader(io.StringIO(segment, newline=''), strict=True)
            if index == 0:
                next(reader)  # The already validated header.
            elif segment.startswith(('\r', '\n')):
                raise ValueError('Empty line at authorized resume boundary')
            resumed = index > 0
            for row in reader:
                yield row, offset + reader.line_num, resumed
                resumed = False
            if resumed:
                raise ValueError('Missing resume record')
    for row, line, resumed in rows():
        number = row[positions[0]].strip() if len(row) > positions[0] else ''
        name = row[positions[1]].strip() if len(row) > positions[1] else ''
        reason = None
        if len(row) != len(headers):
            reason = 'column_count'
        elif len(number) != 9 or not number.isascii() or not number.isdigit():
            reason = 'invalid_id'
        elif (not name or not any('\u05d0' <= c <= '\u05ea' for c in name)
              or any(unicodedata.category(c).startswith('C') for c in name)):
            reason = 'invalid_name'
        if resumed and reason:
            raise ValueError('Invalid record at authorized resume boundary')
        valid_id = len(number) == 9 and number.isascii() and number.isdigit()
        entry = {'line': line, 'registration_number': number, 'official_name': name}
        if valid_id and number in seen:
            blocked.add(number)
            records.pop(number, None)
            first_entry = seen[number]
            if first_entry.get('reason') != 'duplicate_id':
                if 'reason' not in first_entry:
                    quarantine.append(first_entry)
                first_entry['reason'] = 'duplicate_id'
            reason = 'duplicate_id'
        elif valid_id:
            seen[number] = entry
        if reason:
            entry['reason'] = reason
            quarantine.append(entry)
        elif number not in blocked:
            records[number] = name
    if not records:
        raise ValueError('No valid registry records')
    snapshot = {'source': SOURCE, 'imported_at': imported_at.astimezone(timezone.utc).isoformat(),
            'file_sha256': hashlib.sha256(raw).hexdigest(), 'encoding': codec,
            'records': records, 'quarantine': quarantine}
    if recovery is not None:
        snapshot['recovery'] = recovery
    return snapshot


def parse_csv(raw, *, imported_at, encoding=None, recover_malformed=False):
    """Parse a strict complete CSV snapshot, normalizing syntax errors."""
    try:
        return _parse_csv(raw, imported_at=imported_at, encoding=encoding,
                          recover_malformed=recover_malformed)
    except csv.Error as exc:
        raise ValueError('Malformed registry CSV') from exc
