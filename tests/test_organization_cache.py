"""Fixed offline acceptance contract v1; no repo fixtures or generated tests.

The module is deliberately absent during preparation. Do not skip that failure.
Only synthetic registry IDs and evidence are used; all writes use tmp_path.
"""
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
import json
import os
import socket
from pathlib import Path

import pytest

A, B = '000000001', '000000002'
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OLD = (date(2020, 1, 1), date(2020, 12, 31))
NEW = (date(2024, 2, 29), date(2024, 3, 31))
DENIED = (date(2021, 1, 1), date(2021, 12, 31))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Network is forbidden in this offline contract')
    for target, name in ((socket, 'create_connection'), (socket, 'getaddrinfo'),
                         (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
        monkeypatch.setattr(target, name, blocked)


@pytest.fixture
def cache_type():
    # Delayed import permits collection checks before the agent implements code.
    return import_module('src.organization_cache').OrganizationCache


@pytest.fixture
def clock():
    class Clock:
        value = NOW

        def __call__(self):
            return self.value
    return Clock()


def make(cache_type, tmp_path, clock, **kwargs):
    return cache_type(tmp_path / 'cache.json', clock=clock, **kwargs)


def result(exists=None, status='unknown', stale=False):
    return {'exists': exists, 'section46_status': status, 'stale': stale}


def document():
    return {'version': 1, 'organizations': {A: {
        'exists': True, 'approval_periods': [['2020-01-01', '2020-12-31']],
        'denial_periods': [['2021-01-01', '2021-12-31']],
        'checked_at': NOW.isoformat()}}}


def test_empty_cache_and_explicit_save(cache_type, tmp_path, clock):
    cache = make(cache_type, tmp_path, clock)
    assert cache.lookup(A, OLD[0]) == result()
    cache.put(A, exists=True)
    assert not (tmp_path / 'cache.json').exists()
    assert cache.lookup(A, OLD[0]) == result(True)
    cache.save()
    assert json.loads((tmp_path / 'cache.json').read_text()) == {
        'version': 1, 'organizations': {A: {
            'exists': True, 'approval_periods': [], 'denial_periods': [],
            'checked_at': NOW.isoformat()}}}


@pytest.mark.parametrize('exists', [True, False, None])
def test_existence_is_not_tax_approval_or_denial(cache_type, tmp_path, clock, exists):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=exists)
    assert cache.lookup(A, date(2024, 1, 1)) == result(exists)


@pytest.mark.parametrize('exists', [None, False, True])
def test_tax_evidence_does_not_infer_existence(cache_type, tmp_path, clock, exists):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=exists, approval_periods=[OLD], denial_periods=[DENIED])
    assert cache.lookup(A, OLD[0]) == result(exists, 'approved')
    assert cache.lookup(A, DENIED[0]) == result(exists, 'denied')


@pytest.mark.parametrize('day,status', [
    (OLD[0], 'approved'), (OLD[1], 'approved'),
    (date(2020, 6, 15), 'approved'),
    (NEW[0], 'approved'), (NEW[1], 'approved'),
    (DENIED[0], 'denied'), (DENIED[1], 'denied'),
    (date(2019, 12, 31), 'unknown'), (date(2022, 1, 1), 'unknown'),
    (date(2024, 2, 28), 'unknown'), (date(2024, 4, 1), 'unknown'),
])
def test_historical_periods_boundaries_and_gaps(cache_type, tmp_path, clock, day, status):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=True, approval_periods=[OLD, NEW], denial_periods=[DENIED])
    assert cache.lookup(A, day) == result(True, status)


def test_one_day_period_and_same_kind_overlap(cache_type, tmp_path, clock):
    cache = make(cache_type, tmp_path, clock)
    day = date(2024, 2, 29)
    cache.put(A, exists=True, approval_periods=[(day, day), NEW])
    assert cache.lookup(A, day) == result(True, 'approved')
    assert cache.lookup(A, date(2024, 2, 28)) == result(True)


