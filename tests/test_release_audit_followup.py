from contextlib import closing
import json

from openpyxl import Workbook, load_workbook
import pytest

from src import combined_report, sync_state, sync_workbook
from src.receipt_deduplication import _identity
from src.receipt_metadata_extractor import ReceiptMetadata
from src.user_scope import annual_report


@pytest.mark.parametrize(('account', 'year', 'number', 'amount'), [
    ('household_a', 2023, '007841', '1.25'),
    ('household_b', 2025, '003592', '42.75'),
])
def test_current_catalog_parser_checkpoint_and_legacy_refresh(tmp_path, monkeypatch, account, year, number, amount):
    runtime = tmp_path / 'state'
    runtime.mkdir()
    receipts = tmp_path / 'mail'
    month = receipts / account / str(year) / '02_February'
    month.mkdir(parents=True)
    source = month / 'unrelated stored name.pdf'
    source.write_bytes(b'synthetic source for catalog checkpoint')
    meta = ReceiptMetadata(registration_number=str(580_100_000 + year), receipt_number=number,
                           receipt_date=f'09/02/{year}', amount=amount, payment_total_verified=True)
    read = []

    def extract(path, requested_account, config, requested_year):
        read.append((path, requested_account, requested_year))
        return meta

    monkeypatch.setattr(sync_state, '_extract_row', extract)
    catalog, state = sync_state.catalog_documents(receipts, account, runtime, {}, refresh_year=year)
    sha = sync_state.digest(source.read_bytes())
    assert state['documents'][sha]['table_amount_revision'] == 1
    assert state['documents'][sha]['tax_layout_revision'] == 1
    _, repeat = sync_state.catalog_documents(receipts, account, runtime, {}, refresh_year=year)
    assert repeat['extraction_counts']['documents_extracted'] == 0
    legacy = json.loads(catalog.read_bytes())
    entry = legacy['documents'][sha]
    del entry['table_amount_revision']
    del entry['tax_layout_revision']
    entry['metadata']['amount'] = '99'
    catalog.write_text(json.dumps(legacy))
    _, refreshed = sync_state.catalog_documents(receipts, account, runtime, {}, refresh_year=year)
    assert refreshed['extraction_counts']['documents_extracted'] == 1
    assert refreshed['documents'][sha]['metadata']['amount'] == amount
    assert read == [(source, account, year)] * 2


@pytest.mark.parametrize('conflicting', [False, True])
def test_copy_classification_checks_both_sources_first(tmp_path, monkeypatch, conflicting):
    from dataclasses import asdict, replace

    meta = ReceiptMetadata(registration_number=str(580_654_321), receipt_number='00876', amount='2.45',
                           receipt_date='14/03/2024')
    calls, sources, proof_by_sha = [], [], {}
    for index, role in enumerate(('o', 'C')):
        pdf = tmp_path / f'stored-{index}.pdf'
        pdf.write_bytes(f'synthetic distinct source {index}'.encode())
        sha = sync_state.digest(pdf.read_bytes())
        proof_by_sha[sha] = replace(meta, receipt_number='00879') if conflicting and index else meta
        sources.append((sha, pdf, f'account_{index}', {
            'metadata': asdict(meta), 'attachment_origins': [f'000956{role}.pdf'],
        }))

    def proof(path, account, year, sha):
        assert sync_state.digest(path.read_bytes()) == sha
        calls.append('pdf')
        return _identity(proof_by_sha[sha]), proof_by_sha[sha]

    classify = sync_workbook._oc_role

    def recorded_role(path):
        calls.append('role')
        return classify(path)

    monkeypatch.setattr(sync_workbook, '_oc_pdf_evidence', proof)
    monkeypatch.setattr(sync_workbook, '_oc_role', recorded_role)
    preferred = sync_workbook.verified_copy_source(sources, _identity(meta))
    assert preferred == (None if conflicting else sources[1][1])
    assert calls[:2] == ['pdf', 'pdf']
    assert ('role' in calls) is not conflicting


@pytest.mark.parametrize(('user', 'year'), [('viewer_a', 2023), ('viewer_b', 2028)])
def test_combined_filename_preserves_legacy_publication_bytes(tmp_path, user, year):
    path = annual_report(tmp_path / user / 'reports', 'combined', year)
    assert path.name == f'donation_{year}_combined.xlsx'
    path.parent.mkdir(parents=True)
    legacy = path.with_name(f'donation_summary_{year}.xlsx')
    with closing(Workbook()) as wb:
        provenance = wb.create_sheet(combined_report._PROVENANCE)
        provenance.append(['key', 'account', 'source', 'sha'])
        provenance.append(['stable-key', 'sender_group', 'source-key', 'synthetic-hash'])
        wb.active['A1'] = 'Retained synthetic content'
        wb.save(legacy)
    original = legacy.read_bytes()
    path.write_bytes(b'conflicting destination')
    with pytest.raises(ValueError, match='different contents'):
        combined_report._migrate_combined_name(path, year)
    assert legacy.read_bytes() == original
    assert path.read_bytes() == b'conflicting destination'

    path.write_bytes(original)  # Simulate restart after no-clobber publication.
    combined_report._migrate_combined_name(path, year)
    assert not legacy.exists()
    assert path.read_bytes() == original
    with closing(load_workbook(path)) as wb:
        assert wb.active['A1'].value == 'Retained synthetic content'
        assert wb[combined_report._PROVENANCE]['A2'].value == 'stable-key'
