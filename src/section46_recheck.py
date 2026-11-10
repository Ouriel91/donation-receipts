"""Windows-local recheck of the four unrecognized Section 46 responses only."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from src.organization_cache import OrganizationCache
from src.organization_enrichment import enrich_workbook
from src.private_runtime import operational_lock, registry_root, safe_path
from src.section46_live import LiveSection46, NOT_FOUND, identifier


def selected_numbers(live):
    """Original failures and their persisted markers, never an organization scan."""
    return sorted(number for number, record in live.state['organizations'].items()
        if record.get('unrecognized_recheck_at') or (
            record.get('last_attempt_status') == 'unavailable_or_ambiguous'
            and record.get('last_attempt_diagnostic', {}).get('code') == 'unrecognized_response'))


def response_summary(record):
    """Only fixed labels, bounded numeric statuses and JSON types are displayed."""
    diagnostic = record.get('last_attempt_diagnostic', {})
    if record.get('last_attempt_status') == 'success' and record.get('evidence'):
        message = record['evidence'][-1]['message']
        return {'response_type': 'no_current_approval' if message == NOT_FOUND else 'approval_interval',
                'http_status': 200, 'api_status': 200,
                'message_kind': 'str' if message == NOT_FOUND else 'dict'}
    allowed = {'unrecognized_response', 'timeout', 'network_error', 'http_error',
               'oversize_response', 'invalid_response'}
    code = diagnostic.get('code')
    result = {'response_type': code if code in allowed else 'not_completed'}
    for field in ('http_status', 'api_status'):
        value = diagnostic.get(field)
        if type(value) is int and 100 <= value <= 599:
            result[field] = value
    for field in ('json_kind', 'message_kind'):
        value = diagnostic.get(field)
        if value in {'dict', 'list', 'str', 'int', 'float', 'bool', 'NoneType', 'invalid'}:
            result[field] = value
    return result


def recheck_report(root, source, *, live_options=None):
    root, source = registry_root(root), safe_path(source)
    if source.suffix.lower() != '.xlsx' or not source.is_file():
        raise ValueError('Original XLSX missing')
    if not (root / 'organizations.json').is_file() or not (root / 'section46-live.json').is_file():
        raise ValueError('Existing operating registry and Section 46 cache required')
    before = hashlib.sha256(source.read_bytes()).digest()
    with operational_lock(root):
        cache = OrganizationCache(root / 'organizations.json')
        live = LiveSection46(root / 'section46-live.json', cache, **(live_options or {}))
        numbers = selected_numbers(live)
        print('מספרי העמותות לבדיקה מקומית בלבד: ' + ', '.join(numbers))
        if len(numbers) != 4:
            raise ValueError('Expected exactly four saved unrecognized organizations; no requests made')
        live.allowed_requests = {identifier(n) for n in numbers}
        for number in numbers:
            record = live.state['organizations'][number]
            print(number + ' לפני: ' + json.dumps(response_summary(record), ensure_ascii=True))
            live.prepare(number, recheck_unrecognized=True)
            print(number + ' אחרי: ' + json.dumps(response_summary(record), ensure_ascii=True))
        output = source.with_name(source.stem + '_enriched_section46_' + uuid4().hex[:12] + '.xlsx')
        counts = enrich_workbook(source, root / 'organizations.json', output, live=live)
    if hashlib.sha256(source.read_bytes()).digest() != before:
        raise ValueError('Original changed during execution; inspect locally')
    print('סיכום: ' + json.dumps(counts | dict(live.counts), ensure_ascii=True, sort_keys=True))
    print('המקור לא השתנה. קובץ חדש: ' + str(output))
    return numbers, counts, live, output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--input', type=Path, default=Path('reports/primary/2026/donation_summary_2026.xlsx'))
    parser.add_argument('--freshness-days', type=int, default=30)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Run on Windows only; private reports and cache must stay local')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        recheck_report(args.root, args.input, live_options={'ttl_days': args.freshness_days})
    except ValueError as exc:
        # Only our fixed, known setup messages may be printed, never cache bodies.
        safe_errors = {'Original XLSX missing', 'Existing operating registry and Section 46 cache required',
                       'Expected exactly four saved unrecognized organizations; no requests made',
                       'Original changed during execution; inspect locally'}
        print(str(exc) if str(exc) in safe_errors else 'הבדיקה לא הושלמה; בדקו מקומית את המרשם והמטמון')
        return 1
    except Exception:
        print('הבדיקה לא הושלמה; בדקו מקומית את הנתיבים, המרשם והמטמון')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
