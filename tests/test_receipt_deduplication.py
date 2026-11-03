"""Annual consolidation regressions; all metadata and documents are synthetic."""
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import openpyxl
import pytest

from src import receipt_summary as summary
from src.account_config import AccountConfig
from src.hebrew_text_normalizer import normalize_hebrew_text
from src.receipt_deduplication import consolidate_receipts, receipt_amount
from src.receipt_metadata_extractor import ReceiptMetadata


def record(index, month='01_January', **changes):
    path = Path('synthetic') / '2026' / month / f'synthetic_{index}.pdf'
    meta = ReceiptMetadata(
        file_name=path.name, account='synthetic', organization_name='עמותת בדיקה',
        registration_number='580000001', receipt_number='AB-000123456789012',
        receipt_date='02/01/2026', amount='100.00', tax_report_number='654321',
    )
    return path, replace(meta, **changes)


def test_identical_original_and_copy_merge_without_mutating_inputs():
    records = [record(0), record(1)]
    previous = [asdict(meta) for _, meta in records]
    donations = consolidate_receipts(records)
    assert len(donations) == 1
    assert donations[0].sources == tuple(path for path, _ in records)
    assert donations[0].metadata.amount == '100.00'
    assert donations[0].filled_fields == 0
    assert [asdict(meta) for _, meta in records] == previous


@pytest.mark.parametrize('number', ['00001234567890123456', 'AB-0000123456789/007', 'R.000123'])
def test_identifiers_keep_letters_separators_and_leading_zeros(number):
    donations = consolidate_receipts([record(0, receipt_number=number), record(1, receipt_number=number)])
    assert len(donations) == 1
    assert donations[0].metadata.receipt_number == number


@pytest.mark.parametrize('field,value', [
    ('tax_report_number', '654321'), ('organization_name', 'עמותת בדיקה'),
    ('donor_name', 'תורם לדוגמה'), ('donor_id', '123456782'),
])
def test_missing_optional_field_is_completed_without_overwrite(field, value):
    donations = consolidate_receipts([record(0, **{field: ''}), record(1, **{field: value})])
    assert len(donations) == 1
    assert getattr(donations[0].metadata, field) == value
    assert donations[0].filled_fields == 1


@pytest.mark.parametrize('field,value', [
    ('amount', '200.00'), ('receipt_date', '03/01/2026'),
    ('registration_number', '580000002'), ('tax_report_number', '765432'),
    ('organization_name', 'עמותה אחרת לדוגמה'), ('donor_id', '222222226'),
    ('donor_name', 'תורם אחר לדוגמה'),
])
def test_conflicting_fields_are_not_overwritten_or_merged(field, value):
    original = record(0, donor_name='תורם לדוגמה', donor_id='123456782')
    other = record(1, donor_name='תורם לדוגמה', donor_id='123456782')
    other = other[0], replace(other[1], **{field: value})
    donations = consolidate_receipts([original, other])
    assert len(donations) == 2
    assert getattr(donations[0].metadata, field) == getattr(original[1], field)
    assert getattr(donations[1].metadata, field) == value
    assert donations[0].suspected_group == donations[1].suspected_group is not None


@pytest.mark.parametrize('field', ['amount', 'receipt_date', 'registration_number', 'receipt_number'])
def test_missing_core_identity_field_requires_review(field):
    donations = consolidate_receipts([record(0, **{field: ''}), record(1)])
    assert len(donations) == 2
    assert all(d.suspected_group is not None for d in donations)
    assert getattr(donations[0].metadata, field) == ''


@pytest.mark.parametrize('changes', [dict(amount='200'), dict(amount=''), dict(tax_report_number='765432')])
def test_ambiguous_bridge_blocks_entire_candidate_group(changes):
    donations = consolidate_receipts([record(0, tax_report_number=''), record(1), record(2, **changes)])
    assert len(donations) == 3
    assert all(len(d.sources) == 1 and d.suspected_group is not None for d in donations)


def test_different_charities_with_same_receipt_number_never_merge():
    donations = consolidate_receipts([record(0), record(1, registration_number='580000002', amount='150')])
    assert len(donations) == 2
    assert all(d.suspected_group is None for d in donations)


def test_receipt_number_alone_does_not_create_duplicate_group():
    donations = consolidate_receipts([record(0), record(1, registration_number='', receipt_date='', amount='')])
    assert len(donations) == 2
    assert all(d.suspected_group is None for d in donations)


