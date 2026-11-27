"""User-private incremental intake for independently obtained PDF receipts."""
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import re

from src import receipt_summary
from src.attachment_saver import MONTH_NAMES
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.private_runtime import atomic_json, new_bytes, operational_lock, safe_path
from src.receipt_deduplication import consolidate_receipts, receipt_amount
from src.pdf_donation_validator import donation_text_evidence
from src.receipt_metadata_extractor import (IDENTIFIER_REVIEW_NOTES, INSTITUTION_AMOUNT_REVIEW, ReceiptMetadata, _AMOUNT_DATE_TOKEN,
                                           _AMOUNT_TOTAL, _date_candidates, _PAYMENT_HEADING, _PAYMENT_REPORTING_FIELD, _without_full_tax_fields,
                                           receipt_amounts_in_text, receipt_numbers_in_text,
                                           has_receipt_heading, institution_receipt_numbers, institution_amount_candidates, extract_metadata)
from src.sync_state import _refresh_identifiers, digest, donation_date, effective_metadata
from src.user_scope import annual_report

_RECEIPT = re.compile(r'\b(?:קבל[הת]|הלבק|receipt)\b', re.IGNORECASE)
_SUMMARY = re.compile(r'(?:סיכום|ריכוז|פירוט|אישור)\s+(?:שנתי\s+)?(?:של\s+)?(?:ה)?תרומות|'
                      r'אישור\s+שנתי|דוח\s+שנתי|annual\s+(?:(?:donations?|tax)\s+)?(?:summary|statement|report)|'
                      r'donations?\s+(?:summary|statement)', re.IGNORECASE)


def _state(path):
    state = json.loads(path.read_bytes()) if path.exists() else {'version': 1, 'documents': {}}
    if state.get('version') != 1 or not isinstance(state.get('documents'), dict):
        raise ValueError('Invalid manual receipt catalog')
    return state


_RIVHIT_DATE = re.compile(r'(?<![0-9./-])([0-9]{1,2})[./]([0-9]{1,2})[./]([0-9]{4})(?![0-9./-])')
_RIVHIT_RECEIPT_DATE = re.compile(r'תאריך[ \t]+(?:קבלה|תרומה|מסמך)|ךיראת[ \t]+(?:הלבק|המורת|ךמסמ)')
_RIVHIT_OTHER_DATE = re.compile(
    r'תאריך[ \t]+(?:הדפסה|הפקה|פ[י]?רעון)|ךיראת[ \t]+(?:הספדה|הקפה|ןוער[י]?פ)'
)
_FILENAME_DATE_NOTE = 'תאריך מתוך שם הקובץ'


def _rivhit_dates(texts):
    """Use a labelled receipt date, otherwise require one non-auxiliary date."""
    dates, explicit = set(), set()
    for text in texts:
        in_payment = False
        for line in text.splitlines():
            if _PAYMENT_HEADING.search(line):
                in_payment = True
            if _PAYMENT_REPORTING_FIELD.search(line):
                in_payment = False
            matches = list(_RIVHIT_DATE.finditer(line))
            for match in matches:
                day, month, year = map(int, match.groups())
                try:
                    value = date(year, month, day)
                except ValueError:
                    continue
                # Only assign a line label if its date is the sole token.
                if len(matches) == 1:
                    if _RIVHIT_RECEIPT_DATE.search(line) and not _RIVHIT_OTHER_DATE.search(line):
                        explicit.add(value)
                    elif _RIVHIT_OTHER_DATE.search(line) and not _RIVHIT_RECEIPT_DATE.search(line):
                        continue
                if not in_payment:
                    dates.add(value)
    return explicit or dates


