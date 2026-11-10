"""Durable weekly CSV refresh; scheduled daily, downloads only when due."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler

from src.organization_cache import OrganizationCache, _utc, _unique_object
from src.private_runtime import (atomic_json, atomic_bytes, bounded_read,
    operational_lock, registry_root, safe_path)
from src.registry_csv import parse_csv
from src import registry_csv
import hashlib

RESOURCE = 'be5b7935-3922-45d4-9638-08871b17ec95'
METADATA_URL = 'https://data.gov.il/api/3/action/resource_show?id=' + RESOURCE
MAX_CSV_BYTES = 100 * 1024 * 1024


def official_url(url):
    if not isinstance(url, str):
        raise ValueError('Invalid download URL')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'data.gov.il', 'www.data.gov.il'}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment):
        raise ValueError('Download URL is not an allowed official source')
    return url


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, official_url(newurl))


def _get(url, limit, *, expected_size=None):
    deadline = time.monotonic() + 60
    with build_opener(OfficialRedirects()).open(official_url(url), timeout=20) as response:
        official_url(response.geturl())
        length = response.headers.get('Content-Length')
        length = int(length) if length is not None else None
        if length is not None and not 0 <= length <= limit:
            raise ValueError('Invalid response length')
        chunks, total = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('Total download deadline exceeded')
            reader = getattr(response, 'read1', response.read)
            chunk = reader(min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError('Response exceeds byte limit')
        if time.monotonic() > deadline or (length is not None and length != total):
            raise ValueError('Truncated or timed-out response')
        if expected_size is not None and expected_size != total:
            raise ValueError('Official metadata size mismatch')
    return b''.join(chunks)


def download_registry():
    metadata = json.loads(_get(METADATA_URL, 1024 * 1024), object_pairs_hook=_unique_object)
    if not isinstance(metadata, dict) or metadata.get('success') is not True:
        raise ValueError('Invalid official metadata')
    resource = metadata.get('result')
    if (not isinstance(resource, dict) or resource.get('id') != RESOURCE
            or str(resource.get('format', '')).lower() != 'csv'):
        raise ValueError('Wrong official resource')
    size = resource.get('size')
    if type(size) is not int or not 0 < size <= MAX_CSV_BYTES:
        raise ValueError('Positive official metadata size required')
    return _get(official_url(resource.get('url')), MAX_CSV_BYTES, expected_size=size)


def _state(path, now):
    if not path.exists():
        return {}
    state = json.loads(bounded_read(path, 4096), object_pairs_hook=_unique_object)
    if not isinstance(state, dict) or set(state) != {'last_official_success', 'last_manual_import', 'file_sha256'}:
        raise ValueError('Invalid refresh state')
    for field in ('last_official_success', 'last_manual_import'):
        if state[field] is not None:
            if _utc(datetime.fromisoformat(state[field])) > now:
                raise ValueError('Future refresh state')
    if not isinstance(state['file_sha256'], str) or len(state['file_sha256']) != 64:
        raise ValueError('Invalid refresh digest')
    return state


def refresh(root=None, *, manual_csv=None, force=False, allow_large_change=False,
            downloader=download_registry, clock=None):
    """All snapshots/cache stay local; failure never advances official due time.

    Manual imports do not suppress the next official download. Large shrink or
    quarantine changes require an explicit operator flag; no scheduled override.
    """
    root_path, now, imported = None, None, {}
    reason = 'setup_failed'
    try:
        root_path = registry_root(root)
        now = _utc((clock or (lambda: datetime.now(timezone.utc)))())
        with operational_lock(root_path):
            try:
                cache_path = safe_path(root_path / 'organizations.json')
                state_path = safe_path(root_path / 'sync-state.json')
                reason = 'cache_invalid'
                cache = OrganizationCache(cache_path, clock=lambda: now)
                reason = 'state_invalid'
                try:
                    state = _state(state_path, now)
                except (ValueError, TypeError, KeyError, RecursionError, OverflowError, OSError):
                    if not (manual_csv or force):
                        raise
                    state = {}
                checked = state.get('last_official_success')
                if (not manual_csv and not force and checked and cache_path.exists()
                        and cache._registry and cache._registry["file_sha256"] == state["file_sha256"]
                        and (now.date() - _utc(datetime.fromisoformat(checked)).date()).days < 7):
                    return {'status': 'not_due', 'updated': False, 'last_official_success': checked}
                reason = 'network_or_input_unavailable'
                raw = bounded_read(manual_csv, MAX_CSV_BYTES) if manual_csv else downloader()
                if not isinstance(raw, bytes) or len(raw) > MAX_CSV_BYTES:
                    raise ValueError('Invalid CSV download')
                reason = 'csv_invalid_or_abnormal_change'
                # Only the exact SHA-bound previously reviewed bytes get recovery.
                recover = hashlib.sha256(raw).hexdigest() == registry_csv.APPROVED_RECOVERY[0]
                parsed = parse_csv(raw, imported_at=now, recover_malformed=recover)
                old_count = len(cache._registry['records']) if cache._registry else 0
                new_count, rejected = len(parsed['records']), len(parsed['quarantine'])
                unchanged = cache._registry and cache._registry['file_sha256'] == parsed['file_sha256']
                if not unchanged and not allow_large_change and ((old_count and new_count < old_count * .9)
                        or rejected > max(10, (new_count + rejected) * .05)):
                    raise ValueError('Large change needs explicit operator review')
                reason = 'snapshot_publication_failed'
                snapshots = safe_path(root_path / 'snapshots')
                snapshots.mkdir(exist_ok=True)
                digest = parsed['file_sha256']
                snapshot = safe_path(snapshots / (digest + '.csv'))
                # Repair an interrupted old snapshot from validated identical bytes.
                snapshot_durable = True
                if not snapshot.exists() or bounded_read(snapshot, MAX_CSV_BYTES) != raw:
                    snapshot_durable = atomic_bytes(snapshot, raw)
                reason = 'cache_import_failed'
                imported = cache.import_registry(snapshot, recover_malformed=recover)
                reason = 'state_publication_failed'
                state_durable = atomic_json(state_path, {
                    'last_official_success': state.get('last_official_success') if manual_csv else now.isoformat(),
                    'last_manual_import': now.isoformat() if manual_csv else state.get('last_manual_import'),
                    'file_sha256': digest})
                result = {'status': 'success', **imported, 'file_sha256': digest,
                          'mode': 'manual_csv' if manual_csv else 'official_download',
                          'directory_durable': bool(snapshot_durable and state_durable
                                                    and imported.get('directory_durable', True))}
                try:
                    diagnostic_durable = atomic_json(safe_path(root_path / 'last-attempt.json'), {'at': now.isoformat(), **result})
                    result['diagnostic_saved'] = True
                    result['diagnostic_durable'] = diagnostic_durable
                except Exception:
                    result['diagnostic_saved'] = False
                return result
            except Exception:
                result = {'status': 'unavailable', 'updated': imported.get('updated', False), 'reason': reason}
                try:
                    atomic_json(safe_path(root_path / 'last-attempt.json'), {'at': now.isoformat(), **result})
                except Exception:
                    pass
                return result

    except BlockingIOError:
        return {'status': 'unavailable', 'updated': False, 'reason': 'operation_busy'}
    except Exception:  # Network/provider/filesystem boundary: sanitized failures only.
        result = {'status': 'unavailable', 'updated': imported.get('updated', False), 'reason': reason}
        return result


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--manual-csv', type=Path)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--allow-large-change', action='store_true', help='Explicit reviewed shrink/quarantine override')
    args = parser.parse_args(argv)
    result = refresh(args.root, manual_csv=args.manual_csv, force=args.force,
                     allow_large_change=args.allow_large_change)
    labels = {'success': 'הרענון הושלם', 'not_due': 'טרם הגיע מועד הרענון השבועי',
              'unavailable': 'הרענון לא הושלם; בדקו אבחון מקומי. אם הרשת חסומה, אפשר לייבא CSV מקומי'}
    print(labels[result['status']])
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 1 if result['status'] == 'unavailable' else 0


if __name__ == '__main__':
    raise SystemExit(main())
