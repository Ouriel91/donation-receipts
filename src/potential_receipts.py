"""Discover link-only receipt candidates and keep a local review workbook."""
import base64
import binascii
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import os
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.worksheet.datavalidation import DataValidation

from src.account_config import load_account_config
from src.private_runtime import atomic_bytes, file_lock, safe_path
from src.providers.gmail_auth import get_gmail_credentials
from src.providers.gmail_provider import _execute_with_rate_limit_retry, _parse_arrival_date, _parse_date

_HEADERS = ('תאריך הודעה', 'שולח', 'נושא', 'סיבת הזיהוי', 'מספר קבלה',
            'הודעת Gmail', 'סטטוס טיפול', 'מזהה הודעה')
_STATUSES = ('ממתין לבדיקה', 'להורדה', 'הורד', 'נקלט', 'כבר קיים', 'לא רלוונטי')
_STATUS_RANGE = 'G2:G1048576'
_LEGACY_STATUS_FORMULA = '"ממתין להורדה,טופל,כבר קיים במערכת,לא רלוונטי"'
_STATUS_FORMULA = '"' + ','.join(_STATUSES) + '"'
_SHEET = 'קבלות אפשריות'
_STATE = '_scan'
_SEARCH = ('{תרומה תרומות תרומתך תרומתכם תרומתנו תרומתכן התרומה לתרומה'
           ' מהתרומה ובתרומה מסמך אישור קבלה הקבלה קבלת קבלתך קבלות תרומת'
           ' donation donations donated receipt receipts}')
_DONATION = r'(?:[בלמהושכ]?תרומ(?:ה|ות|ת(?:ך|כם|כן|נו|י|ו|ה)?)|donations?|donated)'
_DOCUMENT = r"(?:[בלמהושכ]?קבל(?:ה|ת(?:ך|כם|כן|נו)?|ות)|[בלמהושכ]?מסמ(?:ך|כים)|אישור\s+(?:ה)?תרומה|receipts?|documents?|tax\s+statements?)"
_ACTION = r'(?:להורד[הת]|הורד(?:ה|ת|ו|י)?|להוריד|לצפייה|לצפות|צפ(?:ה|ו|י|ייה)|להדפסה|להדפיס|הדפס(?:ה|ת|ו|י)?|לח[ץצ](?:ו|י)?\s+כאן|download|view|print|click\s+here)'
_ISSUED = re.compile(
    r'(?:תוד[הת]\s+(?:רבה\s+)?(?:על|עבור)\s+(?:התרומה|תרומת(?:ך|כם|כן))'
    r'|תרומת(?:ך|כם|כן)\s+(?:התקבלה|נקלטה|בוצעה)'
    r'|(?:קבלתך|הקבלה|המסמך)\s+(?:שלך|מצורפ[תים]+|מוכנ[הת]|זמינ[הת])'
    r'|thank\s+you\s+for\s+(?:your\s+)?donat(?:ion|ing)|your\s+(?:donation|tax)\s+receipt)', re.I)
_FUTURE = re.compile(
    r'(?:(?:לאחר|אחרי|עם)\s+(?:ביצוע\s+)?(?:התרומה|תרומתך)'
    r'|קבל(?:ו|י)\s+קבלה|(?:תישלח|תקבל(?:ו|י)?)\s+(?:אלי\S+\s+)?קבלה'
    r'|after\s+(?:you\s+donate|donating))', re.I)
_APPEAL = re.compile(r'(?:תרמ(?:ו|י)\s+(?:עכשיו|היום|כעת)|donate\s+(?:now|today))', re.I)
_NAMED_ACCESS = re.compile(rf'(?<!\w){_ACTION}\s+(?:(?:your|the)\s+)?{_DOCUMENT}(?!\w)', re.I)
_VOID_HTML = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
              'param', 'source', 'track', 'wbr'}
