"""Full-mode base report checkpoint; all business engines remain in Sync."""
from collections import defaultdict
from dataclasses import asdict
import io
import json
from pathlib import Path

import openpyxl

from src.account_config import load_account_config
from src.logger import stage, emit
from src.private_runtime import atomic_bytes, atomic_json, safe_path
from src.receipt_deduplication import Donation
from src.receipt_summary import _write_month_sheet, SOURCE_SHEET
from src.report_layout import _apply_primary_layout
from src.sync_state import catalog_documents, digest, document_paths, effective_metadata, donation_date, SyncError
from src.sync_workbook import load

INPUTS = '_Full extraction checkpoint'


def context(receipts_dir, reports_dir, accounts_dir, account, year):
    canonical = safe_path(Path(reports_dir)/account/str(year)/f'donation_summary_{year}.xlsx')
    config = load_account_config(accounts_dir, account)
    return {'receipts': str(safe_path(receipts_dir)), 'canonical': str(canonical),
            'before': digest(canonical.read_bytes()) if canonical.exists() else None,
            'account': account, 'year': year, 'config': asdict(config) if config else None}


def prepare(receipts_dir, reports_dir, accounts_dir, account, year, root, *, resume=False, refresh_base=False):
    """Extract once, render basic month tables, and persist in the existing catalog.

    Reviewed sheets are retained unchanged in this private base workbook. Raw
    monthly tables contain one row per document, before the existing Dedup runs.
    The embedded extraction data is the input to Sync's processing phase.
    """
    config = load_account_config(accounts_dir, account)
    current = context(receipts_dir, reports_dir, accounts_dir, account, year)
    catalog_path = root/f'sync-{account}.json'
    if resume and refresh_base:
        # One local scan/extraction pass; rebuilding must retain its actual
        # extraction counters rather than immediately scanning a second time.
        return prepare(receipts_dir,reports_dir,accounts_dir,account,year,root,resume=False)
    if resume:
        if not catalog_path.is_file():
            raise SyncError('full_checkpoint_missing', 'No completed full extraction checkpoint')
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        checkpoint = catalog.get('full_processing')
        if checkpoint is None:
            # Upgrade recovery: the installed full run may have completed Gmail
            # and extraction before the old historical-source guard failed.
            # Reuse that existing catalog to build the new base locally.
            if catalog.get('version') != 1 or not catalog.get('documents'):
                raise SyncError('full_checkpoint_missing', 'No saved full extraction data')
            for key in catalog['documents']:
                if not document_paths(receipts_dir, account, catalog, [key]):
                    raise SyncError('full_source_changed', 'Saved PDF unavailable; canonical preserved')
            return prepare(receipts_dir, reports_dir, accounts_dir, account, year, root, resume=False)
        if checkpoint['context'] != current:
            raise SyncError('full_checkpoint_stale', 'Full processing inputs changed; canonical preserved')
        path = safe_path(checkpoint['path'])
        expected = safe_path(root/'full-staging'/account/str(year)/'base.xlsx')
        if path != expected or not path.is_file() or digest(path.read_bytes()) != checkpoint['sha256']:
            raise SyncError('full_checkpoint_invalid', 'Full base report checkpoint changed')
        wb = load(path)
        try:
            raw = ''.join(str(row[0]) for row in wb[INPUTS].iter_rows(min_row=2, values_only=True))
            state = json.loads(raw)
        finally:
            wb.close()
        def extraction(documents):
            return {key: {name: value for name, value in item.items()
                          if name not in ('logical_id', 'historical_review_ids')}
                    for key, item in documents.items()}
        if (extraction(catalog['documents']) != extraction(state['documents'])
                or catalog.get('historical_section46') != state.get('historical_section46')
                or catalog.get('reviewed_oc_pairs') != state.get('reviewed_oc_pairs')):
            raise SyncError('full_catalog_changed', 'Extraction catalog changed; rebuild full base')
        for key in state['documents']:
            if not document_paths(receipts_dir, account, state, [key]):
                raise SyncError('full_source_changed', 'Checkpoint PDF unavailable; canonical preserved')
        state['extraction_counts']={'source_pdfs_scanned':0,'documents_extracted':0,
                                    'documents_reused':len(state['documents']),'catalog_documents':len(state['documents'])}
        state['full_processing'] = checkpoint
        emit('WORKBOOK', 'complete')
        return catalog_path, state
    catalog_path, state = catalog_documents(receipts_dir, account, root, config, refresh_year=year)
    state.pop('full_processing', None)
    stage('WORKBOOK')
    canonical = Path(current['canonical'])
    wb = load(canonical) if canonical.exists() else openpyxl.Workbook()
    if not canonical.exists():
        wb.remove(wb.active)
    try:
        if INPUTS in wb:
            raise SyncError('full_checkpoint_sheet_conflict', 'Reserved full checkpoint sheet exists')
        sources = wb[SOURCE_SHEET] if SOURCE_SHEET in wb else wb.create_sheet(SOURCE_SHEET)
        if sources['A1'].value is None:
            sources.append(['תרומה בדוח', 'חודש שמירת מקור', 'פתיחת קובץ מקור'])
        monthly = defaultdict(list)
        for key, item in state['documents'].items():
            paths = document_paths(receipts_dir, account, state, [key])
            if not paths:
                raise SyncError('full_source_missing', 'Catalog PDF unavailable; canonical preserved')
            meta = effective_metadata(item)
            date = donation_date(meta)
            month = date.month if date else 0
            meta.file_name = paths[0].name
            monthly[month].append(Donation(meta, paths))
        for month, donations in sorted(monthly.items()):
            stage('WORKBOOK', month=month)
            title = f'_Raw {month:02d}'
            if title in wb:
                raise SyncError('full_raw_sheet_conflict', 'Reserved full raw sheet exists')
            _write_month_sheet(wb, title, donations, sources)
            _apply_primary_layout(wb[title])
        data = wb.create_sheet(INPUTS)
        data.sheet_state = 'hidden'
        data.append(['Extracted catalog v1'])
        raw = json.dumps(state, ensure_ascii=False)
        # Excel text cells are limited to 32767 characters.
        for start in range(0, len(raw), 30000):
            cell = data.cell(data.max_row+1, 1, raw[start:start+30000])
            cell.data_type = 's'
        path = safe_path(root/'full-staging'/account/str(year)/'base.xlsx')
        if path.is_relative_to(safe_path(reports_dir)):
            raise SyncError('full_staging_location', 'Full staging must be outside reports')
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = io.BytesIO(); wb.save(stream)
        atomic_bytes(path, stream.getvalue())
        if context(receipts_dir, reports_dir, accounts_dir, account, year) != current:
            raise SyncError('full_inputs_changed', 'Full inputs changed during extraction')
        state['full_processing'] = {'context': current, 'path': str(path), 'sha256': digest(path.read_bytes())}
        atomic_json(catalog_path, state)
        # Processing uses the data already saved in the base report.
        prepared_path,prepared=prepare(receipts_dir, reports_dir, accounts_dir, account, year, root, resume=True)
        prepared['extraction_counts']=state['extraction_counts']
        return prepared_path,prepared
    finally:
        wb.close()
