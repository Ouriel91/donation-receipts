"""Import a locally downloaded official registry CSV into OrganizationCache."""
import argparse
import json
from pathlib import Path
from src.organization_cache import OrganizationCache
from src.private_runtime import registry_root, operational_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True)
    location = parser.add_mutually_exclusive_group()
    location.add_argument('--cache', type=Path)
    location.add_argument('--root', type=Path)
    parser.add_argument('--encoding', choices=('utf-8-sig', 'cp1255'))
    parser.add_argument('--recover-known-malformed-record', action='store_true',
                        help='Explicitly quarantine the three fixed reviewed CSV spans; SHA256-bound')
    args = parser.parse_args(argv)
    args.cache = args.cache or registry_root(args.root) / 'organizations.json'
    if args.csv.resolve() == args.cache.resolve():
        parser.error('CSV and cache must be different paths')
    with operational_lock(args.cache.parent):
        result = OrganizationCache(args.cache).import_registry(
            args.csv, encoding=args.encoding,
            recover_malformed=args.recover_known_malformed_record)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