def test_canonical_keys_isolation_and_replacement(cache_type, tmp_path, clock):
    cache = make(cache_type, tmp_path, clock)
    cache.put('  ' + A + '\t', exists=True, approval_periods=[OLD])
    cache.put(B, exists=False)
    assert cache.lookup(A, OLD[0]) == result(True, 'approved')
    assert cache.lookup(' ' + B, OLD[0]) == result(False)
    clock.value += timedelta(hours=1)
    cache.put(A, exists=True, denial_periods=[DENIED])
    assert cache.lookup(A, OLD[0]) == result(True)
    assert cache.lookup(A, DENIED[0]) == result(True, 'denied')
    assert cache.lookup(B, DENIED[0]) == result(False)
    cache.save()
    entries = json.loads((tmp_path / 'cache.json').read_text())['organizations']
    assert set(entries) == {A, B}
    assert entries[A]['checked_at'] == clock.value.isoformat()


def test_caller_and_result_mutations_are_detached(cache_type, tmp_path, clock):
    cache = make(cache_type, tmp_path, clock)
    periods = [[OLD[0], OLD[1]]]
    cache.put(A, exists=True, approval_periods=periods)
    periods[0][0] = date(2025, 1, 1)
    periods.clear()
    answer = cache.lookup(A, OLD[0])
    answer['section46_status'] = 'denied'
    assert cache.lookup(A, OLD[0]) == result(True, 'approved')


@pytest.mark.parametrize('seconds,stale', [(0, False), (59, False), (60, True), (61, True), (-1, True)])
def test_ttl_exact_boundary_and_clock_rollback(cache_type, tmp_path, clock, seconds, stale):
    cache = make(cache_type, tmp_path, clock, ttl=timedelta(seconds=60))
    cache.put(A, exists=True, approval_periods=[OLD], denial_periods=[DENIED])
    clock.value += timedelta(seconds=seconds)
    assert cache.lookup(A, OLD[0]) == (result(stale=True) if stale else result(True, 'approved'))
    assert cache.lookup(A, DENIED[0]) == (result(stale=True) if stale else result(True, 'denied'))


def test_default_ttl_and_zero_ttl(cache_type, tmp_path, clock):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=False)
    clock.value += timedelta(days=1)
    assert cache.lookup(A, OLD[0]) == result(stale=True)
    zero = make(cache_type, tmp_path, clock, ttl=timedelta(0))
    zero.put(A, exists=True)
    assert zero.lookup(A, OLD[0]) == result(stale=True)


def test_persistence_history_ttl_and_refresh(cache_type, tmp_path, clock):
    cache = make(cache_type, tmp_path, clock, ttl=timedelta(minutes=1))
    cache.put(A, exists=True, approval_periods=[OLD, NEW], denial_periods=[DENIED])
    cache.put(B, exists=False)
    cache.save()
    before = (tmp_path / 'cache.json').read_bytes()
    clock.value += timedelta(minutes=2)
    short = make(cache_type, tmp_path, clock, ttl=timedelta(minutes=1))
    assert short.lookup(A, OLD[0]) == result(stale=True)
    long = make(cache_type, tmp_path, clock, ttl=timedelta(days=1))
    assert long.lookup(A, OLD[0]) == result(True, 'approved')
    assert long.lookup(A, NEW[1]) == result(True, 'approved')
    assert long.lookup(A, DENIED[0]) == result(True, 'denied')
    assert long.lookup(B, OLD[0]) == result(False)
    assert (tmp_path / 'cache.json').read_bytes() == before
    short.put(A, exists=True, approval_periods=[OLD])
    assert short.lookup(A, OLD[0]) == result(True, 'approved')


@pytest.mark.parametrize('value,error', [(123, TypeError), (None, TypeError),
    ('', ValueError), ('123', ValueError), ('1234567890', ValueError),
    ('../000001', ValueError), ('abcdefghi', ValueError), ('١٢٣٤٥٦٧٨٩', ValueError)])
def test_invalid_registration_numbers(cache_type, tmp_path, clock, value, error):
    cache = make(cache_type, tmp_path, clock)
    with pytest.raises(error):
        cache.put(value, exists=True)
    with pytest.raises(error):
        cache.lookup(value, OLD[0])


