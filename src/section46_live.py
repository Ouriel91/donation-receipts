"""Read-only official check46Clause transport and private lazy evidence cache.

This schema binds evidence to the requested association only, never a tax file.
The returned interval is not a complete approval history.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import time
import urllib.request
import urllib.error

from src.organization_cache import _unique_object
from src.private_runtime import atomic_json, bounded_read, safe_path

ENDPOINT = 'https://openapi.taxes.gov.il/shaam/production/Charity/v1/check46Clause'
NOT_FOUND = 'Charity association number not found'


class OfficialResponseError(ValueError):
    def __init__(self, diagnostic):
        super().__init__('Official response could not establish approval')
        self.diagnostic = diagnostic


def response_diagnostic(raw, *, code, http_status=None):
    """Allowlisted metadata only: never names, IDs, body text, or exceptions."""
    result = {'code': code}
    if type(http_status) is int:
        result['http_status'] = http_status
    if isinstance(raw, bytes):
        result.update(response_sha256=hashlib.sha256(raw).hexdigest(), response_bytes=len(raw))
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            result['json_kind'] = 'invalid'
        else:
            result['json_kind'] = type(value).__name__
            if isinstance(value, dict):
                status = value.get('Status')
                if type(status) is int and 100 <= status <= 599:
                    result['api_status'] = status
                message = value.get('Message')
                result['message_kind'] = type(message).__name__
                if isinstance(message, dict):
                    result['date_field_types'] = {key: type(message.get(key)).__name__
                        for key in ('start_date', 'end_date', 'as_of_date')}
    return result


def identifier(number):
    if not isinstance(number, str) or not re.fullmatch(r'[0-9]{9}', number):
        raise ValueError('Invalid association number')
    return number


def parse_response(raw):
    try:
        return _parse_response(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise OfficialResponseError(response_diagnostic(raw, code='unrecognized_response', http_status=200)) from exc


def _parse_response(raw):
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {'Status', 'Message'} or type(value['Status']) is not int or value['Status'] != 200:
        raise ValueError('Ambiguous official response')
    message = value['Message']
    if message == NOT_FOUND:
        return message
    if not isinstance(message, dict) or set(message) != {'start_date', 'end_date', 'as_of_date'}:
        raise ValueError('Incomplete approval dates')
    dates = []
    for key in ('start_date', 'end_date', 'as_of_date'):
        part = message[key]
        if not isinstance(part, str):
            raise ValueError('Invalid date')
        parsed = date.fromisoformat(part)
        if parsed.isoformat() != part:
            raise ValueError('Noncanonical date')
        dates.append(parsed)
    if dates[0] > dates[1]:
        raise ValueError('Reversed period')
    return message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected redirect')


def request_official(number):
    identifier(number)
    request = urllib.request.Request(ENDPOINT,
        data=json.dumps({'Charity_association_number': int(number)}).encode('ascii'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise OfficialResponseError({'code': 'http_error', 'http_status': response.status})
            raw = response.read(65537)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(65537)
        finally:
            exc.close()
        raise OfficialResponseError(response_diagnostic(raw, code='http_error', http_status=exc.code)) from None
    if len(raw) > 65536:
        raise OfficialResponseError({'code': 'oversize_response', 'response_bytes': len(raw)})
    return raw


class LiveSection46:
    def __init__(self, path, registry, *, ttl_days=30, transport=request_official,
                 clock=lambda: datetime.now(timezone.utc), sleep=time.sleep,
                 allowed_requests=None):
        if not 1 <= ttl_days <= 365:
            raise ValueError('Freshness must be 1..365 days')
        self.path = safe_path(path)
        self.registry, self.transport, self.clock, self.sleep = registry, transport, clock, sleep
        self.ttl = timedelta(days=ttl_days)
        self.allowed_requests = None if allowed_requests is None else {identifier(n) for n in allowed_requests}
        self.counts = Counter()
        self.seen = set()
        self.state = (json.loads(bounded_read(self.path, 32 * 1024 * 1024), object_pairs_hook=_unique_object)
                      if self.path.exists() else {'version': 1, 'organizations': {}})
        if set(self.state) != {'version', 'organizations'} or self.state['version'] != 1 or not isinstance(self.state['organizations'], dict):
            raise ValueError('Invalid live cache')
        for number, record in self.state['organizations'].items():
            identifier(number)
            if not isinstance(record, dict):
                raise ValueError('Invalid live cache record')
            for evidence in record.get('evidence', []):
                self._validate(evidence, number)

    def _validate(self, evidence, number):
        if (evidence['registration_number'] != number or evidence['source_url'] != ENDPOINT
                or evidence['verification'] != 'official_check46Clause_request_bound'
                or hashlib.sha256(evidence['raw_response'].encode('utf-8')).hexdigest() != evidence['response_sha256']):
            raise ValueError('Invalid API provenance')
        message = parse_response(evidence['raw_response'].encode('utf-8'))
        if evidence['message'] != message:
            raise ValueError('API evidence mismatch')
        stamp = datetime.fromisoformat(evidence['retrieved_at'])
        if stamp.utcoffset() is None or (message != NOT_FOUND and date.fromisoformat(message['as_of_date']) > stamp.date()):
            raise ValueError('Invalid API verification time')
        return stamp

    def fresh(self, evidence, number):
        stamp = self._validate(evidence, number)
        now = self.clock()
        # A stale server snapshot is not a fresh verification merely because it was downloaded today.
        return (timedelta(0) <= now - stamp < self.ttl
                and (evidence['message'] == NOT_FOUND or
                     timedelta(0) <= now.date() - date.fromisoformat(evidence['message']['as_of_date']) < self.ttl))

    def prepare(self, number, extracted_name=None, *, identity_only=False, recheck_unrecognized=False):
        identifier(number)
        if number in self.seen:
            return
        self.seen.add(number)
        record = self.state['organizations'].setdefault(number, {'evidence': []})
        recheck = (recheck_unrecognized
                   and record.get('last_attempt_status') == 'unavailable_or_ambiguous'
                   and record.get('last_attempt_diagnostic', {}).get('code') == 'unrecognized_response'
                   and not record.get('unrecognized_recheck_at'))
        identity = self.registry.official_identity(number)
        record['identity_status'] = 'official_registry' if identity else 'pending_unverified'
        if not identity and isinstance(extracted_name, str):
            record['extracted_name_unverified'] = extracted_name
        if identity_only:
            atomic_json(self.path, self.state)
            return
        if not recheck and any(self.fresh(e, number) for e in record['evidence']):
            self.counts['cache_hits'] += 1
            atomic_json(self.path, self.state)
            return
        if self.allowed_requests is not None and number not in self.allowed_requests:
            self.counts['outside_recheck_scope'] += 1
            atomic_json(self.path, self.state)
            return
        # New diagnosed failures get a one-day cooldown. Old undiagnosed failures
        # are retried once to obtain useful safe diagnostics, never bulk-scanned.
        if not recheck and record.get('last_attempt_status') == 'unavailable_or_ambiguous' and record.get('last_attempt_diagnostic'):
            stamp = datetime.fromisoformat(record['last_attempt_at'])
            if stamp.utcoffset() is not None and timedelta(0) <= self.clock() - stamp < timedelta(days=1):
                self.counts['failure_cooldown_hits'] += 1
                atomic_json(self.path, self.state)
                return
        # Applicable existing document-backed evidence is handled by the caller.
        record['last_attempt_at'] = self.clock().isoformat()
        if recheck:
            # Retain selection/provenance after success clears the error field.
            # This keeps reruns focused on the same four without extra calls.
            record['unrecognized_recheck_at'] = record['last_attempt_at']
        record['last_attempt_status'] = 'started'
        atomic_json(self.path, self.state)
        if self.counts['api_requests']:
            self.sleep(1.0)
        self.counts['api_requests'] += 1
        try:
            raw = self.transport(number)
            if not isinstance(raw, bytes) or len(raw) > 65536:
                raise ValueError('Invalid response bytes')
            message = parse_response(raw)
            evidence = {'registration_number': number, 'source_url': ENDPOINT,
                'verification': 'official_check46Clause_request_bound',
                'retrieved_at': self.clock().isoformat(), 'message': message,
                'raw_response': raw.decode('utf-8'), 'response_sha256': hashlib.sha256(raw).hexdigest()}
            self._validate(evidence, number)
            record['evidence'].append(evidence)
            record['last_success_at'] = evidence['retrieved_at']
            record['last_attempt_status'] = 'success'
            record.pop('last_attempt_diagnostic', None)
        except Exception as exc:
            record['last_attempt_status'] = 'unavailable_or_ambiguous'
            if isinstance(exc, OfficialResponseError):
                diagnostic = exc.diagnostic
            else:
                code = ('timeout' if isinstance(exc, TimeoutError) else
                        'network_error' if isinstance(exc, (urllib.error.URLError, OSError)) else 'invalid_response')
                diagnostic = {'code': code}
            record['last_attempt_diagnostic'] = diagnostic
            self.counts['api_errors'] += 1
        atomic_json(self.path, self.state)

    def diagnostics(self):
        """Aggregate-only report, including old failures whose causes were lost."""
        counts = Counter()
        allowed = {'http_error', 'unrecognized_response', 'oversize_response',
                   'timeout', 'network_error', 'invalid_response'}
        for record in self.state['organizations'].values():
            if record.get('last_attempt_status') != 'unavailable_or_ambiguous':
                continue
            diagnostic = record.get('last_attempt_diagnostic', {})
            code = diagnostic.get('code')
            counts[code if code in allowed else 'legacy_unclassified'] += 1
            status = diagnostic.get('http_status')
            if type(status) is int and 100 <= status <= 599:
                counts[f'http_{status}'] += 1
            status = diagnostic.get('api_status')
            if type(status) is int and 100 <= status <= 599:
                counts[f'api_status_{status}'] += 1
        return dict(counts)

    def outcome(self, number, donated):
        record = self.state['organizations'].get(number, {})
        evidence = record.get('evidence', [])
        fresh = [e for e in evidence if self.fresh(e, number)]
        if any(e['message'] != NOT_FOUND and date.fromisoformat(e['message']['start_date']) <= donated <= date.fromisoformat(e['message']['end_date']) for e in fresh):
            return 'approved'
        if self.current_not_found(number):
            return 'no_current_approval'
        if record.get('last_attempt_status') == 'unavailable_or_ambiguous':
            return ('unrecognized_response' if record.get('last_attempt_diagnostic', {}).get('code') == 'unrecognized_response'
                    else 'api_unavailable')
        if fresh:
            return 'historical_gap'
        if evidence:
            return 'stale_evidence'
        if record.get('last_attempt_at'):
            return 'api_unavailable'
        return 'never_checked'

    def current_not_found(self, number):
        evidence = self.state['organizations'].get(number, {}).get('evidence', [])
        fresh = [e for e in evidence if self.fresh(e, number)]
        return bool(fresh and max(reversed(fresh), key=lambda e: datetime.fromisoformat(e['retrieved_at']))['message'] == NOT_FOUND)

    def values(self, number, donated):
        record = self.state['organizations'].get(number, {})
        evidence = record.get('evidence', [])
        fresh = [e for e in evidence if self.fresh(e, number)]
        matching = [e for e in fresh if e['message'] != NOT_FOUND and date.fromisoformat(e['message']['start_date']) <= donated <= date.fromisoformat(e['message']['end_date'])]
        outcome = self.outcome(number, donated)
        approved = outcome == 'approved'
        selected = (matching or fresh or evidence)
        notes = []
        if selected:
            e = selected[-1]
            m = e['message']
            if m != NOT_FOUND:
                notes.append(f"תקופת אישור: {m['start_date']}–{m['end_date']}; נכון ל־{m['as_of_date']}; מקור: רשות המסים ({ENDPOINT}); " + ('בדיקה בתוקף' if self.fresh(e, number) else 'נדרשת בדיקה עדכנית'))
        if self.current_not_found(number):
            notes.append('לא נמצא אישור נוכחי בשירות רשות המסים; אין בכך קביעה שהעמותה אינה קיימת במרשם או שלילה היסטורית; מקור: ' + ENDPOINT)
        elif selected and selected[-1]['message'] == NOT_FOUND:
            notes.append('בדיקה קודמת מצאה שלא נמצא אישור נוכחי בשירות; תוקף המטמון פג ונדרשת בדיקה עדכנית')
        if not approved:
            notes.append('אין ראיה עדכנית המכסה את תאריך התרומה; אין להסיק דחייה או היסטוריה מלאה')
        if outcome == 'historical_gap':
            notes.append('תאריך התרומה מחוץ לתקופות האישור שהתקבלו; השירות אינו מספק היסטוריה מלאה')
        if outcome == 'never_checked':
            notes.append('טרם בוצעה בדיקה רשמית')
        if record.get('last_attempt_status') == 'unavailable_or_ambiguous':
            notes.append('הניסיון האחרון נכשל או שהתשובה אינה חד־משמעית')
        if record.get('identity_status') == 'pending_unverified':
            notes.append('זהות העמותה ושמה טרם אומתו במרשם המקומי')
        labels = {'approved': 'מאושר', 'historical_gap': 'לא ידוע — אין ראיה לתאריך התרומה',
                  'no_current_approval': 'לא ידוע לתאריך התרומה — לא נמצא אישור נוכחי בשירות',
                  'api_unavailable': 'לא ידוע — הבדיקה לא הושלמה',
                  'unrecognized_response': 'לא ידוע — תשובת שירות לא מוכרת',
                  'stale_evidence': 'לא ידוע — נדרשת בדיקה עדכנית',
                  'never_checked': 'לא ידוע — טרם נבדק'}
        return (labels[outcome],
                'עשוי להיות מוכר לזיכוי מס — בכפוף לתנאים' if approved else 'נדרש בירור',
                record.get('last_success_at', '')[:10], '; '.join(notes)), ('approved' if approved else 'unknown')
