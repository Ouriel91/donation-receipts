from contextlib import closing
from io import StringIO
from pathlib import Path
import json
import subprocess
import sys

import openpyxl
import pytest

from src import sync
from src.logger import RunDiagnostics
from src.organization_enrichment import HEADERS, LIVE_HEADERS
from src.private_runtime import operational_lock
from src.sync_state import digest, effective_metadata, review_identifiers
from src.sync_workbook import ID_HEADER, INDEX, enrich_candidates, headers, receipt_source_evidence
from tests.test_qa_fixes_2026 import document, env, put, report, rows, saved

UNKNOWN = 'לא ידוע / דורש בירור'


def test_offline_enrichment_is_safe_with_staged_historical_approval(env):
    base, root = env
    put(base, document(), 'source.pdf')
    saved(env)
    with closing(openpyxl.load_workbook(report(base))) as wb:
        ws, row = rows(wb)[0]
        cols = headers(ws)
        key = ws.cell(row, cols[ID_HEADER]).value
        status = ws.cell(row, cols[LIVE_HEADERS[0]])
        status.value = UNKNOWN
        stage = base / 'offline-enrichment'
        stage.mkdir()
        enrich_candidates(wb, {key: {'managed': {}}}, {key: (ws, row)}, {}, root, None, None, stage,
                          source_state={'historical_section46': {key: {'before': UNKNOWN}}})
        assert status.value == UNKNOWN
        assert all(label in headers(ws) for label in HEADERS)


def test_pdf_scope_amounts_and_review_survive_serialization(env):
    base, root = env
    pdf = put(base, document(), 'proof.pdf')
    saved(env)
    catalog = root / 'sync-primary.json'
    sha = digest(pdf.read_bytes())
    entry = json.loads(catalog.read_bytes())['documents'][sha]
    amounts = receipt_source_evidence(pdf, effective_metadata(entry))['amounts']
    assert json.loads(json.dumps(amounts)) == ['100']
    assert 'amounts' not in entry


def test_revoke_retry_commit_failure_and_paired_restore(env, monkeypatch):
    base, root = env
    put(base, document(), 'proof.pdf')
    saved(env)
    path = report(base)
    catalog = root / 'sync-primary.json'
    with closing(openpyxl.load_workbook(path)) as wb:
        ws,row = rows(wb)[0]
        cols = headers(ws)
        key = ws.cell(row, cols[ID_HEADER]).value
        item = next(v for k,v in wb[INDEX].iter_rows(min_row=2,max_col=2) if k.value == key)
        entry = json.loads(item.value)
        entry['managed'].pop(LIVE_HEADERS[0])
        item.value = json.dumps(entry)
        ws.cell(row, cols[LIVE_HEADERS[0]]).value = UNKNOWN
        wb.save(path)
    initial_workbook, initial_catalog = path.read_bytes(), catalog.read_bytes()
    args = base/'receipts', 'primary', root, 2026
    approval = [(key,UNKNOWN,'580000001','2026-01-15')]
    assert review_identifiers(*args, eligibility=approval, reports_dir=base/'reports')['approved'] == 1
    first = json.loads(catalog.read_bytes())['historical_section46'][key]['approval_id']
    assert review_identifiers(*args, revoke_eligibility=[key])['revoked_section46'] == 1
    assert not saved(env).get('corrections_published')
    review_identifiers(*args, eligibility=approval, reports_dir=base/'reports')
    second = json.loads(catalog.read_bytes())['historical_section46'][key]['approval_id']
    assert first != second

    before_failure = path.read_bytes()
    output = StringIO()
    def fail(*args, **kwargs):
        raise OSError('synthetic publish failure')

    with monkeypatch.context() as patch:
        patch.setattr(sync, 'replace_verified', fail)
        with RunDiagnostics(log_dir=base/'logs', stream=output) as diagnostics:
            with pytest.raises(OSError, match='synthetic publish failure') as error:
                saved(env)
            diagnostics.failed(error.value)
    assert 'corrections_published=' not in output.getvalue()
    assert path.read_bytes() == before_failure
    assert saved(env)['corrections_published'] == 1
    assert not saved(env).get('corrections_published')
    review_identifiers(*args, revoke_eligibility=[key])
    with closing(openpyxl.load_workbook(path)) as workbook:
        ws,r = rows(workbook)[0]
        assert ws.cell(r,headers(ws)[LIVE_HEADERS[0]]).value == 'מאושר'
    path.write_bytes(initial_workbook)
    catalog.write_bytes(initial_catalog)
    assert not saved(env).get('corrections_published')
    with closing(openpyxl.load_workbook(path)) as workbook:
        ws,r = rows(workbook)[0]
        assert ws.cell(r,headers(ws)[LIVE_HEADERS[0]]).value == UNKNOWN


def test_review_and_sync_contend_on_same_runtime_lock(env):
    base, root = env
    put(base, document(), 'proof.pdf')
    saved(env)
    path, catalog = report(base), root/'sync-primary.json'
    start = path.read_bytes(), catalog.read_bytes()
    probe = '''
import sys
from pathlib import Path
from src.sync_state import review_identifiers
try:
    review_identifiers(Path(sys.argv[1]), 'primary', Path(sys.argv[2]), 2026)
except OSError:
    sys.exit(73)
sys.exit(0)
'''
    child = [sys.executable, '-c', probe, str(base/'receipts'), str(root)]
    with operational_lock(root):
        assert subprocess.run(child, timeout=15).returncode == 73
        with pytest.raises(OSError):
            saved(env)
    assert (path.read_bytes(), catalog.read_bytes()) == start
    assert subprocess.run(child, timeout=15).returncode == 0