def test_leading_zero_identifier_is_not_equal_to_stripped_identifier():
    donations = consolidate_receipts([record(0, receipt_number='0001234'), record(1, receipt_number='1234')])
    assert len(donations) == 2
    assert all(d.suspected_group is None for d in donations)


def test_equivalent_amount_formatting_uses_exact_decimal_equality():
    donations = consolidate_receipts([record(0, amount='1,000.10'), record(1, amount='1000.1')])
    assert len(donations) == 1
    assert receipt_amount(donations[0].metadata.amount) == Decimal('1000.10')


@pytest.mark.parametrize('amount', ['', 'NaN', 'Infinity', 'not an amount', '0', '-100'])
def test_invalid_or_nonpositive_amount_is_not_a_merge_identity(amount):
    assert receipt_amount(amount) is None
    donations = consolidate_receipts([record(0, amount=amount), record(1, amount=amount)])
    assert len(donations) == 2
    assert all(d.suspected_group is not None for d in donations)


def test_invalid_date_does_not_enable_merge():
    donations = consolidate_receipts([record(0, receipt_date='32/01/2026'), record(1, receipt_date='32/01/2026')])
    assert len(donations) == 2
    assert all(d.suspected_group is not None for d in donations)


def test_no_duplicates_or_empty_input():
    assert consolidate_receipts([]) == []
    donations = consolidate_receipts([record(0), record(1, receipt_number='OTHER-001')])
    assert len(donations) == 2
    assert all(len(d.sources) == 1 and d.suspected_group is None for d in donations)


@pytest.fixture
def make_workbook(tmp_path, monkeypatch):
    def make(specs, year=2026, config=None):
        receipts = tmp_path / 'receipts'
        texts, paths = {}, []
        for index, (month, changes) in enumerate(specs):
            # Avoid numeric filename fallback in missing-receipt test cases.
            path = receipts / 'synthetic' / str(year) / month / f'synthetic_{chr(97 + index)}.pdf'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic PDF placeholder; readers mocked')
            values = dict(receipt_number='AB-000123456789012', registration_number='580000001',
                          receipt_date=f'02/01/{year}', amount='100.00', tax_report_number='654321')
            values.update(changes)
            labels = {'receipt_number': 'מספר קבלה:', 'registration_number': 'מספר עמותה:',
                      'receipt_date': '', 'amount': '₪', 'tax_report_number': 'אישור דיווח:'}
            texts[path] = 'עמותת בדיקה\n' + '\n'.join(
                f'{labels[field]} {value}' for field, value in values.items() if value)
            paths.append(path)
        calls = []
        def read(path):
            calls.append(path)
            return texts[path]
        monkeypatch.setattr(summary, 'extract_text_from_pdf', read)
        monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf', lambda path: '')
        path, counts = summary.generate_summary_workbook(receipts, tmp_path / 'reports', 'synthetic', year, config)
        assert calls == sorted(paths)
        assert all(p.read_bytes() == b'synthetic PDF placeholder; readers mocked' for p in paths)
        return openpyxl.load_workbook(path), counts, paths
    return make


def metrics(wb):
    labels = {label: key for key, label in summary.METRIC_LABELS.items()}
    return {labels[row[0]]: row[1] for row in wb['סיכום'].iter_rows(min_row=2, values_only=True)
            if row[0] in labels}


def month_rows(wb):
    return [dict(zip(summary.REPORT_COLUMNS, values)) for name in summary._MONTH_SHEET_NAMES.values()
            if name in wb for values in wb[name].iter_rows(min_row=2, values_only=True)]


def test_copy_supplies_reporting_number_before_severity_is_finalized(make_workbook):
    wb, counts, _ = make_workbook([('01_January', {'tax_report_number': ''}), ('01_January', {})])
    try:
        rows = month_rows(wb)
        assert counts == {'ready': 1}
        assert len(rows) == 1
        assert rows[0]['tax_report_number'] == '654321'
        assert rows[0]['extraction_status'] == 'תקין'
        assert 'מספר אישור דיווח חסר' not in (rows[0]['notes'] or '')
        assert 'אוחדו 2 קובצי מקור' in rows[0]['notes']
        data = metrics(wb)
        assert data['pdf_files'] == 2 and data['donations'] == 1
        assert data['warning_before'] == 1 and data['ready_before'] == data['ready_after'] == 1
        assert data['merged_groups'] == data['merged_copies'] == data['filled_fields'] == 1
        assert data['amount_before'] == 200 and data['amount_after'] == data['duplicate_amount_removed'] == 100
        cell = wb['ינואר'].cell(2, summary.COLUMNS.index('receipt_number') + 1)
        assert cell.value == 'AB-000123456789012' and cell.data_type == 's' and cell.number_format == '@'
    finally:
        wb.close()