def _inspect(path, raw, year, evidence=None, *, source_name=None):
    empty = ReceiptMetadata(file_name=source_name or path.name, account='manual')
    if not raw.startswith(b'%PDF-'):
        return empty, 'invalid_pdf'
    backend_text = {}
    for backend, reader in (('pypdf', receipt_summary.extract_text_from_pdf),
                            ('pymupdf', receipt_summary.extract_text_from_pdf_pymupdf)):
        try:
            text = reader(path)
        except receipt_summary.PdfExtractionError:
            continue
        if text:
            backend_text[backend] = text
    raw_texts = list(backend_text.values())
    texts = [variant for raw_text in raw_texts
             for variant in (raw_text, normalize_hebrew_text(raw_text))]
    text = '\n'.join(texts)
    document_reason = None
    if _SUMMARY.search(text):
        document_reason = 'annual_reference_document'
    elif not any(donation_text_evidence(raw_text)[0] for raw_text in raw_texts):
        document_reason = 'unsupported_document'
    if document_reason and evidence is None:
        return empty, document_reason
    meta = receipt_summary._extract_row(path, 'manual', None, year, source_name=source_name)
    if path.read_bytes() != raw:
        raise ValueError('Manual source changed during intake')

    # The extractor distinguishes conflicts within receipt and tax fields.
    # Broad historical scope scans are diagnostic, not single-receipt proof.
    notes = set(meta.notes.split('; '))
    receipt_conflict = IDENTIFIER_REVIEW_NOTES['receipt_number'] in notes
    tax_conflict = IDENTIFIER_REVIEW_NOTES['tax_report_number'] in notes
    receipt_texts = [_without_full_tax_fields(value) for value in texts]
    has_receipt_label = any(_RECEIPT.search(value) for value in receipt_texts)
    receipt_heading = any(has_receipt_heading(value) for value in receipt_texts)
    dates = _date_candidates(text)
    if meta.full_source_serial and meta.payment_total_verified:
        dates = _rivhit_dates(texts)
        if len(dates) == 1:
            meta.receipt_date = next(iter(dates)).strftime('%d/%m/%Y')
            meta.notes = '; '.join(note for note in meta.notes.split('; ')
                                   if note != _FILENAME_DATE_NOTE)
        else:
            meta.receipt_date = ''
    date_from_filename = _FILENAME_DATE_NOTE in meta.notes
    broad_amounts = {receipt_amount(value) for value in receipt_amounts_in_text('\n'.join(raw_texts))} - {None}
    totals = {receipt_amount(match.group(1)) for value in texts
              for match in _AMOUNT_TOTAL.finditer(_AMOUNT_DATE_TOKEN.sub('|', value))} - {None}
    amount = receipt_amount(meta.amount)
    amount_conflict = not meta.payment_total_verified and (totals != {amount} if totals else len(broad_amounts) > 1)
    if evidence is not None:
        evidence['extractors'] = backend_evidence(backend_text, source_name or path.name)
        if meta.full_source_serial:
            evidence['institution_amount_candidates'] = [str(value) for value in sorted(
                institution_amount_candidates(*texts))]
        from src.sync_workbook import receipt_source_evidence

        historical = receipt_source_evidence(path, meta)
        if historical['sha256'] != digest(raw):
            raise ValueError('Manual source changed during diagnostic')
        numbers = historical['receipt_numbers']
        excluded = {meta.registration_number, meta.donor_id} - {''}
        evidence.update(old_receipt_set_mismatch=numbers != [meta.receipt_number],
                        old_multiple_amounts=historical['multiple_amounts'],
                        old_identifier_note=any(note in meta.notes for note in IDENTIFIER_REVIEW_NOTES.values()),
                        broad_receipt_candidates=len(numbers), receipt_conflict=receipt_conflict,
                        tax_conflict=tax_conflict, receipt_number_present=bool(meta.receipt_number),
                        tax_confirmation_present=bool(meta.tax_report_number),
                        receipt_label=has_receipt_label, receipt_heading=receipt_heading, date_candidates=len(dates),
                        date_from_filename=date_from_filename, valid_receipt_date=donation_date(meta) is not None,
                        amount_candidates=len(broad_amounts), total_candidates=len(totals),
                        amount_conflict=amount_conflict,
                        tax_in_broad_scan=bool(meta.tax_report_number) and any(
                            meta.tax_report_number in receipt_numbers_in_text(value, excluded - {meta.tax_report_number})
                            for value in texts))
    if document_reason:
        return empty, document_reason
    if receipt_conflict:
        return meta, 'ambiguous_receipt_identity'
    if not has_receipt_label or (not meta.receipt_number and not receipt_heading):
        return meta, 'unsupported_document'
    if not meta.registration_number and not meta.organization_name:
        return meta, 'organization_review'
    if amount_conflict or INSTITUTION_AMOUNT_REVIEW in notes:
        return meta, 'ambiguous_donation_amount'
    if (len(dates) != 1 or date_from_filename
            or donation_date(meta) is None or amount is None):
        return meta, 'missing_or_ambiguous_date_or_amount'
    return meta, None


