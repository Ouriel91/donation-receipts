import hashlib
import json
from datetime import datetime, timezone

import openpyxl
import pytest

from src.organization_cache import OrganizationCache
from src.organization_enrichment import enrich_workbook, LIVE_HEADERS
from src.section46_live import LiveSection46, parse_response

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
RAW = json.dumps({'Status': 200, 'Message': {'start_date': '2024-01-01', 'end_date': '2029-12-31', 'as_of_date': '2026-11-09'}}).encode()


def setup(tmp_path, transport=lambda n: RAW):
    path = tmp_path / 'organizations.json'
    cache = OrganizationCache(path, clock=lambda: NOW)
    cache.save()
    live = LiveSection46(tmp_path / 'live.json', cache, transport=transport, clock=lambda: NOW, sleep=lambda n: None)
    return cache, live


def test_request_binding_pending_cache_and_gap(tmp_path):
    cache, live = setup(tmp_path)
    live.prepare('123456789', 'Synthetic')
    assert live.counts['api_requests'] == 1
    record = live.state['organizations']['123456789']
    assert record['identity_status'] == 'pending_unverified'
    assert 'tax_file_number' not in record['evidence'][0]
    assert live.values('123456789', NOW.date())[1] == 'approved'
    assert live.values('123456789', NOW.date().replace(year=2020))[1] == 'unknown'
    again = LiveSection46(live.path, cache, transport=lambda n: pytest.fail('network'), clock=lambda: NOW)
    again.prepare('123456789')
    assert again.counts['cache_hits'] == 1
    assert again.counts['api_requests'] == 0


@pytest.mark.parametrize('raw', [b'{}', b'null', b'{"Status":404,"Message":{}}', RAW.replace(b'2029-12-31', b'2020-01-01'), RAW.replace(b'2024-01-01', b'2024-02-30')])
def test_malformed(raw, tmp_path):
    with pytest.raises((ValueError, TypeError)):
        parse_response(raw)
    _, live = setup(tmp_path, lambda n: raw)
    live.prepare('123456789')
    assert 'last_success_at' not in live.state['organizations']['123456789']
    assert live.values('123456789', NOW.date())[1] == 'unknown'


def test_unavailable(tmp_path):
    def unavailable(n):
        raise TimeoutError()
    _, live = setup(tmp_path, unavailable)
    live.prepare('123456789')
    assert live.counts['api_errors'] == 1
    assert live.values('123456789', NOW.date())[1] == 'unknown'


def test_workbook_preserved_date_and_unique(tmp_path):
    cache, live = setup(tmp_path)
    source, output = tmp_path / 'original.xlsx', tmp_path / 'new.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['מספר עמותה', 'תאריך', 'שם עמותה', 'amount', 'review'])
    ws.append(['123456789', '01/01/2026', 'Synthetic', 100, '=1+1'])
    ws.append(['123456789', '01/01/2020', 'Synthetic', 50, 'review'])
    ws['C2'].hyperlink = 'https://example.org'
    wb.create_sheet('Summary')['A1'] = '=SUM(Sheet!D2:D3)'
    wb.save(source)
    before = source.read_bytes()
    counts = enrich_workbook(source, cache._path, output, live=live)
    assert source.read_bytes() == before
    assert counts['rows'] == 2 and counts['approved'] == 1 and counts['unknown'] == 1
    assert live.counts['api_requests'] == 1
    result = openpyxl.load_workbook(output)
    assert [result.active.cell(1, c).value for c in range(6, 10)] == list(LIVE_HEADERS)
    assert result.active['E2'].value == '=1+1'
    assert result.active['C2'].value == 'Synthetic'
    assert result.active['C2'].hyperlink.target == 'https://example.org'
    result.close()
    with pytest.raises(ValueError):
        enrich_workbook(source, cache._path, output, live=live)


def test_stale_refresh_failure_is_not_success(tmp_path):
    from datetime import timedelta
    cache, live = setup(tmp_path)
    live.prepare('123456789')
    success = live.state['organizations']['123456789']['last_success_at']
    later = NOW + timedelta(days=31)
    def unavailable(n):
        raise TimeoutError()
    stale = LiveSection46(live.path, cache, clock=lambda: later, transport=unavailable)
    stale.prepare('123456789')
    record = stale.state['organizations']['123456789']
    assert record['last_success_at'] == success
    assert record['last_attempt_at'] != success
    assert stale.values('123456789', NOW.date())[1] == 'unknown'


