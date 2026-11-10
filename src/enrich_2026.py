"""Windows-only enrichment of an existing report. No Gmail or receipt extraction."""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import os
import sys
from uuid import uuid4

from src.organization_cache import OrganizationCache
from src.organization_enrichment import enrich_workbook, LIVE_HEADERS, HEADERS
from src.private_runtime import operational_lock, safe_path, registry_root
from src.section46_live import LiveSection46


def locate(reports):
    import openpyxl
    candidates = []
    for path in sorted(reports.rglob('*.xlsx')):
        if '2026' not in path.parts and '2026' not in path.stem:
            continue
        if path.name.startswith(('~$', '.')) or 'enriched' in path.stem.lower():
            continue
        wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
        try:
            valid = False
            enriched = False
            for ws in wb.worksheets:
                headers = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
                valid |= all(h in headers for h in ('מספר עמותה', 'תאריך'))
                enriched |= any(h in headers for h in LIVE_HEADERS + HEADERS)
            if valid and not enriched:
                candidates.append(path)
        finally:
            wb.close()
    if not candidates:
        raise ValueError('No original 2026 donation workbook found under reports')
    if len(candidates) == 1:
        return candidates[0]
    print('נמצאו מספר דוחות; בחרו את הדוח הרצוי (הבחירה נשארת במחשב המקומי):')
    for i, path in enumerate(candidates, 1):
        print(f'{i}: {path}')
    selected = int(input('מספר דוח: '))
    if not 1 <= selected <= len(candidates):
        raise ValueError('Invalid report selection')
    return candidates[selected - 1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--reports', type=Path, default=Path('reports'))
    parser.add_argument('--root', type=Path, help='Override shared AppData registry; also DONATION_RECEIPTS_REGISTRY_ROOT')
    parser.add_argument('--freshness-days', type=int, default=30)
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Run on local Windows only; do not upload private reports')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        source = safe_path(args.input or locate(safe_path(args.reports)))
        root = registry_root(args.root)
        if not (root / 'organizations.json').is_file():
            raise ValueError('Existing operational organizations.json not found')
        output = source.with_name(source.stem + '_enriched_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8] + '.xlsx')
        before = hashlib.sha256(source.read_bytes()).digest()
        with operational_lock(root):
            registry = OrganizationCache(root / 'organizations.json')
            live = LiveSection46(root / 'section46-live.json', registry, ttl_days=args.freshness_days)
            print('אבחון API קודם (ספירות בלבד): ' + str(live.diagnostics()))
            counts = enrich_workbook(source, root / 'organizations.json', output, live=live)
        if hashlib.sha256(source.read_bytes()).digest() != before:
            raise ValueError('Source changed during processing; inspect locally')
        print('מרשם תפעולי: ' + str(root))
        print('אבחון API נוכחי (ספירות בלבד): ' + str(live.diagnostics()))
        print('הושלם: ' + ', '.join(f'{k}={v}' for k, v in (counts | dict(live.counts)).items()))
        print('המקור לא השתנה. קובץ חדש: ' + str(output))
        return 0
    except Exception as exc:
        print(f'לא הושלם ({type(exc).__name__}); בדקו מקומית את בחירת הדוח, הנתיבים והמטמון. אין להסיק דחייה מכשל שירות.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
