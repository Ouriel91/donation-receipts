"""Focused local delivery workflow using only synthetic identities/workbooks."""
from datetime import datetime, timezone
import json

import openpyxl
import pytest

from src.organization_cache import OrganizationCache
from src.private_runtime import atomic_json
from src.section46_live import LiveSection46
from src.section46_recheck import recheck_report

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
NEGATIVE = b'{"Status":200,"Message":"Charity association number not found"}'
POSITIVE = json.dumps({'Status': 200, 'Message': {'start_date': '2024-01-01',
    'end_date': '2029-12-31', 'as_of_date': '2026-11-09'}}).encode()
NUMBERS = ['111111111', '222222222', '333333333', '444444444']


def seed(tmp_path):
    root = tmp_path / 'registry'
    root.mkdir()
    cache = OrganizationCache(root / 'organizations.json', clock=lambda: NOW)
    cache.save()
    live = LiveSection46(root / 'section46-live.json', cache, clock=lambda: NOW,
        transport=lambda n: POSITIVE)
    live.prepare('555555555')
    for number in NUMBERS:
        live.state['organizations'][number] = {'evidence': [], 'last_attempt_at': NOW.isoformat(),
            'last_attempt_status': 'unavailable_or_ambiguous',
            'last_attempt_diagnostic': {'code': 'unrecognized_response', 'http_status': 200,
                                      'api_status': 200, 'message_kind': 'str'}}
    atomic_json(live.path, live.state)
    source = tmp_path / 'original.xlsx'
    wb = openpyxl.Workbook()
    wb.active.append(['מספר עמותה', 'תאריך', 'שם עמותה', 'amount'])
    for number in NUMBERS + ['555555555', '666666666']:
        wb.active.append([number, '01/01/2026', 'Synthetic unverified name', 100])
    wb.save(source)
    return root, source


def test_only_four_rechecked_and_repeat_uses_negative_ttl_and_failure_cooldown(tmp_path, capsys):
    root, source = seed(tmp_path)
    original = source.read_bytes()
    calls = []
    def transport(number):
        calls.append(number)
        if number == NUMBERS[0]:
            return NEGATIVE
        if number == NUMBERS[1]:
            return POSITIVE
        if number == NUMBERS[2]:
            return b'{"Status":200,"Message":"PRIVATE UNVERIFIED BODY"}'
        if number == NUMBERS[3]:
            raise TimeoutError('PRIVATE EXCEPTION')
        pytest.fail('Request outside the four selected IDs')
    options = {'transport': transport, 'clock': lambda: NOW, 'sleep': lambda n: None}
    numbers, counts, live, output = recheck_report(root, source, live_options=options)
    assert numbers == calls == NUMBERS
    assert live.counts['api_requests'] == 4 and live.counts['api_errors'] == 2
    assert counts['outcome_no_current_approval'] == 1
    assert counts['approved'] == 2 and counts['unknown'] == 4
    assert live.state['organizations']['111111111']['last_attempt_status'] == 'success'
    assert live.state['organizations']['666666666'].get('last_attempt_at') is None
    wb = openpyxl.load_workbook(output)
    assert 'לא נמצא אישור נוכחי בשירות רשות המסים' in wb.active['H2'].value
    wb.close()
    report = capsys.readouterr().out
    assert all(number in report for number in NUMBERS)
    assert 'no_current_approval' in report and 'approval_interval' in report
    assert 'unrecognized_response' in report and 'timeout' in report
    assert 'PRIVATE' not in report and 'Synthetic unverified name' not in report
    assert source.read_bytes() == original
    numbers2, _, repeat, output2 = recheck_report(root, source, live_options=options)
    assert numbers2 == NUMBERS and calls == NUMBERS
    assert repeat.counts['api_requests'] == 0
    assert output2 != output and output.is_file() and source.read_bytes() == original


def test_selection_count_mismatch_makes_no_requests(tmp_path):
    root, source = seed(tmp_path)
    path = root / 'section46-live.json'
    state = json.loads(path.read_bytes())
    del state['organizations'][NUMBERS[-1]]
    atomic_json(path, state)
    with pytest.raises(ValueError, match='exactly four'):
        recheck_report(root, source, live_options={'clock': lambda: NOW,
            'transport': lambda n: pytest.fail('No requests allowed')})
    assert not list(tmp_path.glob('*enriched*.xlsx'))
