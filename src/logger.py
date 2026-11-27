"""Private full-run traceback and aggregate progress; inert outside a run context."""
from contextvars import ContextVar
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import traceback
from uuid import uuid4

from src.private_runtime import safe_path

_active = ContextVar('donation_full_diagnostics', default=None)
STAGES = {'START', 'CONFIG', 'GMAIL', 'DOWNLOAD', 'EXTRACTION', 'WORKBOOK',
          'DEDUP', 'REGISTRY', 'SECTION46', 'EXCEL', 'PUBLISH', 'DONE', 'PLAN'}
COUNTERS = {'documents_reused','classified_best','classified_good','classified_denied','classified_review','classified_manual','links_fixed', 'links_not_found', 'links_conflicts', 'month', 'pages', 'messages_found', 'messages_total', 'messages_scanned',
            'details_completed', 'new_files', 'existing_files', 'failed_files',
            'rejected_files', 'skipped_messages', 'rejected_messages', 'source_pdfs_scanned',
            'documents_extracted', 'dates_recovered', 'catalog_documents', 'merged_groups',
            'merged_copies', 'suspected_groups', 'organizations_total',
            'organizations_completed', 'registry_matches', 'rows_enriched',
            'approved_rows', 'denied_rows', 'other_status_rows', 'reports_updated', 'published',
            'pending_date', 'cross_year', 'api_requests', 'api_errors', 'cache_hits',
            'attempt', 'delay_seconds', 'historical_review_rows',
            'historical_review_groups', 'unresolved_historical_document_groups', 'archive_proof_review_rows',
            'rejected_attachments', 'identifier_documents_refreshed', 'correction_proposals_created',
            'correction_proposals_pending',
            'section46_decision_valid', 'section46_write_blocked', 'section46_evidence_invalid',
            'section46_evidence_missing', 'section46_input_invalid', 'section46_companions_preserved', 'corrections_published'}
SAFE_REASONS = {
    'Physical receipt linked to multiple historical donations': 'historical_ownership_conflict',
    'Donation published in multiple tax-year workbooks': 'cross_year_publication_conflict',
    'Existing operational registry required': 'registry_missing',
    'Workbook round-trip verification failed': 'workbook_roundtrip_verification',
    'Workbook changed during sync; close Excel before retry': 'workbook_changed',
    'Backup verification failed': 'backup_verification',
    'Invalid sync catalog; restore locally before retry': 'catalog_invalid',
    'Formula donation amount requires an explicit value before sync': 'amount_formula_review',
    'Attachment download failures; rerun required': 'attachment_retry_required',
}
SAFE_TYPES = {'ValueError', 'RuntimeError', 'OSError', 'PermissionError',
              'FileNotFoundError', 'TimeoutError', 'ConnectionError', 'TypeError',
              'KeyError', 'AttributeError', 'UnicodeDecodeError', 'JSONDecodeError',
              'BadZipFile', 'SyncError', 'HttpError', 'TransportError',
              'ZoneInfoNotFoundError', 'ImportError', 'ModuleNotFoundError'}


def error_type(exc):
    name = type(exc).__name__
    return name if name in SAFE_TYPES else 'Exception'


def current():
    return _active.get()


def stage(name, event='begin', **counts):
    run = current()
    if run is not None:
        run.phase = name
        if 'month' in counts:
            run.month = counts['month']
        run.emit(name, event, **counts)


def emit(name, event, **counts):
    run = current()
    if run is not None:
        run.emit(name, event, **counts)


def metric(name, value=1, *, absolute=False):
    run = current()
    if run is not None and name in COUNTERS and type(value) is int and value >= 0:
        run.counts[name] = value if absolute else run.counts.get(name, 0) + value


def snapshot(name, event, *keys):
    run = current()
    if run is not None:
        run.emit(name, event, **{key: run.counts.get(key, 0) for key in keys})


def note_exception(exc):
    run = current()
    if run is not None and run.file is not None:
        run.file.write(f'Handled exception stage={run.phase}\n')
        run.file.writelines(traceback.TracebackException.from_exception(exc, capture_locals=False).format(chain=True))
        run.file.flush()


