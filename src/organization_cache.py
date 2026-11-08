"""Offline cache of independently supplied organization and tax evidence."""

import json
import hashlib
import os
import stat
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


_RECORD_FIELDS = {
    "exists", "approval_periods", "denial_periods", "checked_at"
}


def _registry_recoveries(snapshot):
    """Normalize the previous single-span audit without changing stored bytes."""
    value = snapshot.get('recovery', [])
    return [value] if isinstance(value, dict) else value


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
        if (not isinstance(document, dict) or not {"version", "organizations"} <= set(document)
                or set(document) - {"version", "organizations", "registry", "tax_evidence"}):
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
                 clock=None, tax_ttl: timedelta = timedelta(days=90)):
        if not isinstance(ttl, timedelta):
            raise TypeError("ttl must be a timedelta")
        if ttl < timedelta(0):
            raise ValueError("ttl must not be negative")
        if not isinstance(tax_ttl, timedelta) or tax_ttl < timedelta(0):
            raise ValueError("Invalid independent tax evidence TTL")
        self._tax_ttl = tax_ttl
        self._dirty = set()
        self.last_save_durable = True
        from src.private_runtime import safe_path
        self._path = safe_path(path)
        self._ttl = ttl
        self._clock = clock if clock is not None else self._default_clock
        self._now()
        self._registry = None
        self._tax_evidence = {}
        self._records = self._read()

    @staticmethod
    def _default_clock():
        return datetime.now(timezone.utc)

    def _now(self):
        return _utc(self._clock())

    def _check_path(self):
        from src.private_runtime import safe_path
        safe_path(self._path)
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
            self._registry = None
            self._tax_evidence = {}
            return {}
        with source:
            try:
                document = json.load(
                    source, object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
            except UnicodeError as exc:
                raise ValueError("cache is not valid UTF-8") from exc
        records = _decode(document)
        registry = document.get("registry")
        if registry is not None:
            self._validate_registry(registry)
            for recovery in _registry_recoveries(registry):
                from src.private_runtime import safe_path
                artifact = safe_path(self._path.parent / recovery['artifact'])
                payload = artifact.read_bytes()
                if (len(payload) != recovery['byte_end'] - recovery['byte_start']
                        or hashlib.sha256(payload).hexdigest() != recovery['sha256']):
                    raise ValueError('Registry quarantine artifact differs from audit')
        tax_evidence = document.get("tax_evidence", {})
        self._validate_tax_evidence(tax_evidence)
        self._tax_evidence = tax_evidence
        self._registry = registry
        return records

    @staticmethod
    def _validate_registry(snapshot):
        from src.registry_csv import SOURCE
        required = {"source", "imported_at", "file_sha256", "encoding", "records", "quarantine"}
        if (not isinstance(snapshot, dict) or not required <= set(snapshot)
                or set(snapshot) - (required | {"recovery"})
                or snapshot["source"] != SOURCE
                or snapshot["encoding"] not in ("utf-8-sig", "cp1255")
                or not isinstance(snapshot["file_sha256"], str)
                or len(snapshot["file_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in snapshot["file_sha256"])
                or not isinstance(snapshot["records"], dict)
                or not isinstance(snapshot["quarantine"], list)
                or not isinstance(snapshot["imported_at"], str)):
            raise ValueError("Invalid registry snapshot")
        _utc(datetime.fromisoformat(snapshot["imported_at"]))
        if "recovery" in snapshot:
            from src.registry_csv import APPROVED_RECOVERY
            digest, spans, columns = APPROVED_RECOVERY
            recoveries = _registry_recoveries(snapshot)
            # Older single-span caches retain their original audit representation.
            expected = spans[:1] if isinstance(snapshot['recovery'], dict) else spans
            if (not isinstance(recoveries, list) or not recoveries
                    or len(recoveries) != len(expected)):
                raise ValueError('Invalid registry recovery audit')
            previous_end = 0
            for recovery, (first, last) in zip(recoveries, expected):
                if (not isinstance(recovery, dict) or set(recovery) != {
                    "start_line", "end_line", "resume_line", "byte_start", "byte_end",
                    "sha256", "artifact", "reason", "expected_columns"}
                    or snapshot["file_sha256"] != digest
                    or any(type(recovery[k]) is not int for k in (
                        "start_line", "end_line", "resume_line", "byte_start", "byte_end",
                        "expected_columns"))
                    or (recovery["start_line"], recovery["end_line"], recovery["resume_line"],
                        recovery["expected_columns"]) != (first, last, last + 1, columns)
                    or not previous_end < recovery["byte_start"] < recovery["byte_end"]
                    or recovery["artifact"] != f'registry-quarantine-{digest}-{first}-{last}.bin'
                    or recovery["reason"] != "malformed_csv"
                    or not isinstance(recovery["sha256"], str)
                    or len(recovery["sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in recovery["sha256"])):
                    raise ValueError('Invalid registry recovery audit')
                previous_end = recovery['byte_end']
        for number, name in snapshot["records"].items():
            if (_registration_number(number) != number or not isinstance(name, str)
                    or not name.strip() or name != name.strip()
                    or any(unicodedata.category(c).startswith("C") for c in name)
                    or not any("א" <= c <= "ת" for c in name)):
                raise ValueError("Invalid registry identity")
        for row in snapshot["quarantine"]:
            if (not isinstance(row, dict) or set(row) != {
                    "line", "registration_number", "official_name", "reason"}
                    or type(row["line"]) is not int or row["line"] < 2
                    or not isinstance(row["registration_number"], str)
                    or not isinstance(row["official_name"], str)
                    or not isinstance(row["reason"], str)
                    or row["reason"] not in {
                        "invalid_id", "invalid_name", "column_count", "duplicate_id"}):
                raise ValueError("Invalid registry quarantine")

    def import_registry(self, csv_path, *, encoding=None, recover_malformed=False):
        with self._write_lock():
            return self._import_registry_locked(csv_path, encoding=encoding,
                                                recover_malformed=recover_malformed)

    def _import_registry_locked(self, csv_path, *, encoding=None, recover_malformed=False):
        """Import a full local snapshot; persisted tax evidence remains independent.

        Save pending tax edits before calling. Cache writers must be serialized.
        """
        if self._dirty:
            raise ValueError("Save pending tax edits before importing registry")
        from src.registry_csv import parse_csv
        raw = Path(csv_path).read_bytes()
        snapshot = parse_csv(raw, imported_at=self._now(), encoding=encoding,
                             recover_malformed=recover_malformed)
        self._validate_registry(snapshot)
        recoveries = _registry_recoveries(snapshot)
        for recovery in recoveries:
            self._preserve_quarantine(raw, recovery)
        reasons = [row['reason'] for row in snapshot['quarantine']]
        counts = {'verified': len(snapshot['records']), 'quarantined': len(reasons),
                  'rows_read': len(snapshot['records']) + len(reasons),
                  'duplicate_rows': reasons.count('duplicate_id'),
                  'duplicate_ids': len({row['registration_number'] for row in snapshot['quarantine']
                                        if row['reason'] == 'duplicate_id'}),
                  'malformed_segments': len(recoveries),
                  'quarantined_physical_lines': sum(r['end_line'] - r['start_line'] + 1
                                                     for r in recoveries)}
        self._records = self._read()
        if self._registry and self._registry["file_sha256"] == snapshot["file_sha256"]:
            if self._registry != snapshot:
                # Import timestamps can differ on an otherwise identical repeat.
                comparable = {**snapshot, 'imported_at': self._registry['imported_at']}
                if comparable != self._registry:
                    raise ValueError('Existing registry differs for the same source SHA256')
            return {"updated": False, **counts}
        previous = self._registry
        self._registry = snapshot
        try:
            self._save_locked(registry_snapshot=snapshot)
        except BaseException:
            self._registry = previous
            raise
        return {"updated": True, **counts,
                "directory_durable": self.last_save_durable}

    def _preserve_quarantine(self, raw, recovery):
        """Publish exact rejected bytes before the cache; never overwrite evidence.

        A later cache failure may leave an unreferenced, immutable audit artifact.
        """
        from src.private_runtime import safe_path, fsync_directory
        artifact = safe_path(self._path.parent / recovery['artifact'])
        if artifact == self._path:
            raise ValueError('Quarantine artifact must differ from cache')
        payload = raw[recovery['byte_start']:recovery['byte_end']]
        if hashlib.sha256(payload).hexdigest() != recovery['sha256']:
            raise ValueError('Quarantine digest mismatch')
        if artifact.is_symlink():
            raise ValueError('Symbolic quarantine artifact is not allowed')
        if artifact.exists():
            if artifact.read_bytes() != payload:
                raise ValueError('Existing quarantine artifact differs')
            fsync_directory(self._path.parent)
            return
        descriptor, temporary = tempfile.mkstemp(prefix='.registry-quarantine-', dir=self._path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, artifact)
            except FileExistsError:
                if artifact.is_symlink() or artifact.read_bytes() != payload:
                    raise ValueError('Quarantine artifact publication conflict')
            fsync_directory(self._path.parent)
        finally:
            os.unlink(temporary)

    def official_identity(self, registration_number):
        import copy
        number = _registration_number(registration_number)
        snapshot = self._registry
        if snapshot is None or number not in snapshot["records"]:
            return None
        return {"registration_number": number, "official_name": snapshot["records"][number],
                "provenance": copy.deepcopy({key: snapshot[key] for key in
                    ("source", "imported_at", "file_sha256")})}

    @staticmethod
    def _validate_tax_evidence(records):
        from src.section46 import normalize_evidence
        try:
            if not isinstance(records, dict):
                raise ValueError("Invalid tax evidence map")
            for number, envelopes in records.items():
                if _registration_number(number) != number or not isinstance(envelopes, list) or not envelopes:
                    raise ValueError("Invalid tax evidence entries")
                for value in envelopes:
                    normalize_evidence(value, registration_number=number,
                        tax_file_number=value["tax_file_number"],
                        now=_utc(datetime.fromisoformat(value["retrieved_at"])))
        except (TypeError, KeyError, OverflowError, AttributeError) as exc:
            raise ValueError("Invalid stored tax evidence") from exc

    def lookup_tax(self, registration_number, donation_date, *, tax_file_number=None):
        import copy
        number = _registration_number(registration_number)
        donated = _date(donation_date)
        now = self._now()
        if tax_file_number is not None:
            tax_file_number = _registration_number(tax_file_number)
        envelopes = self._tax_evidence.get(number, [])
        bindings = {e["tax_file_number"] for e in envelopes}
        reverse_conflict = any(
            other != number and any(e["tax_file_number"] in bindings for e in other_envelopes)
            for other, other_envelopes in self._tax_evidence.items())
        if len(bindings) > 1 or reverse_conflict:
            return {"section46_status": "unknown", "stale": False,
                    "conflict": True, "provenance": []}
        statuses, all_statuses, matched = set(), set(), []
        stale = False
        for evidence in envelopes:
            if tax_file_number is not None and evidence["tax_file_number"] != tax_file_number:
                continue
            retrieved = _utc(datetime.fromisoformat(evidence["retrieved_at"]))
            expired = retrieved > now or now - retrieved >= self._tax_ttl
            for field, status in (("approval_periods", "approved"), ("denial_periods", "denied")):
                if any(date.fromisoformat(a) <= donated <= date.fromisoformat(b)
                       for a, b in evidence[field]):
                    all_statuses.add(status)
                    if expired:
                        stale = True
                    else:
                        statuses.add(status)
                        matched.append(evidence)
        return {"section46_status": next(iter(statuses)) if len(statuses) == 1 and len(all_statuses) == 1 else "unknown",
                "stale": stale and not statuses, "conflict": len(all_statuses) > 1,
                "provenance": copy.deepcopy(matched)}

    def store_tax_evidence(self, evidence):
        with self._write_lock():
            return self._store_tax_evidence_locked(evidence)

    def _store_tax_evidence_locked(self, evidence):
        if self._dirty:
            raise ValueError("Save pending tax edits before storing evidence")
        from src.section46 import normalize_evidence
        import copy
        value = normalize_evidence(evidence, registration_number=evidence["registration_number"],
            tax_file_number=evidence["tax_file_number"], now=self._now())
        self._records = self._read()  # Preserve persisted identity and legacy tax fields.
        previous = copy.deepcopy(self._tax_evidence)
        envelopes = self._tax_evidence.setdefault(value["registration_number"], [])
        if value in envelopes:
            return False
        envelopes.append(value)
        try:
            self._save_locked(tax_snapshot=self._tax_evidence)
        except BaseException:
            self._tax_evidence = previous
            raise
        return True

    def put(self, registration_number: str, *, exists: bool | None,
            approval_periods=(), denial_periods=()) -> None:
        number = _registration_number(registration_number)
        existence, approvals, denials = _evidence(
            exists, approval_periods, denial_periods
        )
        checked_at = self._now()
        self._records[number] = (existence, approvals, denials, checked_at)
        self._dirty.add(number)

    def _lookup_legacy(self, registration_number: str, donation_date: date) -> dict:
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

    def lookup(self, registration_number: str, donation_date: date) -> dict:
        # Legacy v1 compatibility only. New product tax decisions use lookup_tax.
        return self._lookup_legacy(registration_number, donation_date)

    def save(self) -> None:
        self._save()

    def _write_lock(self):
        from src.private_runtime import file_lock
        self._check_path()
        return file_lock(self._path.with_name('.organization-cache.lock'))

    def _save(self, *, registry_snapshot=None, tax_snapshot=None):
        with self._write_lock():
            self._save_locked(registry_snapshot=registry_snapshot, tax_snapshot=tax_snapshot)

    def _save_locked(self, *, registry_snapshot=None, tax_snapshot=None):
        # Refuse to replace a file that has become invalid since construction.
        pending = {number: self._records[number] for number in self._dirty}
        persisted = self._read()
        self._records = {**persisted, **pending}
        if registry_snapshot is not None:
            self._registry = registry_snapshot
        if tax_snapshot is not None:
            self._tax_evidence = tax_snapshot
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
        document = {"version": 1, "organizations": organizations}
        if self._registry is not None:
            document["registry"] = self._registry
        if self._tax_evidence:
            document["tax_evidence"] = self._tax_evidence
        _decode(document)
        if self._registry is not None:
            self._validate_registry(self._registry)
        self._validate_tax_evidence(self._tax_evidence)
        payload = json.dumps(
            document,
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
            self._dirty.clear()
            self.last_save_durable = os.name != 'nt'
            from src.private_runtime import fsync_directory
            try:
                fsync_directory(self._path.parent)
            except OSError:
                # Publication already happened: do not roll back in-memory state.
                self.last_save_durable = False
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
