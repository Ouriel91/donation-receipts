# Historical standalone cache prototype (contract v1)

This retained profile tests a GENERATED STANDALONE v1 prototype, not the current
repository cache with registry/tax metadata and shared locking helpers. Task 2
uses separate refresh, tax-provider and privacy-guard profiles; the actual
repository cache and Excel integration receive host tests and full-source review.
Do not represent this legacy two-file profile as current-cache integration.

## Goal, scope and delivery

Implement ONLY `src/organization_cache.py`, using the Python standard library.
Return exactly JSON `{"code": "<complete Python module>"}` to product.py. Do
not use tools or commands, edit tests, add files, or modify the runner. No
network, Gmail, environment/credential access, personal data or import-time
I/O. This is an offline evidence cache, not a registry lookup or legal advice.
All IDs and evidence in acceptance tests are synthetic. Do not fetch evidence.
The complete acceptance file is fixed at `tests/test_organization_cache.py`.
The runner copies only that test file and your module into its temporary root;
no src/__init__.py, other product modules or repository fixtures are available.

## Public API (exact names and return shapes)

```python
from datetime import date, datetime, timedelta
from pathlib import Path

class OrganizationCache:
    def __init__(self, path: Path, *, ttl: timedelta = timedelta(days=1),
                 clock=None): ...
    def put(self, registration_number: str, *, exists: bool | None,
            approval_periods=(), denial_periods=()) -> None: ...
    def lookup(self, registration_number: str, donation_date: date) -> dict: ...
    def save(self) -> None: ...
```

`clock` is a callable returning an aware datetime; default is current UTC.
Normalize timestamps to UTC. Reject naive/non-datetime clock values with
ValueError. Reject a negative TTL with ValueError and a non-timedelta TTL with
TypeError. Zero TTL is valid and immediately stale. Constructors load existing
cache JSON or start empty if missing; neither construction, lookup nor put
writes files. The existing parent directory must be a directory; do not create
missing parents. Registration numbers are strings of exactly nine ASCII digits
AFTER stripping surrounding whitespace. Preserve leading zeros; integer IDs
are TypeError, invalid strings ValueError. Do not use IDs as filesystem paths.

`put` replaces the whole record for that ID, preserving other IDs, and records
`clock()` as checked_at. exists must be exactly True, False or None (other
values TypeError). Each period collection is a list or tuple of two-item lists
or tuples of datetime.date values; datetime instances are not valid dates here.
Invalid container/date types are TypeError; reversed periods are ValueError.
Copy all caller-owned collections so later mutations cannot change the cache.
All three existence values are allowed with tax evidence: keep these facts
independent and do not infer registry existence from approval or denial.
An approval and a denial period must not intersect (inclusive endpoints);
conflicting evidence raises ValueError. Overlaps within the same collection
are allowed; normalization/merging is optional. Validate before changing state:
an invalid put must leave the previous record and checked_at unchanged.

`lookup` returns a NEW dict with EXACTLY these keys:

- `exists`: True, False or None.
- `section46_status`: the string `approved`, `denied` or `unknown`.
- `stale`: bool.

Validate donation_date as a date excluding datetime (otherwise TypeError).
For a fresh record: return stored exists independently of the tax outcome;
return approved if donation_date is in an approval period, denied if it is in
an explicit official-denial period, otherwise unknown. Endpoints are inclusive.
Registry absence, an empty approval list, gaps and dates outside known periods
are NEVER evidence of official denial. Never project today's approval or denial
backwards/forwards to an unrelated date. Preserve all historical periods supplied
by the caller; an expired historical approval may still cover an old donation.
Missing ID returns exists=None, section46_status=unknown, stale=False.

Freshness uses the evidence-fetch timestamp, NOT donation_date or the period's
end. A record is stale when clock() - checked_at >= ttl OR checked_at is in the
future (clock rollback). Stale lookups return exists=None, status=unknown,
stale=True without deleting evidence. Reopening with a different TTL changes
freshness, not the stored evidence. No sleeping or automatic refresh.

## Persistence and safe filesystem behavior

UTF-8 JSON schema, exact field names, no extra fields:

```json
{"version": 1, "organizations": {
  "000000001": {
    "exists": true,
    "approval_periods": [["2020-01-01", "2020-12-31"]],
    "denial_periods": [["2021-01-01", "2021-12-31"]],
    "checked_at": "2026-01-01T00:00:00+00:00"
  }
}}
```

Persist canonical IDs and ISO calendar dates (YYYY-MM-DD) / aware timestamps.
The loader must validate the ENTIRE schema and every record with the same
semantic constraints as put. Reject unknown versions (including bool), extra
fields, duplicate JSON keys, noncanonical stored IDs, naive timestamps, invalid
JSON/types/dates or contradictory evidence with ValueError. Do not silently
drop records, reset corrupted files, execute serialized content or overwrite
invalid existing data. Missing files alone mean an empty cache. Ordinary OS
errors propagate as OSError. Reads never modify the source file.

save explicitly updates this cache's valid JSON file, not arbitrary user files.
Use a uniquely named temporary file in the SAME parent directory, write all
JSON bytes, flush and fsync the file, then atomically publish with os.replace.
On failure propagate OSError, preserve previous destination bytes and remove
the temporary file. Never truncate the destination in place. Reject symbolic
links in the destination or any existing ancestor with ValueError on both load
and save; recheck on save, including links introduced after construction. Do not
follow links to unrelated files. A stable regular non-symlink `.organization-cache.lock` sidecar may remain in
the cache parent for cross-process serialization; no other extra files may
remain after save, including failed saves. No claim of adversarial TOCTOU protection,
multi-writer locking or directory-fsync power-loss durability is required here.

## Acceptance and review

All 85 fixed acceptance tests must pass without skips in the runner's isolated
Docker Worker. Keep implementation under product.py's 24 KiB code limit; use
simple readable Python. Tests are supplied by the task owner and are not
model-editable input. The runner uses fixed pytest arguments, read-only inputs
and checks source/test byte identity before/after tests and review.

Generated-code execution requires human approval of the exact source hash.
Do not change Live's isolation gate. No commit/push/PR/merge is authorized.
