"""Offline document-backed Section 46 lookup with explicit historical intervals."""
import argparse
from datetime import date, timedelta
import hashlib
import json
import sys
from pathlib import Path
from src.organization_cache import OrganizationCache, _unique_object
from src.private_runtime import operational_lock, safe_path, operational_root, bounded_read
from src.section46 import Section46Verifier


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'runtime/registry')
    parser.add_argument('--registration-number', required=True)
    parser.add_argument('--donation-date', type=date.fromisoformat, required=True)
    parser.add_argument('--tax-ttl-days', type=int, default=90)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--document', type=Path)
    args = parser.parse_args(argv)
    if bool(args.evidence) != bool(args.document):
        parser.error('Both normalized evidence and its official document are required')
    try:
        args.root = operational_root(args.root)
        with operational_lock(args.root):
            cache = OrganizationCache(safe_path(args.root) / 'organizations.json', tax_ttl=timedelta(days=args.tax_ttl_days))
            if args.evidence:
                evidence = json.loads(bounded_read(args.evidence, 1024 * 1024).decode('utf-8'), object_pairs_hook=_unique_object)
                if (evidence['verification'] != 'operator_attested_official_intervals'
                        or evidence['registration_number'] != args.registration_number
                        or evidence['evidence_sha256'] != hashlib.sha256(bounded_read(args.document, 20 * 1024 * 1024)).hexdigest()):
                    raise ValueError('Evidence document/identity mismatch')
                cache.store_tax_evidence(evidence)
                if not cache.last_save_durable:
                    print("הראיות פורסמו; עמידות התיקייה בנפילת מתח לא אושרה")
            result = Section46Verifier(cache).verify(args.registration_number, args.donation_date)
    except Exception:
        print('לא ניתן לאמת את הראיות; אין להסיק אישור או דחייה.')
        return 1
    print('סעיף 46 בתאריך התרומה: ' + result['section46_status'])
    print('מקור החלטה: ' + result['origin'] + '; השירות החי אינו מחובר או מאומת')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