@pytest.mark.parametrize('value', [None, '2020-01-01', datetime(2020, 1, 1), 1])
def test_invalid_donation_date(cache_type, tmp_path, clock, value):
    cache = make(cache_type, tmp_path, clock)
    with pytest.raises(TypeError):
        cache.lookup(A, value)


@pytest.mark.parametrize('kwargs,error', [
    ({'exists': 1}, TypeError), ({'exists': 'yes'}, TypeError),
    ({'exists': True, 'approval_periods': None}, TypeError),
    ({'exists': True, 'approval_periods': ['2020']}, TypeError),
    ({'exists': True, 'approval_periods': [(OLD[1], OLD[0])]}, ValueError),
    ({'exists': True, 'denial_periods': [(DENIED[1], DENIED[0])]}, ValueError),
    ({'exists': True, 'approval_periods': [('2020-01-01', OLD[1])]}, TypeError),
    ({'exists': True, 'approval_periods': [(datetime(2020, 1, 1), OLD[1])]}, TypeError),
    ({'exists': True, 'approval_periods': [OLD], 'denial_periods': [OLD]}, ValueError),
    ({'exists': True, 'approval_periods': [OLD],
      'denial_periods': [(OLD[1], date(2021, 1, 1))]}, ValueError),
])
def test_invalid_put_preserves_state_and_timestamp(cache_type, tmp_path, clock, kwargs, error):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=True, approval_periods=[OLD])
    cache.save()
    before = json.loads((tmp_path / 'cache.json').read_text())
    clock.value += timedelta(hours=1)
    with pytest.raises(error):
        cache.put(A, **kwargs)
    assert cache.lookup(A, OLD[0]) == result(True, 'approved')
    cache.save()
    assert json.loads((tmp_path / 'cache.json').read_text()) == before


@pytest.mark.parametrize('ttl,error', [(60, TypeError), (True, TypeError),
    (None, TypeError), (timedelta(seconds=-1), ValueError)])
def test_invalid_ttl(cache_type, tmp_path, clock, ttl, error):
    with pytest.raises(error):
        make(cache_type, tmp_path, clock, ttl=ttl)


@pytest.mark.parametrize('value', [datetime(2026, 1, 1), date(2026, 1, 1), 'now'])
def test_invalid_clock_rejected_before_mutation(cache_type, tmp_path, clock, value):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=True)
    clock.value = value
    with pytest.raises(ValueError):
        cache.put(A, exists=False)
    clock.value = NOW
    assert cache.lookup(A, OLD[0]) == result(True)


def test_aware_clock_offset_normalized_to_utc(cache_type, tmp_path, clock):
    clock.value = NOW.astimezone(timezone(timedelta(hours=2)))
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=True)
    cache.save()
    stamp = json.loads((tmp_path / 'cache.json').read_text())['organizations'][A]['checked_at']
    assert stamp == NOW.isoformat()


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(version=2), lambda d: d.update(version=True),
    lambda d: d.update(extra=True), lambda d: d.update(organizations=[]),
    lambda d: d['organizations'][A].update(exists=1),
    lambda d: d['organizations'][A].update(extra='unexpected'),
    lambda d: d['organizations'][A].pop('exists'),
    lambda d: d['organizations'][A].update(checked_at='2026-01-01T00:00:00'),
    lambda d: d['organizations'][A].update(approval_periods=[['2020-02-30', '2020-12-31']]),
    lambda d: d['organizations'][A].update(approval_periods=[['2020-12-31', '2020-01-01']]),
    lambda d: d['organizations'][A].update(denial_periods=[['2020-01-01', '2020-12-31']]),
    lambda d: d['organizations'][A].update(denial_periods=None),
    lambda d: d['organizations'].update({' ' + A: d['organizations'].pop(A)}),
    lambda d: d['organizations'].update({B: {'exists': True}}),
])
def test_corrupt_schema_is_not_reset_or_partially_loaded(cache_type, tmp_path, clock, mutation):
    data = document()
    mutation(data)
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        make(cache_type, tmp_path, clock)
    assert path.read_bytes() == before