def _store(path, raw):
    try:
        new_bytes(path, raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError('Manual receipt storage collision') from None


def _source(base, relative):
    path = safe_path(base / relative)
    if not path.is_relative_to(base):
        raise ValueError('Manual receipt path escaped user scope')
    return path


def run_manual(scope, year, *, fresh=False, decisions=None, retry_review=False):
    if not 1900 <= year <= 9998:
        raise ValueError('Invalid manual report year')
    scope.require()
    base = safe_path(scope.receipts / 'manual')
    inbox = safe_path(base / 'inbox')
    review = safe_path(base / 'review')
    inbox.mkdir(parents=True, exist_ok=True)
    review.mkdir(parents=True, exist_ok=True)
    intake_path = safe_path(scope.runtime / 'manual-intake.json')
    catalog_path = safe_path(scope.runtime / 'sync-manual.json')
    with operational_lock(scope.runtime):
        intake, catalog = _state(intake_path), _state(catalog_path)
        if decisions:
            _apply_review(scope, base, year, intake, intake_path, catalog, catalog_path, decisions)
        _recover_catalog(intake, catalog, catalog_path)
        retried = _retry_review(base, year, intake, intake_path, catalog) if retry_review else 0
        processed, duplicates = 0, 0
        for source in sorted(inbox.iterdir()):
            if source.suffix.lower() != '.pdf' or not safe_path(source).is_file():
                continue
            identity = _submission_identity(source)
            raw = source.read_bytes()
            key = digest(raw)
            known = intake['documents'].get(key)
            if known is None:
                meta, reason = _inspect(source, raw, year)
                donated = donation_date(meta)
                duplicate = _matching_manual(key, meta, intake, catalog) if not reason else None
                if duplicate:
                    _verify_duplicate_owner(base, duplicate, intake, catalog)
                    owner = catalog['documents'].get(duplicate) or intake['documents'][duplicate]
                    from src.receipt_deduplication import _identity
                    from src.sync_workbook import verified_copy_source
                    old_paths = [_source(base, value) for value in owner['paths']]
                    old_paths = [p for p in old_paths if p.is_file() and digest(p.read_bytes()) == duplicate]
                    incoming = {'metadata': asdict(meta), 'attachment_origins': [source.name]}
                    pair = [(duplicate, old_paths[0], 'manual', owner),
                            (key, source, 'manual', incoming)] if len(old_paths) == 1 else []
                    from src.manual_exclusion import excluded_publications
                    owner_id = owner.get('logical_id')
                    owner_docs = {sha for sha, entry in catalog['documents'].items()
                                  if owner_id and entry.get('logical_id') == owner_id} or {duplicate}
                    if (len(owner_docs) == 1 and owner_id not in excluded_publications(catalog, donated.year)
                            and verified_copy_source(pair, _identity(meta)) is not None):
                        duplicate = None  # Existing grouping owns both, no review bypass without proof.
                    else:
                        reason = 'duplicate_manual_receipt'
                folder = review if reason else safe_path(base / str(donated.year) / MONTH_NAMES[donated.month])
                target = safe_path(folder / f'{key}.pdf')
                _store(target, raw)
                known = {'metadata': asdict(meta), 'paths': [target.relative_to(base).as_posix()],
                         'logical_id': None, 'stage': 'review' if reason else 'extracted',
                         'inbox_submission': identity, 'table_amount_revision': 1}
                if reason:
                    known['reason'] = reason
                if duplicate:
                    known['duplicate_of'] = duplicate
                    duplicates += 1
                intake['documents'][key] = known
                atomic_json(intake_path, intake)
                processed += 1
            else:
                if not any(_source(base, value).is_file() and _source(base, value).read_bytes() == raw
                           for value in known['paths']):
                    raise ValueError('Stored manual original is missing or changed')
                if known['stage'] == 'extracted' and known.get('inbox_submission') != identity:
                    duplicates += 1
                    duplicate_path = safe_path(review / f'{key}.duplicate.pdf')
                    _store(duplicate_path, raw)
                    intake.setdefault('duplicates', {})[key] = {
                        'stage': 'review', 'reason': 'duplicate_manual_receipt',
                        'metadata': {'file_name': source.name},
                        'paths': [duplicate_path.relative_to(base).as_posix()], 'duplicate_of': key,
                    }
                    atomic_json(intake_path, intake)
            if _submission_identity(source) != identity or source.read_bytes() != raw:
                raise ValueError('Manual inbox file changed during intake')
            source.unlink()

        _recover_catalog(intake, catalog, catalog_path)
        cleaned = _reconcile_review(base, intake, catalog)
        if fresh:
            for key, entry in catalog['documents'].items():
                meta = effective_metadata(entry)
                if donation_date(meta).year != year:
                    continue
                path = _source(base, entry['paths'][0])
                raw = path.read_bytes()
                if digest(raw) != key:
                    raise ValueError('Stored manual receipt changed')
                refreshed, reason = _inspect(path, raw, year, source_name=entry['metadata']['file_name'])
                if refreshed.full_source_serial and not entry['metadata'].get('full_source_serial'):
                    entry['metadata']['full_source_serial'] = refreshed.full_source_serial
                if reason:
                    entry['fresh_review_reason'] = reason
                else:
                    entry.pop('fresh_review_reason', None)
                    _refresh_identifiers(entry, refreshed, year)
            atomic_json(catalog_path, catalog)
        from src.sync_state import stage_saved_table_amounts
        stage_saved_table_amounts(catalog, scope.receipts, 'manual', None, {year})
        atomic_json(catalog_path, catalog)
        from src.sync import publish

        review_items = {**intake['documents']}
        for key, entry in catalog['documents'].items():
            if entry.get('fresh_review_reason'):
                review_items[key] = {**entry, 'stage': 'review', 'reason': entry['fresh_review_reason']}
        review_items.update({f'{key}:duplicate': entry
                             for key, entry in intake.get('duplicates', {}).items()})
        counts = publish(scope.receipts, scope.reports, scope.accounts, 'manual', year,
                         scope.runtime, public_registry=scope.registry, user=scope.user,
                         identity_scope=f'{scope.user}/manual', prepared_catalog=(catalog_path, catalog),
                         manual_review=review_items)
        counts['manual_review'] = sum(item['stage'] == 'review' for item in review_items.values())
        counts['manual_review_copies_removed'] = cleaned
        counts.update(manual_ingested=processed, manual_duplicates=duplicates, manual_review_promoted=retried)
        return counts


def _apply_review(scope, base, year, intake, intake_path, catalog, catalog_path, path):
    from src.sync_review import apply_decisions

    path = safe_path(path)
    if not path.is_relative_to(scope.runtime):
        raise ValueError('Place review decisions in the selected user private runtime')
    decisions = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(decisions, list):
        raise ValueError('Manual review needs a list of decisions')
    for decision in decisions:
        entry = intake['documents'][decision['document_sha256']]
        action = decision['action']
        reason = entry.get('reason')
        correctable = (reason == 'missing_or_ambiguous_date_or_amount'
                       or (reason == 'ambiguous_donation_amount' and 'amount' in decision))
        if action != 'not-donation' and not (action == 'correct-fields' and correctable):
            raise ValueError('The selected manual PDF cannot be accepted by a date or amount correction')
    apply_decisions(intake, decisions, annual_report(scope.reports, 'manual', year), scope.receipts, 'manual', year)
    for decision in decisions:
        if decision['action'] != 'correct-fields':
            continue
        key = decision['document_sha256']
        entry = intake['documents'][key]
        meta = effective_metadata(entry)
        date = donation_date(meta)
        if not date or receipt_amount(meta.amount) is None:
            raise ValueError('A verified date and amount are required')
        original = _source(base, entry['paths'][0])
        target = safe_path(base / str(date.year) / MONTH_NAMES[date.month] / f'{key}.pdf')
        raw = original.read_bytes()
        if digest(raw) != key:
            raise ValueError('Manual review source changed')
        _store(target, raw)
        entry['paths'] = [target.relative_to(base).as_posix()]
    atomic_json(intake_path, intake)
    _recover_catalog(intake, catalog, catalog_path)


def _recover_catalog(intake, catalog, catalog_path):
    changed = False
    for key, entry in intake['documents'].items():
        if entry['stage'] == 'extracted' and key not in catalog['documents']:
            item = dict(entry)
            meta = effective_metadata(item)
            _refresh_identifiers(item, meta, donation_date(meta).year)
            catalog['documents'][key] = item
            changed = True
    if changed:
        atomic_json(catalog_path, catalog)


def write_review(wb, receipts_base, documents):
    title = 'בדיקת מסמכים ידניים'
    if title in wb:
        wb.remove(wb[title])
    ws = wb.create_sheet(title)
    ws.sheet_view.rightToLeft = True
    ws.append(['SHA-256', 'קובץ מקורי', 'מצב בדיקה', 'מסמך', 'SHA-256 של קבלה קיימת'])
    from src.report_layout import _local_pdf_address
    for key, entry in sorted(documents.items()):
        if entry['stage'] == 'extracted':
            continue
        source = _source(receipts_base, entry['paths'][0])
        ws.append([key.split(':', 1)[0], entry['metadata']['file_name'],
                   entry.get('reason', entry['stage']), source.name, entry.get('duplicate_of')])
        ws.cell(ws.max_row, 2).data_type = 's'
        ws.cell(ws.max_row, 4).hyperlink = _local_pdf_address(source)


def _review_queue(intake):
    for key, entry in sorted(intake['documents'].items()):
        if (entry['stage'] == 'review' and entry.get('reason') in {'ambiguous_receipt_identity', 'unsupported_document', 'ambiguous_donation_amount'}
                and not entry.get('review_decision') and not entry.get('review_history')):
            yield key, entry


def _read_review_pdf(base, key, entry):
    path = _source(base, entry['paths'][0])
    raw = path.read_bytes()
    if digest(raw) != key:
        raise ValueError('Stored manual review PDF changed')
    return path, raw


def diagnose_review(scope, year):
    """Read-only per-document predicates. Include extracted identifiers locally, without PDF text or donor fields."""
    scope.require()
    base = safe_path(scope.receipts / 'manual')
    intake = _state(safe_path(scope.runtime / 'manual-intake.json'))
    selected = list(_review_queue(intake))
    selected.extend((key, entry) for key, entry in sorted(intake['documents'].items())
                    if entry['stage'] == 'extracted'
                    and (date := donation_date(effective_metadata(entry))) and date.year == year)
    for key, entry in selected:
        evidence = {}
        try:
            path, raw = _read_review_pdf(base, key, entry)
            meta, reason = _inspect(path, raw, year, evidence=evidence, source_name=entry['metadata']['file_name'])
            evidence['merged'] = {field: getattr(meta, field) for field in (
                'receipt_number', 'full_source_serial', 'tax_report_number', 'tax_confirmation_candidate',
                'registration_number', 'receipt_date', 'amount')}
            if entry['stage'] == 'extracted':
                from src.sync_state import refresh_selected_identifiers
                try:
                    preview, = refresh_selected_identifiers(
                        scope.receipts, scope.reports, scope.accounts, 'manual',
                        scope.runtime, year, [key], preview=True)
                    evidence['publication_id'] = preview['publication_id']
                    evidence['published'] = preview['published']
                    evidence['proposals'] = preview['proposals']
                    evidence['protected_fields'] = preview['protected_fields']
                except (ValueError, OSError) as exc:
                    evidence['preview_issue'] = getattr(exc, 'code', type(exc).__name__)
        except (ValueError, OSError):
            reason = 'diagnostic_source_unavailable_or_changed'
        yield {'document_sha256': key, 'sha256_prefix': key[:16], 'stored_reason': entry.get('reason', 'accepted'),
               **evidence, 'result': reason or 'publishable'}


def _retry_review(base, year, intake, intake_path, catalog):
    promoted = 0
    for key, entry in _review_queue(intake):
        path, raw = _read_review_pdf(base, key, entry)
        meta, reason = _inspect(path, raw, year, source_name=entry['metadata']['file_name'])
        entry['metadata'] = asdict(meta)
        duplicate = _matching_manual(key, meta, intake, catalog) if not reason else None
        if duplicate:
            _verify_duplicate_owner(base, duplicate, intake, catalog)
            reason = 'duplicate_manual_receipt'
            entry['duplicate_of'] = duplicate
        if reason:
            entry['reason'] = reason
        else:
            donated = donation_date(meta)
            target = safe_path(base / str(donated.year) / MONTH_NAMES[donated.month] / f'{key}.pdf')
            _store(target, raw)
            entry['paths'] = [target.relative_to(base).as_posix()]
            entry['stage'] = 'extracted'
            entry.pop('reason')
            promoted += 1
        atomic_json(intake_path, intake)
    return promoted


def backend_evidence(texts, name):
    """Compact field presence and layout evidence for each PDF reader."""
    result = {}
    for backend in ('pypdf', 'pymupdf'):
        raw = texts.get(backend, '')
        normal = normalize_hebrew_text(raw)
        extracted = extract_metadata(normal, Path(name), raw_text=raw)
        result[backend] = {
            'has_text': bool(raw),
            'donation_signal': donation_text_evidence(raw)[0],
            'receipt_heading_raw': has_receipt_heading(raw),
            'receipt_heading_normalized': has_receipt_heading(normal),
            'institution_serial_raw': bool(institution_receipt_numbers(raw)),
            'institution_serial_normalized': bool(institution_receipt_numbers(normal)),
            'receipt_number': extracted.receipt_number,
            'full_source_serial': extracted.full_source_serial,
            'tax_confirmation': extracted.tax_report_number,
            'tax_confirmation_candidate': extracted.tax_confirmation_candidate,
            'organization_identifier_present': bool(extracted.registration_number),
            'date_present': bool(extracted.receipt_date),
            'amount_present': bool(extracted.amount),
        }
    return result


def _matching_manual(key, meta, intake, catalog):
    """Use the existing full-identity consolidation rule, without merging sources."""
    from src.receipt_deduplication import _identity

    identity = _identity(meta)
    if None in identity:
        return None
    owners = {}
    for sha, item in intake['documents'].items():
        if sha == key or item['stage'] != 'extracted':
            continue
        accepted = catalog['documents'].get(sha, item)
        if accepted.get('review_decision', {}).get('action') == 'distinct':
            continue
        previous = effective_metadata(accepted)
        if _identity(previous) != identity:
            continue
        if len(consolidate_receipts([(Path(sha), previous), (Path(key), meta)])) == 1:
            owners.setdefault(accepted.get('logical_id') or sha, sha)
    return next(iter(owners.values())) if len(owners) == 1 else None


def _verify_duplicate_owner(base, key, intake, catalog):
    entry = catalog['documents'].get(key, intake['documents'][key])
    _read_review_pdf(base, key, entry)


def _reconcile_review(base, intake, catalog):
    """Clean only known legacy review copies after the annual catalog is durable."""
    referenced = {relative for state in (intake, catalog)
                  for field in ('documents', 'duplicates')
                  for entry in state.get(field, {}).values() for relative in entry['paths']}
    removed = 0
    for key, entry in intake['documents'].items():
        if entry['stage'] != 'extracted' or key not in catalog['documents']:
            continue
        relative = f'review/{key}.pdf'
        if relative in referenced:
            continue
        review_path = _source(base, relative)
        if not review_path.is_file():
            continue
        accepted = catalog['documents'][key]
        date = donation_date(effective_metadata(accepted))
        if not date:
            continue
        stored = f'{date.year}/{MONTH_NAMES[date.month]}/{key}.pdf'
        if stored not in entry['paths'] or stored not in accepted['paths']:
            continue
        destination = _source(base, stored)
        if (not destination.is_file() or digest(destination.read_bytes()) != key
                or digest(review_path.read_bytes()) != key):
            continue
        review_path.unlink()
        removed += 1
    return removed


def _submission_identity(path):
    stat = path.stat()
    return [path.name, stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size]