def test_transport_exact_request_and_bounds(monkeypatch):
    from src.section46_live import request_official, ENDPOINT
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            assert limit == 65537
            return RAW
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == ENDPOINT
            assert request.get_method() == 'POST'
            assert json.loads(request.data) == {'Charity_association_number': 123456789}
            assert not request.has_header('Authorization')
            assert timeout == 20
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    assert request_official('123456789') == RAW


def test_live_names_are_written_and_originals_audited(tmp_path):
    cache, live = setup(tmp_path)
    csv = tmp_path / 'synthetic.csv'
    csv.write_text('מספר עמותה,שם עמותה בעברית\n123456789,עמותה רשמית סינתטית\n', encoding='utf-8')
    cache.import_registry(csv)
    source, output = tmp_path / 'original.xlsx', tmp_path / 'corrected.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['מספר עמותה', 'תאריך', 'שם עמותה', 'amount', 'review'])
    ws.append(['123456789', '01/01/2026', 'שם קבלה שגוי', 100, '=1+1'])
    ws.append(['123456789', '02/01/2026', 'עמותה רשמית סינתטית', 50, 'בדיקה'])
    ws.append(['987654321', '03/01/2026', 'שם לא מאומת', 20, 'בדיקה'])
    wb.save(source)
    before = source.read_bytes()
    counts = enrich_workbook(source, cache._path, output, live=live)
    assert counts['official_names'] == 2 and counts['official_names_changed'] == 1
    assert counts['unmatched_names'] == 1
    result = openpyxl.load_workbook(output)
    assert result.active['C2'].value == result.active['C3'].value == 'עמותה רשמית סינתטית'
    assert result.active['C4'].value == 'שם לא מאומת'
    assert 'אינו מאומת' in result.active['I4'].value
    assert result['Registry identity audit']['D2'].value == 'שם קבלה שגוי'
    assert result['Registry identity audit']['E2'].value == 'עמותה רשמית סינתטית'
    assert result.active['E2'].value == '=1+1'
    result.close()
    assert source.read_bytes() == before


def test_distinct_unknown_outcomes_and_safe_diagnostics(tmp_path):
    from datetime import timedelta
    cache, live = setup(tmp_path)
    assert live.outcome('123456789', NOW.date()) == 'never_checked'
    live.prepare('123456789')
    assert live.outcome('123456789', NOW.date().replace(year=2020)) == 'historical_gap'
    later = LiveSection46(live.path, cache, clock=lambda: NOW + timedelta(days=31))
    assert later.outcome('123456789', NOW.date()) == 'stale_evidence'
    # This unverified no-approval-like schema must NOT become an official denial.
    raw = b'{"Status":200,"Message":null,"private":"DO NOT PRINT donor-name"}'
    live.transport = lambda n: raw
    live.prepare('987654321')
    assert live.outcome('987654321', NOW.date()) == 'unrecognized_response'
    assert live.values('987654321', NOW.date())[1] == 'unknown'
    record = live.state['organizations']['987654321']
    diagnostic = record['last_attempt_diagnostic']
    assert diagnostic['api_status'] == 200 and diagnostic['message_kind'] == 'NoneType'
    assert diagnostic['response_sha256'] == hashlib.sha256(raw).hexdigest()
    assert 'donor' not in json.dumps(diagnostic)
    assert 'last_success_at' not in record
    again = LiveSection46(live.path, cache, clock=lambda: NOW, transport=lambda n: pytest.fail('repeat failure'))
    again.prepare('987654321')
    assert again.counts['failure_cooldown_hits'] == 1
    assert again.diagnostics() == {'unrecognized_response': 1, 'http_200': 1, 'api_status_200': 1}