@pytest.mark.parametrize('raw', ['{broken', 'null', '[]',
    '{"version":1,"version":1,"organizations":{}}',
    '{"version":1,"organizations":{"000000001":{},"000000001":{}}}'])
def test_malformed_json_and_duplicate_keys(cache_type, tmp_path, clock, raw):
    path = tmp_path / 'cache.json'
    path.write_text(raw)
    with pytest.raises(ValueError):
        make(cache_type, tmp_path, clock)
    assert path.read_text() == raw


def test_load_valid_schema(cache_type, tmp_path, clock):
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps(document()))
    cache = make(cache_type, tmp_path, clock)
    assert cache.lookup(A, OLD[0]) == result(True, 'approved')
    assert cache.lookup(A, DENIED[0]) == result(True, 'denied')


def test_save_is_atomic_and_fsynced(cache_type, tmp_path, clock, monkeypatch):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=True)
    cache.save()
    path = tmp_path / 'cache.json'
    before = path.read_bytes()
    cache.put(B, exists=False)
    original_replace, original_fsync = os.replace, os.fsync
    events = []

    def sync(fd):
        events.append('fsync')
        return original_fsync(fd)

    def replace(source, destination, *args, **kwargs):
        source, destination = Path(source), Path(destination)
        assert source != destination and source.parent == path.parent
        assert destination == path and path.read_bytes() == before
        assert set(json.loads(source.read_text())['organizations']) == {A, B}
        assert 'fsync' in events
        events.append('replace')
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'replace', replace)
    cache.save()
    assert 'replace' in events
    assert set(tmp_path.iterdir()) in ({path}, {path, tmp_path / '.organization-cache.lock'})
    if (tmp_path / '.organization-cache.lock').exists():
        assert (tmp_path / '.organization-cache.lock').is_file()
        assert not (tmp_path / '.organization-cache.lock').is_symlink()
    assert set(json.loads(path.read_text())['organizations']) == {A, B}


@pytest.mark.parametrize('operation', ['replace', 'fsync'])
def test_failed_save_preserves_old_bytes_and_cleans_temp(cache_type, tmp_path, clock, monkeypatch, operation):
    cache = make(cache_type, tmp_path, clock)
    cache.put(A, exists=True)
    cache.save()
    path = tmp_path / 'cache.json'
    before = path.read_bytes()
    cache.put(B, exists=False)

    def fail(*args, **kwargs):
        raise OSError('synthetic disk failure')

    monkeypatch.setattr(os, operation, fail)
    with pytest.raises(OSError):
        cache.save()
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) in ({path}, {path, tmp_path / '.organization-cache.lock'})
    if (tmp_path / '.organization-cache.lock').exists():
        assert (tmp_path / '.organization-cache.lock').is_file()
        assert not (tmp_path / '.organization-cache.lock').is_symlink()


def test_symlink_destination_rejected_on_load_and_save(cache_type, tmp_path, clock):
    outside = tmp_path / 'unrelated.json'
    outside.write_text(json.dumps(document()))
    before = outside.read_bytes()
    cache = make(cache_type, tmp_path, clock)
    cache.put(B, exists=True)
    (tmp_path / 'cache.json').symlink_to(outside)
    with pytest.raises(ValueError):
        make(cache_type, tmp_path, clock)
    with pytest.raises(ValueError):
        cache.save()
    assert outside.read_bytes() == before


def test_ancestor_symlink_rejected(cache_type, tmp_path, clock):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'alias'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        cache_type(link / 'cache.json', clock=clock)
    assert list(real.iterdir()) == []


def test_parent_replaced_with_symlink_before_save(cache_type, tmp_path, clock):
    parent = tmp_path / 'parent'
    parent.mkdir()
    cache = cache_type(parent / 'cache.json', clock=clock)
    cache.put(A, exists=True)
    moved = tmp_path / 'moved'
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError):
        cache.save()
    assert list(moved.iterdir()) == []


def test_missing_parent_is_not_created(cache_type, tmp_path, clock):
    parent = tmp_path / 'missing'
    with pytest.raises(OSError):
        cache_type(parent / 'cache.json', clock=clock)
    assert not parent.exists()
