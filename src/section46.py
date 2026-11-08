"""Cache-first date-specific tax verification; no unverified live API adapter."""
from datetime import date, datetime, timezone
import json
import re
from urllib.parse import urlsplit


def normalize_evidence(value, *, registration_number, tax_file_number, now):
    """Validate an internal attested or normalized envelope, never a live schema.

    An operator must establish the exact registration-ID/tax-file binding and
    explicit date intervals from an official document. Current approval alone,
    registration existence and not-found responses cannot establish any interval.
    """
    def number(value):
        if not isinstance(value, str) or len(value) != 9 or not value.isascii() or not value.isdigit():
            raise ValueError('Invalid exact identifier')
        return value

    number(registration_number)
    number(tax_file_number)
    fields = {'registration_number', 'tax_file_number', 'source_url', 'retrieved_at',
              'evidence_sha256', 'approval_periods', 'denial_periods', 'verification'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Incomplete evidence envelope')
    if (value['registration_number'] != registration_number
            or value['tax_file_number'] != tax_file_number
            or not isinstance(value['verification'], str)
            or value['verification'] not in {'operator_attested_official_intervals',
                'verified_normalized_transport_intervals'}):
        raise ValueError('Evidence identity/verification mismatch')
    if not isinstance(value['source_url'], str) or not isinstance(value['retrieved_at'], str):
        raise ValueError('Invalid provenance field types')
    source = value['source_url']
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in source) or any(c in source for c in '\\?#'):
        raise ValueError('Noncanonical evidence URL')
    url = urlsplit(source)
    if (url.scheme != 'https' or not url.hostname
            or not (url.hostname == 'gov.il' or url.hostname.endswith('.gov.il'))
            or '@' in url.netloc or '%' in url.netloc
            or re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', url.hostname) is None
            or url.port not in (None, 443)
            or url.query or url.fragment):
        raise ValueError('Invalid official evidence source')
    digest = value['evidence_sha256']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid evidence hash')
    retrieved = datetime.fromisoformat(value['retrieved_at'])
    if not isinstance(now, datetime) or retrieved.utcoffset() is None or now.utcoffset() is None:
        raise ValueError('Invalid evidence retrieval time')
    try:
        retrieved = retrieved.astimezone(timezone.utc)
        now = now.astimezone(timezone.utc)
    except (OverflowError, TypeError) as exc:
        raise ValueError('Unrepresentable UTC retrieval time') from exc
    if retrieved > now:
        raise ValueError('Future evidence retrieval time')
    intervals = []
    for field in ('approval_periods', 'denial_periods'):
        periods = value[field]
        if not isinstance(periods, list):
            raise ValueError('Explicit intervals required')
        parsed = []
        for pair in periods:
            if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(p, str) for p in pair):
                raise ValueError('Invalid evidence interval')
            start, end = (date.fromisoformat(part) for part in pair)
            if pair != [start.isoformat(), end.isoformat()] or start > end:
                raise ValueError('Invalid canonical interval dates')
            parsed.append((start, end))
        intervals.append(parsed)
    if not any(intervals):
        raise ValueError('No explicit historical evidence')
    if any(a <= d and c <= b for a, b in intervals[0] for c, d in intervals[1]):
        raise ValueError('Contradictory evidence')
    normalized = json.loads(json.dumps(value))
    normalized['retrieved_at'] = retrieved.isoformat()
    return normalized


def _usable_cache_result(result):
    """Preserve cache provenance while preventing stale evidence from deciding."""
    result = dict(result)
    if result.get('stale'):
        result['section46_status'] = 'unknown'
    return result


class Section46Verifier:
    def __init__(self, cache, *, transport=None, clock=None):
        self.cache = cache
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(self, registration_number, donation_date, *, tax_file_number=None):
        # Cache validates the ID/date before any transport. Legacy evidence
        # without provenance does not satisfy this independently verified path.
        cached = _usable_cache_result(self.cache.lookup_tax(
            registration_number, donation_date, tax_file_number=tax_file_number))
        if cached['section46_status'] != 'unknown' or cached.get('conflict'):
            return cached | {'origin': 'cache'}
        if self.transport is None or tax_file_number is None:
            return cached | {'origin': 'offline', 'reason': cached.get('reason', 'no_verified_live_adapter_or_tax_binding')}
        if not isinstance(tax_file_number, str) or len(tax_file_number) != 9 or not tax_file_number.isascii() or not tax_file_number.isdigit():
            raise ValueError('Explicit tax file binding required')
        try:
            envelope = self.transport(tax_file_number, donation_date)
            if not isinstance(envelope, dict) or envelope.get('verification') != 'verified_normalized_transport_intervals':
                raise ValueError('Normalized provider provenance required')
            normalized = normalize_evidence(envelope, registration_number=registration_number,
                tax_file_number=tax_file_number, now=self.clock())
            self.cache.store_tax_evidence(normalized)
            refreshed = _usable_cache_result(self.cache.lookup_tax(
                registration_number, donation_date, tax_file_number=tax_file_number))
        except Exception:  # Injected provider boundary: no raw errors or partial approvals.
            return cached | {'origin': 'unavailable', 'reason': 'incomplete_or_unavailable_evidence'}
        return refreshed | {'origin': 'normalized_transport',
            'directory_durable': getattr(self.cache, 'last_save_durable', False)}