def test_old_errors_are_diagnosed_once_without_losing_successes(tmp_path):
    cache, live = setup(tmp_path)
    live.prepare('123456789')
    evidence = live.state['organizations']['123456789']['evidence'][:]
    live.state['organizations']['987654321'] = {'evidence': [], 'last_attempt_at': NOW.isoformat(),
        'last_attempt_status': 'unavailable_or_ambiguous'}
    assert live.diagnostics() == {'legacy_unclassified': 1}
    live.transport = lambda n: (_ for _ in ()).throw(TimeoutError('PRIVATE'))
    live.prepare('987654321')
    live.prepare('987654321')
    assert live.counts['api_errors'] == 1
    assert live.outcome('987654321', NOW.date()) == 'api_unavailable'
    assert live.diagnostics() == {'timeout': 1}
    assert live.state['organizations']['123456789']['evidence'] == evidence
    assert 'PRIVATE' not in live.path.read_text()


def test_http_failure_diagnostics_do_not_keep_response_text(tmp_path, monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    from src.section46_live import request_official
    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError('https://example.test', 403, 'PRIVATE ERROR', {}, BytesIO(b'PRIVATE BODY'))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    _, live = setup(tmp_path, request_official)
    live.prepare('123456789')
    assert live.diagnostics() == {'http_error': 1, 'http_403': 1}
    assert live.values('123456789', NOW.date())[1] == 'unknown'
    assert 'PRIVATE' not in live.path.read_text()


NOT_FOUND_RAW = b'{"Status":200,"Message":"Charity association number not found"}'


def test_exact_not_found_is_successful_cached_observation_not_historical_denial(tmp_path):
    from datetime import timedelta
    from src.section46_live import NOT_FOUND
    cache, live = setup(tmp_path, lambda n: NOT_FOUND_RAW)
    live.prepare('123456789')
    record = live.state['organizations']['123456789']
    assert record['last_attempt_status'] == 'success'
    assert record['last_success_at'] == NOW.isoformat()
    assert record['evidence'][0]['message'] == NOT_FOUND
    assert 'as_of_date' not in record['evidence'][0]
    assert live.counts['api_errors'] == 0 and live.diagnostics() == {}
    assert live.outcome('123456789', NOW.date().replace(year=2020)) == 'no_current_approval'
    values, status = live.values('123456789', NOW.date().replace(year=2020))
    assert status == 'unknown' and values[2] == '2026-11-09'
    assert 'לא נמצא אישור נוכחי בשירות רשות המסים' in values[3]
    assert 'כשל' not in values[3]
    assert cache.official_identity('123456789') is None
    again = LiveSection46(live.path, cache, clock=lambda: NOW + timedelta(days=29),
        transport=lambda n: pytest.fail('negative TTL must avoid network'))
    again.prepare('123456789')
    assert again.counts['cache_hits'] == 1 and again.counts['api_requests'] == 0
    expired = LiveSection46(live.path, cache, clock=lambda: NOW + timedelta(days=30),
        transport=lambda n: NOT_FOUND_RAW)
    assert expired.outcome('123456789', NOW.date()) == 'stale_evidence'
    expired.prepare('123456789')
    assert expired.counts['api_requests'] == 1 and expired.counts['api_errors'] == 0


@pytest.mark.parametrize('raw', [
    NOT_FOUND_RAW.replace(b'not found', b'Not found'),
    NOT_FOUND_RAW.replace(b'not found', b'not found '),
    NOT_FOUND_RAW.replace(b'200', b'404'),
    NOT_FOUND_RAW.replace(b'200', b'true'),
    NOT_FOUND_RAW.replace(b'}', b',"extra":1}'),
])
def test_not_found_requires_exact_verified_schema(raw):
    with pytest.raises(ValueError):
        parse_response(raw)


def test_current_not_found_preserves_verified_historical_interval(tmp_path):
    cache, live = setup(tmp_path)
    live.prepare('123456789')
    record = live.state['organizations']['123456789']
    original_evidence = record['evidence'][0].copy()
    record['last_attempt_status'] = 'unavailable_or_ambiguous'
    record['last_attempt_diagnostic'] = {'code': 'unrecognized_response'}
    # Force only this previously unrecognized response past the error cooldown.
    live.seen.clear()
    live.transport = lambda n: NOT_FOUND_RAW
    live.prepare('123456789', recheck_unrecognized=True)
    assert record['evidence'][0] == original_evidence
    assert live.values('123456789', NOW.date())[1] == 'approved'
    assert 'לא נמצא אישור נוכחי בשירות רשות המסים' in live.values('123456789', NOW.date())[0][3]
    assert live.values('123456789', NOW.date().replace(year=2020))[1] == 'unknown'