def test_annual_merge_uses_earliest_source_month_and_links_all_sources(make_workbook):
    wb, _, paths = make_workbook([('02_February', {}), ('01_January', {})])
    try:
        assert wb['ינואר'].max_row == 2 and wb['פברואר'].max_row == 1
        assert wb['ינואר'].cell(2, summary.COLUMNS.index('file_name') + 1).value == paths[1].name
        rows = month_rows(wb)
        assert rows[0]['source_count'] == 2
        assert wb['ינואר'].cell(2, len(summary.REPORT_COLUMNS)).hyperlink.target == "#'קובצי מקור'!A2"
        source_sheet = wb[summary.SOURCE_SHEET]
        assert source_sheet.max_row == 3
        linked_paths = set()
        for row in source_sheet.iter_rows(min_row=2):
            assert row[0].hyperlink.target == "#'ינואר'!A2"
            uri = urlparse(row[2].hyperlink.target)
            assert uri.scheme == 'file'
            linked_paths.add(Path(url2pathname(unquote(uri.path))).resolve())
        assert linked_paths == {p.resolve() for p in paths}
        assert all(ws.sheet_view.rightToLeft for ws in wb)
        assert wb.sheetnames[:2] == ['ינואר', 'פברואר']
    finally:
        wb.close()


@pytest.mark.parametrize('changes', [{'amount': '200'}, {'receipt_date': '03/01/2026'},
                                     {'registration_number': '580000002'}, {'receipt_number': ''}])
def test_suspected_duplicates_remain_separate_marked_and_counted(make_workbook, changes):
    wb, counts, _ = make_workbook([('01_January', {}), ('02_February', changes)])
    try:
        rows = month_rows(wb)
        assert len(rows) == 2
        assert all(row['source_count'] == 1 and 'חשד לכפילות' in row['notes'] for row in rows)
        assert all(row['severity'] != 'מוכן' for row in rows)
        assert sum(counts.values()) == 2
        data = metrics(wb)
        assert data['merged_groups'] == 0
        assert data['suspected_groups'] == 1 and data['suspected_files'] == 2
        assert data['amount_before'] == data['amount_after']
        assert data['duplicate_amount_removed'] == 0
    finally:
        wb.close()


@pytest.mark.parametrize('year', range(2020, 2026))
def test_previous_year_reports_merge_without_requiring_reporting_number(make_workbook, year):
    wb, counts, _ = make_workbook([('01_January', {'tax_report_number': ''}),
                                  ('02_February', {'tax_report_number': ''})], year=year)
    try:
        assert counts == {'ready': 1}
        assert len(month_rows(wb)) == 1
        assert 'שדה לא נדרש לשנת מס זו' in month_rows(wb)[0]['notes']
        assert metrics(wb)['amount_after'] == 100
    finally:
        wb.close()


def test_no_duplicates_preserves_donations_and_sums(make_workbook):
    wb, counts, _ = make_workbook([('01_January', {}), ('02_February', {'receipt_number': 'OTHER-002'})])
    try:
        assert counts == {'ready': 2}
        assert len(month_rows(wb)) == 2
        data = metrics(wb)
        assert data['pdf_files'] == data['donations'] == 2
        assert data['merged_groups'] == data['suspected_groups'] == data['filled_fields'] == 0
        assert data['amount_before'] == data['amount_after'] == 200
    finally:
        wb.close()


def test_completed_donor_is_checked_against_config_after_merge(monkeypatch, tmp_path):
    receipts = tmp_path / 'receipts'
    month = receipts / 'synthetic' / '2026' / '01_January'
    month.mkdir(parents=True)
    first, second = month / 'first.pdf', month / 'second.pdf'
    for path in (first, second):
        path.write_bytes(b'synthetic')
    inputs = {first: record(0, donor_id='')[1], second: record(1, donor_id='123456782')[1]}
    monkeypatch.setattr(summary, '_extract_row', lambda path, *args: replace(inputs[path], file_name=path.name))
    config = AccountConfig('Synthetic', expected_donor_ids=['123456782'])
    path, counts = summary.generate_summary_workbook(receipts, tmp_path / 'reports', 'synthetic', 2026, config)
    wb = openpyxl.load_workbook(path)
    try:
        row = month_rows(wb)[0]
        assert counts == {'ready': 1}
        assert row['donor_match'] == 'התאמה'
        assert 'לא זוהה תורם' not in row['notes']
    finally:
        wb.close()


