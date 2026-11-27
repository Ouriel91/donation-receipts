from pathlib import Path

import pytest

from src.receipt_deduplication import Donation
from src.receipt_metadata_extractor import ReceiptMetadata
from src.sync import historical_review_counts, run_sync
from src.sync_state import SyncError
from tests.test_sync import document, env, historical_review_workbook, put


def test_equal_metadata_does_not_authorize_an_unbound_source():
    legacy = {'legacy:row': ReceiptMetadata()}
    group = Donation(ReceiptMetadata(), (Path('legacy:row'), Path('new-file')))
    assert historical_review_counts([group], legacy, {'legacy:row': {'row'}},
                                    require_source_binding=True)[
        'unresolved_historical_document_groups'] == 1


def test_full_2026_refuses_unbound_match_before_canonical_publication(env):
    base, root, provider, network = env
    path = historical_review_workbook(env, amounts=('125',))
    put(base, document('1001', '100'), filename='synthetic-unbound.pdf')
    before = path.read_bytes()
    with pytest.raises(SyncError) as failure:
        run_sync(provider=provider, receipts_dir=base/'receipts',
                 reports_dir=base/'reports', accounts_dir=base/'accounts',
                 manifest_path=base/'manifest.json', account='primary', year=2026,
                 root=root, full_report=True)
    assert failure.value.code == 'historical_source_conflict'
    assert path.read_bytes() == before
    network.assert_not_called()


def test_sorted_archive_retains_ownership_and_active_correction(env):
    import json

    import openpyxl

    from src.sync_workbook import ARCHIVE, ID_HEADER, headers
    from tests.test_report2026_core import existing, saved
    from tests.test_sync import report, rows
    from tests.test_task_b_annual_excel import swap_rows

    existing(env, ('1001', '9999'))
    saved(env)
    path = report(env[0])
    workbook = openpyxl.load_workbook(path)
    sheet, active = rows(workbook)[0]
    columns = headers(sheet)
    publication = sheet.cell(active, columns[ID_HEADER]).value
    archived = next(row for row in range(2, sheet.max_row + 1)
                    if sheet.cell(row, columns[ARCHIVE]).value)
    sheet.cell(active, columns['סכום'], 225)
    swap_rows(sheet, active, archived)
    workbook.save(path)
    workbook.close()

    saved(env)
    assert saved(env)['reports_updated'] == 0
    workbook = openpyxl.load_workbook(path)
    [(sheet, active)] = rows(workbook)
    columns = headers(sheet)
    assert sheet.cell(active, columns[ID_HEADER]).value == publication
    assert sheet.cell(active, columns['סכום']).value == 225
    markers = [json.loads(sheet.cell(row, columns[ARCHIVE]).value)
               for row in range(2, sheet.max_row + 1)
               if sheet.cell(row, columns[ARCHIVE]).value]
    assert len(markers) == 1 and markers[0]['primary'] == publication
    workbook.close()


def test_malformed_content_addressed_backup_is_not_evidence(tmp_path):
    from src.sync_state import digest
    from src.sync_workbook import _backup_archive_snapshots

    invalid = b'synthetic invalid workbook'
    path = tmp_path / ('.backup-' + digest(invalid) + '.xlsx')
    path.write_bytes(invalid)
    assert _backup_archive_snapshots(path) == ('', {})


def test_rebind_does_not_stamp_an_altered_archive_as_valid():
    from openpyxl import Workbook

    from src.sync_workbook import ARCHIVE, _rebind_archive_owner, headers, set_archive_proof

    ws = Workbook().active
    ws.append(['סכום'])
    ws.append([100])
    set_archive_proof(ws, 2, 'original-owner')
    proof = ws.cell(2, headers(ws)[ARCHIVE])
    before = proof.value
    ws['A2'] = 200
    with pytest.raises(SyncError, match='Cannot rebind unverified archive'):
        _rebind_archive_owner(ws, 2, 'original-owner', 'new-owner')
    assert proof.value == before
