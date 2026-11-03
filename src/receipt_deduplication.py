"""Conservative annual receipt consolidation; no file access or modification."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from src.receipt_metadata_extractor import ReceiptMetadata, _is_valid_corporate_registration_number


COMPLEMENTARY_FIELDS = ('organization_name', 'tax_report_number', 'donor_name', 'donor_id')


@dataclass
class Donation:
    metadata: ReceiptMetadata
    sources: tuple[Path, ...]
    suspected_group: int | None = None
    filled_fields: int = 0


def receipt_amount(value: str) -> Decimal | None:
    """Use exact decimal equality and totals, never float equality for grouping."""
    try:
        amount = Decimal(re.sub(r'[₪,\s]', '', value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _identity(meta: ReceiptMetadata) -> tuple:
    registration = meta.registration_number.strip()
    if not _is_valid_corporate_registration_number(registration):
        registration = None
    try:
        date = datetime.strptime(meta.receipt_date, '%d/%m/%Y').date()
    except ValueError:
        date = None
    # Do not cast identifiers to numbers or remove separators/leading zeros.
    return registration, meta.receipt_number.strip() or None, date, receipt_amount(meta.amount)


def _possibly_same(a: tuple, b: tuple) -> bool:
    same = [x is not None and y is not None and x == y for x, y in zip(a, b)]
    if same[1]:
        # Org + receipt identify a conflict even when date/amount differ.
        # Receipt + date + amount also warrant review of missing/different orgs.
        return same[0] or (same[2] and same[3])
    # A missing receipt ID is insufficient for merging, but worth reviewing.
    return same[0] and same[2] and same[3] and (a[1] is None or b[1] is None)


def _consistent_optional_fields(metas: list[ReceiptMetadata]) -> bool:
    return all(len({' '.join(getattr(meta, field).split()) for meta in metas
                    if getattr(meta, field).strip()}) <= 1
               for field in COMPLEMENTARY_FIELDS)


def consolidate_receipts(records: list[tuple[Path, ReceiptMetadata]]) -> list[Donation]:
    """Require a complete, consistent identity across the entire candidate group.

    Incomplete or conflicting bridge records block automatic consolidation of
    that group. This deliberately prefers manual review over a false merge.
    Input order determines the primary source (earliest scanned month/name).
    """
    keys = [_identity(meta) for _, meta in records]
    neighbors = [set() for _ in records]
    for i, a in enumerate(keys):
        for j in range(i + 1, len(keys)):
            if _possibly_same(a, keys[j]):
                neighbors[i].add(j)
                neighbors[j].add(i)

    visited = set()
    donations = []
    for first in range(len(records)):
        if first in visited:
            continue
        pending, component = [first], set()
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            pending.extend(neighbors[index] - component)
        visited.update(component)
        indices = sorted(component)
        metas = [records[i][1] for i in indices]
        complete_match = (all(value is not None for value in keys[first])
                          and all(keys[i] == keys[first] for i in indices)
                          and _consistent_optional_fields(metas))
        if len(indices) == 1 or not complete_match:
            for i in indices:
                donations.append(Donation(replace(records[i][1]), (records[i][0],),
                                          suspected_group=first + 1 if len(indices) > 1 else None))
            continue

        merged = replace(metas[0])
        filled = 0
        for field in COMPLEMENTARY_FIELDS:
            if not getattr(merged, field).strip():
                replacement = next((getattr(meta, field) for meta in metas
                                    if getattr(meta, field).strip()), '')
                setattr(merged, field, replacement)
                filled += bool(replacement)
        merged.notes = '; '.join(dict.fromkeys(note for meta in metas
                                              for note in meta.notes.split('; ') if note))
        donations.append(Donation(merged, tuple(records[i][0] for i in indices), filled_fields=filled))
    # Components may interleave; retain the original scan order in the workbook.
    order = {path: index for index, (path, _) in enumerate(records)}
    donations.sort(key=lambda donation: order[donation.sources[0]])
    return donations