def test_source_filename_cannot_become_an_excel_formula(monkeypatch, tmp_path):
    month = tmp_path / 'receipts' / 'synthetic' / '2026' / '01_January'
    month.mkdir(parents=True)
    source = month / '=synthetic.pdf'
    source.write_bytes(b'synthetic')
    monkeypatch.setattr(summary, '_extract_row', lambda path, *args: replace(record(0)[1], file_name=path.name))
    path, _ = summary.generate_summary_workbook(tmp_path / 'receipts', tmp_path / 'reports', 'synthetic', 2026)
    wb = openpyxl.load_workbook(path, data_only=False)
    try:
        for cell in (wb['ינואר'].cell(2, summary.COLUMNS.index('file_name') + 1), wb[summary.SOURCE_SHEET]['C2']):
            assert cell.value == '=synthetic.pdf'
            assert cell.data_type == 's'
            assert cell.hyperlink.target.startswith('file:')
    finally:
        wb.close()


@pytest.mark.parametrize('normalized', [False, True])
@pytest.mark.parametrize('visual_hash', [False, True])
@pytest.mark.parametrize('conflict', [None, 'amount', 'receipt_date', 'registration_number', 'tax_report_number'])
def test_hash_and_on_donation_formats_merge_only_when_consistent(tmp_path, monkeypatch, normalized, visual_hash, conflict):
    month = tmp_path / 'receipts' / 'synthetic' / '2026' / '07_July'
    month.mkdir(parents=True)
    original, copy = month / 'original_9999.pdf', month / 'copy_9999.pdf'
    for path in (original, copy):
        path.write_bytes(b'synthetic PDF; text readers mocked')
    fields = {'amount': '100.00', 'receipt_date': '02/07/2026',
              'registration_number': '580000001', 'tax_report_number': '654321'}
    different = {'amount': '200.00', 'receipt_date': '03/07/2026',
                 'registration_number': '580000002', 'tax_report_number': '765432'}
    texts = {}
    for path, receipt_label in ((original, 'קבלה #'), (copy, 'קבלה על תרומה:')):
        values = dict(fields)
        if path == copy and conflict:
            values[conflict] = different[conflict]
        receipt_text = ('AB-000313123456789/007 # הלבק' if path == original and visual_hash
                        else f'{receipt_label} AB-000313123456789/007')
        text = ('עמותת בדיקה\n' + receipt_text + '\n'
                + f"מספר עמותה: {values['registration_number']}\n"
                + f"אישור דיווח: {values['tax_report_number']}\n"
                + f"{values['receipt_date']}\n₪{values['amount']}")
        texts[path] = normalize_hebrew_text(text) if normalized else text
    primary = dict(texts)
    if visual_hash:
        # Reproduce a label available only in the alternative PDF engine.
        primary[original] = '\n'.join(line for i, line in enumerate(texts[original].splitlines()) if i != 1)
    monkeypatch.setattr(summary, 'extract_text_from_pdf', lambda path: primary[path])
    monkeypatch.setattr(summary, 'extract_text_from_pdf_pymupdf',
                        lambda path: texts[path] if visual_hash and path == original else '')
    output, counts = summary.generate_summary_workbook(tmp_path / 'receipts', tmp_path / 'reports', 'synthetic', 2026)
    wb = openpyxl.load_workbook(output)
    try:
        rows, data = month_rows(wb), metrics(wb)
        assert data['pdf_files'] == 2
        assert wb[summary.SOURCE_SHEET].max_row == 3
        assert all(row['receipt_number'] == 'AB-000313123456789/007' for row in rows)
        # Conflicting numeric filename fallback must lose to the actual text ID.
        assert len(rows) == (2 if conflict else 1)
        assert data['merged_groups'] == (0 if conflict else 1)
        assert data['suspected_groups'] == (1 if conflict else 0)
        assert sum(counts.values()) == len(rows)
        if conflict:
            assert all('חשד לכפילות' in row['notes'] for row in rows)
            assert data['amount_before'] == data['amount_after']
        else:
            assert counts == {'ready': 1}
            assert rows[0]['source_count'] == 2
            assert data['amount_before'] == 200 and data['amount_after'] == 100
    finally:
        wb.close()
