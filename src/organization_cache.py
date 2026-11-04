"""Offline cache of independently supplied organization and tax evidence."""

import json
import os
import stat
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


_RECORD_FIELDS = {
    "exists", "approval_periods", "denial_periods", "checked_at"
}


def _registration_number(value):
    if not isinstance(value, str):
        raise TypeError("registration_number must be a string")
    value = value.strip()
    if len(value) != 9 or any(c < "0" or c > "9" for c in value):
        raise ValueError("registration_number must contain nine ASCII digits")
    return value


def _date(value):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError("period endpoints and donation_date must be dates")
    return value


def _periods(values):
    if not isinstance(values, (list, tuple)):
        raise TypeError("periods must be a list or tuple")
    result = []
    for period in values:
        if not isinstance(period, (list, tuple)) or len(period) != 2:
            raise TypeError("each period must be a two-item list or tuple")
        start, end = (_date(period[0]), _date(period[1]))
        if start > end:
            raise ValueError("period starts after its end")
        result.append((start, end))
    return tuple(result)


def _evidence(exists, approvals, denials):
    if exists is not True and exists is not False and exists is not None:
        raise TypeError("exists must be True, False, or None")
    approvals = _periods(approvals)
    denials = _periods(denials)
    for start, end in approvals:
        for denied_start, denied_end in denials:
            if start <= denied_end and denied_start <= end:
                raise ValueError("approval and denial periods intersect")
    return exists, approvals, denials


def _utc(value):
    if not isinstance(value, datetime):
        raise ValueError("clock and timestamps must be aware datetimes")
    try:
        if value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(timezone.utc)
    except (TypeError, OverflowError) as exc:
        raise ValueError("invalid aware datetime") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid JSON constant: " + value)


def _stored_date(value):
    if not isinstance(value, str):
        raise ValueError("stored date must be a string")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("stored date must use YYYY-MM-DD")
    return parsed


def _stored_periods(values):
    if not isinstance(values, list):
        raise ValueError("stored periods must be arrays")
    result = []
    for period in values:
        if not isinstance(period, list) or len(period) != 2:
            raise ValueError("stored period must be a two-item array")
        result.append((_stored_date(period[0]), _stored_date(period[1])))
    return result


def _decode(document):
    try:
        if not isinstance(document, dict) or set(document) != {
            "version", "organizations"
        }:
            raise ValueError("invalid cache fields")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("unsupported cache version")
        organizations = document["organizations"]
        if not isinstance(organizations, dict):
            raise ValueError("organizations must be an object")
        records = {}
        for number, record in organizations.items():
            if _registration_number(number) != number:
                raise ValueError("stored registration number is not canonical")
            if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
                raise ValueError("invalid organization record fields")
            exists, approvals, denials = _evidence(
                record["exists"],
                _stored_periods(record["approval_periods"]),
                _stored_periods(record["denial_periods"]),
            )
            timestamp = record["checked_at"]
            if not isinstance(timestamp, str):
                raise ValueError("checked_at must be a timestamp string")
            checked_at = _utc(datetime.fromisoformat(timestamp))
            records[number] = (exists, approvals, denials, checked_at)
        return records
    except (TypeError, OverflowError) as exc:
        raise ValueError("invalid cache data") from exc


class OrganizationCache:
    def __init__(self, path: Path, *, ttl: timedelta = timedelta(days=1),
                 clock=None):
        if not isinstance(ttl, timedelta):
            raise TypeError("ttl must be a timedelta")
        if ttl < timedelta(0):
            raise ValueError("ttl must not be negative")
        self._path = Path(os.path.abspath(os.fspath(path)))
        self._ttl = ttl
        self._clock = clock if clock is not None else self._default_clock
        self._now()
        self._records = self._read()

    @staticmethod
    def _default_clock():
        return datetime.now(timezone.utc)

    def _now(self):
        return _utc(self._clock())

    def _check_path(self):
        # Inspect each component without resolving symbolic links.
        for component in reversed((self._path,) + tuple(self._path.parents)):
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError("symbolic links are not allowed in cache paths")
        parent_mode = self._path.parent.stat().st_mode
        if not stat.S_ISDIR(parent_mode):
            raise NotADirectoryError(str(self._path.parent))

    def _read(self):
        self._check_path()
        try:
            source = self._path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return {}
        with source:
            try:
                document = json.load(
                    source, object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
            except UnicodeError as exc:
                raise ValueError("cache is not valid UTF-8") from exc
        return _decode(document)

    def put(self, registration_number: str, *, exists: bool | None,
            approval_periods=(), denial_periods=()) -> None:
        number = _registration_number(registration_number)
        existence, approvals, denials = _evidence(
            exists, approval_periods, denial_periods
        )
        checked_at = self._now()
        self._records[number] = (existence, approvals, denials, checked_at)

    def lookup(self, registration_number: str, donation_date: date) -> dict:
        number = _registration_number(registration_number)
        donation_date = _date(donation_date)
        now = self._now()
        record = self._records.get(number)
        if record is None:
            return {"exists": None, "section46_status": "unknown", "stale": False}
        exists, approvals, denials, checked_at = record
        if checked_at > now or now - checked_at >= self._ttl:
            return {"exists": None, "section46_status": "unknown", "stale": True}
        status = "unknown"
        if any(start <= donation_date <= end for start, end in approvals):
            status = "approved"
        elif any(start <= donation_date <= end for start, end in denials):
            status = "denied"
        return {"exists": exists, "section46_status": status, "stale": False}

    def save(self) -> None:
        # Refuse to replace a file that has become invalid since construction.
        self._read()
        organizations = {}
        for number, record in self._records.items():
            exists, approvals, denials, checked_at = record
            organizations[number] = {
                "exists": exists,
                "approval_periods": [
                    [start.isoformat(), end.isoformat()] for start, end in approvals
                ],
                "denial_periods": [
                    [start.isoformat(), end.isoformat()] for start, end in denials
                ],
                "checked_at": checked_at.isoformat(),
            }
        payload = json.dumps(
            {"version": 1, "organizations": organizations},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        temporary = None
        descriptor = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".organization-cache-", suffix=".tmp",
                dir=str(self._path.parent),
            )
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_path()
            os.replace(temporary, self._path)
            temporary = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