_HIDDEN_STYLE = re.compile(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', re.I)
_NUMBER = re.compile(
    r"(?<!\w)[בלמהושכ]?קבל[הת](?:\s+(?:על\s+)?תרומה)?\s*(?:מספר|מס[׳'’]?|#)\s*[:#–—־-]?\s*([0-9]+)(?![\w/-]|\.\w)"
    r"|\breceipt\s*(?:number|no\.?|#)\s*[:#–—-]?\s*([0-9]+)(?![\w/-]|\.\w)", re.I)
_MARKS = re.compile(r'[\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]')
_MESSAGE_ID = re.compile(r'[0-9a-f]+')


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skipped = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skipped:
            if tag == self.skipped:
                self.skip_depth += 1
            return
        if (tag in ('head', 'style', 'script', 'template') or 'hidden' in attrs
                or (attrs.get('aria-hidden') or '').lower() == 'true'
                or (tag == 'input' and (attrs.get('type') or '').lower() == 'hidden')
                or _HIDDEN_STYLE.search(attrs.get('style') or '')):
            if tag not in _VOID_HTML:
                self.skipped = tag
                self.skip_depth = 1
            return
        if tag in ('p', 'br', 'div', 'li', 'tr', 'td', 'a', 'button'):
            self.parts.append(' ')
        if tag in ('a', 'button', 'input', 'img'):
            labels = ' '.join(attrs[key] for key in ('aria-label', 'title', 'alt', 'value') if attrs.get(key))
            if labels:
                self.parts.extend((' ', labels, ' '))

    def handle_endtag(self, tag):
        if self.skipped == tag:
            self.skip_depth -= 1
            if not self.skip_depth:
                self.skipped = None
        elif not self.skipped and tag in ('p', 'div', 'li', 'tr', 'td', 'a', 'button'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)


def _normalize(text):
    return ' '.join(_MARKS.sub('', text).split())


def _terms(pattern, text):
    return list(re.finditer(rf'(?<!\w){pattern}(?!\w)', text, re.I))


def detect(subject, body):
    text = _normalize(subject + ' ' + body)
    documents = _terms(_DOCUMENT, text)
    donations = _terms(_DONATION, text)
    actions = _terms(_ACTION, text)
    if not (documents and donations and actions):
        return None
    # Score the document and action in their local context, rather than using
    # action words from a distant footer or donation appeal.
    issued = bool(_ISSUED.search(text))
    if not issued and (_FUTURE.search(text) or (_APPEAL.search(text) and not _NAMED_ACCESS.search(text))):
        return None
    for document in documents:
        if not any(abs(donation.start() - document.start()) <= (240 if issued else 140)
                   for donation in donations):
            continue
        action = next((item for item in actions if abs(item.start() - document.start()) <= 150), None)
        if action is None:
            continue
        numbers = {match.group(1) or match.group(2) for match in _NUMBER.finditer(text)}
        reason = f'הקשר תרומה ומסמך ({document.group()}) עם פעולת גישה ({action.group()})'
        return reason, next(iter(numbers)) if len(numbers) == 1 else ''
    return None


def _mime_parts(payload, *, in_attachment=False):
    headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
    attached = (in_attachment or bool(payload.get('filename'))
                or headers.get('content-disposition', '').lower().startswith('attachment')
                or payload.get('mimeType', '').lower() == 'message/rfc822')
    yield payload, headers, attached
    for part in payload.get('parts', []):
        yield from _mime_parts(part, in_attachment=attached)


def _candidate(messages, detail):
    parts = list(_mime_parts(detail.get('payload', {})))
    if any(part.get('mimeType', '').lower() == 'application/pdf'
           or part.get('filename', '').strip().lower().endswith('.pdf') for part, _, _ in parts):
        return None
    headers = parts[0][1]
    subject = headers.get('subject', '')
    bodies = []
    for part, part_headers, attached in parts:
        mime = part.get('mimeType', '').lower()
        if attached or mime not in ('text/plain', 'text/html'):
            continue
        content = part.get('body', {})
        data = content.get('data')
        if not data and content.get('attachmentId'):
            data = _execute_with_rate_limit_retry(messages.attachments().get(
                userId='me', messageId=detail['id'], id=content['attachmentId'])).get('data')
        if not data:
            continue
        try:
            raw = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
        except (ValueError, binascii.Error):
            continue
        charset = re.search(r'charset\s*=\s*["\']?([\w.-]+)', part_headers.get('content-type', ''), re.I)
        try:
            body = raw.decode(charset.group(1) if charset else 'utf-8', errors='replace')
        except LookupError:
            body = raw.decode('utf-8', errors='replace')
        if mime == 'text/html':
            parser = _VisibleHTML()
            parser.feed(body)
            parser.close()
            body = ''.join(parser.parts)
        bodies.append(body)
    for body in bodies or [detail.get('snippet', '')]:
        result = detect(subject, body)
        if result:
            date = _parse_date(headers.get('date', '')) if headers.get('date') else (
                _parse_arrival_date(detail.get('internalDate'), ''))
            return (date, headers.get('from', ''), subject, *result)
    return None


class DiscoveryError(ValueError):
    """Controlled diagnostic code that does not include workbook or Gmail content."""


def _report_paths(reports_dir, account, year):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', account) or not 1900 <= year <= 9998:
        raise DiscoveryError('invalid_account_or_year')
    filename = f'potential_receipts_{year}.xlsx'
    root = safe_path(reports_dir)
    return (safe_path(root / account / str(year) / filename),
            safe_path(root / 'manual' / str(year) / filename))


def _report_state(workbook):
    if _STATE not in workbook or _SHEET not in workbook:
        raise DiscoveryError('unexpected_report_layout')
    state = workbook[_STATE]
    sheet = workbook[_SHEET]
    if (not isinstance(state['A1'].value, str) or not state['A1'].value
            or tuple(cell.value for cell in sheet[1][:8]) != _HEADERS):
        raise DiscoveryError('unexpected_report_layout')
    return state


def _set_status_dropdown(sheet):
    existing = None
    for validation in sheet.data_validations.dataValidation:
        if not any(area.min_col <= 7 <= area.max_col and area.max_row >= 2
                   for area in validation.sqref.ranges):
            continue
        if (existing is not None or validation.type != 'list'
                or str(validation.sqref) != _STATUS_RANGE
                or validation.formula1 not in (_LEGACY_STATUS_FORMULA, _STATUS_FORMULA)):
            raise DiscoveryError('status_validation_conflict')
        existing = validation
    if existing is not None:
        existing.formula1 = _STATUS_FORMULA
        return
    validation = DataValidation(type='list', formula1=_STATUS_FORMULA, showErrorMessage=True)
    sheet.add_data_validation(validation)
    validation.add(_STATUS_RANGE)


def _open_report(before, address, account):
    if before is not None:
        workbook = load_workbook(BytesIO(before))
        try:
            state = _report_state(workbook)
            if state['A3'].value != account or state['A1'].value != address:
                raise DiscoveryError('report_account_conflict')
        except Exception:
            workbook.close()
            raise
        return workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _SHEET
    sheet.sheet_view.rightToLeft = True
    sheet.append(_HEADERS)
    sheet.freeze_panes = 'A2'
    for column, width in zip('ABCDEFG', (16, 32, 48, 56, 16, 18, 22)):
        sheet.column_dimensions[column].width = width
    sheet.column_dimensions['H'].hidden = True
    _set_status_dropdown(sheet)
    state = workbook.create_sheet(_STATE)
    state.sheet_state = 'veryHidden'
    for coordinate, value in (('A1', address), ('A3', account)):
        state[coordinate] = value
        state[coordinate].data_type = 's'
    return workbook


def _write_report(path, workbook, before):
    output = BytesIO()
    workbook.save(output)
    original = sha256(before).digest() if before is not None else None
    current = sha256(path.read_bytes()).digest() if path.exists() else None
    if original != current:
        raise DiscoveryError('report_changed_during_operation')
    atomic_bytes(path, output.getvalue())


def relocate_report(reports_dir, account, year):
    """Move the old workbook offline, preserving legacy choices as literal values."""
    legacy, destination = _report_paths(reports_dir, account, year)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(destination.with_suffix('.lock')):
        # The old release uses this lock. Hold both while updating and moving it.
        if legacy != destination and legacy.parent.is_dir():
            with file_lock(legacy.with_suffix('.lock')):
                return _relocate_locked(legacy, destination, account)
        return _relocate_locked(legacy, destination, account)


def _relocate_locked(legacy, destination, account):
    if legacy != destination and legacy.exists() and destination.exists():
        if not legacy.samefile(destination):
            raise DiscoveryError('relocation_destination_exists')
        # A previous no-clobber link succeeded before its source could be removed.
        with closing(load_workbook(destination)) as workbook:
            if _report_state(workbook)['A3'].value != account:
                raise DiscoveryError('report_account_conflict')
        legacy.unlink()
        return 'relocated'
    source = legacy if legacy.exists() else destination
    if not source.is_file():
        raise DiscoveryError('legacy_report_missing')
    before = source.read_bytes()
    with closing(load_workbook(BytesIO(before))) as workbook:
        state = _report_state(workbook)
        owner = state['A3'].value
        if owner == account and source == destination:
            return 'already_relocated'
        if owner not in (None, account) or (source == destination and legacy != destination):
            raise DiscoveryError('report_account_conflict')
        state['A3'] = account
        state['A3'].data_type = 's'
        _set_status_dropdown(workbook[_SHEET])
        _write_report(source, workbook, before)
    if source != destination:
        # Same-volume exclusive publication. An existing destination makes
        # link fail even if it appeared after the check above.
        try:
            os.link(source, destination)
        except FileExistsError:
            raise DiscoveryError('relocation_destination_exists') from None
        source.unlink()
    return 'relocated'


def _legacy_reports(destination):
    root = destination.parents[2]
    for folder in root.iterdir():
        if folder != destination.parents[1]:
            candidate = folder / destination.parent.name / destination.name
            if candidate.is_file():
                yield candidate


def _scoped_report(before, address, account):
    """Upgrade the accepted single-account queue without changing user cells."""
    if before is None:
        workbook = _open_report(None, address, account)
    else:
        workbook = load_workbook(BytesIO(before))
    try:
        state = _report_state(workbook)
        sheet = workbook[_SHEET]
        if state['D1'].value is None:
            legacy_account = state['A3'].value
            if not isinstance(legacy_account, str) or not legacy_account:
                raise DiscoveryError('unexpected_report_layout')
            state['B1'] = state['A2'].value
            state['C1'] = legacy_account
            state.delete_rows(2, 2)
            for row in range(2, sheet.max_row + 1):
                sheet.cell(row, 9, legacy_account).data_type = 's'
            sheet.cell(1, 9, 'Source account')
            state['D1'] = 'account-checkpoints-v1'
        for row in range(1, state.max_row + 1):
            if state.cell(row, 3).value == account:
                if state.cell(row, 1).value != address:
                    raise DiscoveryError('report_account_conflict')
                return workbook, state.cell(row, 2)
        row = state.max_row + 1
        state.cell(row, 1, address).data_type = 's'
        state.cell(row, 3, account).data_type = 's'
        return workbook, state.cell(row, 2)
    except Exception:
        workbook.close()
        raise


def _scan(messages, query, sheet, address, account=None):
    seen = {key for key, source in sheet.iter_rows(min_row=2, min_col=8, max_col=9, values_only=True)
            if source == account}
    added = 0
    page = {}
    while True:
        response = _execute_with_rate_limit_retry(messages.list(
            userId='me', q=query, includeSpamTrash=False, **page))
        for item in response.get('messages', []):
            key = item['id']
            if not _MESSAGE_ID.fullmatch(key) or key in seen:
                continue
            seen.add(key)
            try:
                detail = _execute_with_rate_limit_retry(messages.get(userId='me', id=key, format='full'))
            except HttpError as exc:
                if exc.resp.status == 404:
                    continue
                raise
            if detail.get('id') != key:
                raise ValueError('Gmail message identity changed')
            candidate = _candidate(messages, detail)
            if candidate:
                sheet.append([*candidate, 'פתיחה ב־Gmail', _STATUSES[0], key])
                if account is not None:
                    sheet.cell(sheet.max_row, 9, account)
                for cell in sheet[sheet.max_row][:9 if account else 8]:
                    cell.value = ILLEGAL_CHARACTERS_RE.sub('', cell.value)
                    cell.data_type = 's'
                thread = detail.get('threadId', '')
                anchor = thread if _MESSAGE_ID.fullmatch(thread) else key
                sheet.cell(sheet.max_row, 6).hyperlink = (
                    f'https://mail.google.com/mail/u/?authuser={quote(address, safe="")}#all/{anchor}')
                sheet.cell(sheet.max_row, 6).style = 'Hyperlink'
                added += 1
        token = response.get('nextPageToken')
        if not token:
            return added
        page = {'pageToken': token}


def run_discovery(accounts_dir, reports_dir, account, year, *, delta=False, now=None, scoped=False):
    legacy, path = _report_paths(reports_dir, account, year)
    account_dir = safe_path(accounts_dir / account)
    if not account_dir.is_dir():
        raise DiscoveryError('configured_account_directory_missing')
    config = load_account_config(accounts_dir, account)
    zone = ZoneInfo(config.operating_timezone if config else 'Asia/Jerusalem')
    now = now if now is not None else datetime.now(timezone.utc)
    start = datetime(year, 1, 1, tzinfo=zone)
    end = min(datetime(year + 1, 1, 1, tzinfo=zone), now)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix('.lock')):
        if ((legacy != path and legacy.exists())
                or (not path.exists() and any(_legacy_reports(path)))):
            raise DiscoveryError('legacy_report_requires_relocation')
        before = path.read_bytes() if path.exists() else None
        if before is not None and not scoped:
            with closing(load_workbook(BytesIO(before))) as workbook:
                if _report_state(workbook)['A3'].value != account:
                    raise DiscoveryError('report_account_conflict')
        expected_address = None
        if scoped:
            from src.user_scope import mailbox
            binding = safe_path(account_dir / 'mailbox.txt')
            expected_address = mailbox(binding.read_text(encoding='utf-8').strip())
        service = build('gmail', 'v1', credentials=get_gmail_credentials(account_dir))
        users = service.users()
        address = _execute_with_rate_limit_retry(users.getProfile(userId='me'))['emailAddress']
        if scoped:
            if mailbox(address) != expected_address:
                raise DiscoveryError('gmail_profile_mismatch')
            workbook, checkpoint_cell = _scoped_report(before, address, account)
        else:
            workbook = _open_report(before, address, account)
            checkpoint_cell = workbook[_STATE]['A2']
        with closing(workbook):
            checkpoint = checkpoint_cell.value
            if checkpoint is not None:
                try:
                    stamp = datetime.fromisoformat(checkpoint)
                    if stamp.utcoffset() is None or stamp > now:
                        raise ValueError
                except (ValueError, TypeError):
                    raise DiscoveryError('invalid_report_checkpoint') from None
                if delta:
                    start = max(start, stamp - timedelta(hours=48))
            sheet = workbook[_SHEET]
            added = 0
            if start < end:
                query = f'after:{int(start.timestamp()) - 1} before:{int(end.timestamp())} {_SEARCH}'
                added = _scan(users.messages(), query, sheet, address, account if scoped else None)
                checkpoint_cell.value = end.isoformat()
            last_column = 'I' if scoped else 'H'
            sheet.auto_filter.ref = f'A1:{last_column}{sheet.max_row}'
            if added or before is None or checkpoint != checkpoint_cell.value:
                _write_report(path, workbook, before)
    return added