class RunDiagnostics:
    def __init__(self, *, enabled=True, forbidden=(), log_dir=None, stream=None):
        self.enabled = enabled
        self.phase = 'START' if enabled else 'PLAN'
        self.month = None
        self.reported_failure = False
        self.counts = {}
        self.path = None
        self.file = None
        self.stream = stream if stream is not None else sys.stdout
        self.forbidden = list(map(Path, forbidden))
        if log_dir is None:
            self.forbidden.append(Path(__file__).resolve().parents[1])
        self.log_dir = log_dir
        self.token = None

    def __enter__(self):
        if not self.enabled:  # Plan remains free of diagnostic file writes.
            return self
        if self.log_dir is None:
            local = os.environ.get('LOCALAPPDATA')
            if os.name == 'nt' and not local:
                raise ValueError('Private log root unavailable')
            self.log_dir = Path(local) if local else Path.home()/'.local'/'state'
            self.log_dir = self.log_dir/'DonationReceiptsRuntime'/'logs'
        directory = safe_path(self.log_dir)
        if any(directory.is_relative_to(p.resolve()) for p in self.forbidden):
            raise ValueError('Private logs must be outside code and user data')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.path = directory/f'full-{stamp}-{uuid4().hex[:12]}.log'
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.file = os.fdopen(fd, 'w', encoding='utf-8')
        self.token = _active.set(self)
        return self

    def line(self, text):
        if self.file is not None:
            self.file.write(datetime.now(timezone.utc).isoformat()+' '+text+'\n')
            self.file.flush()
        print(text, file=self.stream, flush=True)

    def emit(self, name, event, **counts):
        if not self.enabled:
            return
        if name not in STAGES or event not in {'begin', 'page', 'details', 'batch', 'complete',
                'retry', 'archive_proof_review', 'cache_loaded', 'backup_begin', 'commit_begin', 'verified', 'unchanged'}:
            raise ValueError('Unknown diagnostic event')
        if self.month is not None and 'month' not in counts:
            counts['month'] = self.month
        safe = {key: value for key, value in counts.items()
                if key in COUNTERS and type(value) is int and value >= 0}
        suffix = ' '.join(f'{key}={value}' for key, value in safe.items())
        self.line(f'[{name}] {event}'+(' | '+suffix if suffix else ''))

    def done(self, counts, report):
        if not self.enabled:
            print('[DONE] plan only; no diagnostic log created', file=self.stream, flush=True)
            return
        measured = {k: v for k, v in counts.items() if k in COUNTERS and type(v) is int and v >= 0}
        self.counts.update(measured)
        self.emit('DOWNLOAD', 'complete', **{k: self.counts.get(k, 0) for k in
            ('messages_total','messages_scanned','new_files','existing_files','failed_files','rejected_files','skipped_messages','rejected_messages')})
        self.emit('EXTRACTION','complete',**{k:self.counts.get(k,0) for k in ('source_pdfs_scanned','documents_extracted','documents_reused','catalog_documents')})
        self.emit('EXCEL', 'complete', **{k:self.counts.get(k, 0) for k in ('links_fixed','links_not_found','links_conflicts')})
        self.emit('REGISTRY', 'complete', registry_matches=self.counts.get('registry_matches', 0))
        self.emit('SECTION46', 'complete', **{k: self.counts.get(k, 0) for k in
            ('api_requests','api_errors','cache_hits','approved_rows','denied_rows','other_status_rows',
             'section46_decision_valid', 'section46_write_blocked', 'section46_evidence_invalid',
             'section46_evidence_missing', 'section46_input_invalid', 'section46_companions_preserved')})
        self.emit('DEDUP', 'complete', **{k: self.counts.get(k, 0) for k in
            ('merged_groups','merged_copies','suspected_groups')})
        self.emit('EXCEL','complete',**{k:self.counts.get(k,0) for k in ('classified_best','classified_good','classified_denied','classified_review','classified_manual')})
        self.emit('PUBLISH', 'complete', **{k: self.counts.get(k, 0) for k in (
            'identifier_documents_refreshed', 'correction_proposals_created',
            'correction_proposals_pending', 'corrections_published', 'reports_updated', 'published')})
        self.phase = 'DONE'
        text = '[DONE] mode=full | report='+report
        if self.file is not None:
            self.file.write(text+'\n')
            self.file.flush()
            self.file.close()
            self.file = None
        print(text, file=self.stream, flush=True)

    def failed(self, exc):
        self.reported_failure = True
        reason = SAFE_REASONS.get(exc.args[0], 'unclassified') if (
            type(exc) in (ValueError, RuntimeError) and len(exc.args)==1 and type(exc.args[0]) is str) else 'unclassified'
        if type(exc).__name__ == 'SyncError':
            code = getattr(exc, 'code', '')
            if code and all(c in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in code):
                reason = code
        self.emit('EXCEL', 'batch', **{k:self.counts.get(k, 0) for k in ('links_fixed','links_not_found','links_conflicts')})
        saved = False
        try:
            if self.file is not None:
                self.file.write(f'FAILED stage={self.phase}\n')
                self.file.writelines(traceback.TracebackException.from_exception(exc, capture_locals=False).format(chain=True))
                self.file.flush()
                saved = True
        except Exception:
            pass  # Never replace the original failure with a diagnostic-write failure.
        target = str(self.path) if saved else ('disabled (plan)' if not self.enabled else 'unavailable (private log write failed)')
        print(f'[FAILED] stage={self.phase} error={error_type(exc)} reason={reason} log={target}',
              file=self.stream, flush=True)

    def __exit__(self, kind, exc, tb):
        try:
            if exc is not None:
                self.failed(exc)
        finally:
            if self.token is not None:
                _active.reset(self.token)
            if self.file is not None:
                self.file.close()
        return False
