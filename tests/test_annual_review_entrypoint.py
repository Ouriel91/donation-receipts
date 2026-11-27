from contextlib import closing
import json

import openpyxl
import pytest
from openpyxl.comments import Comment

from src import main
from src.organization_enrichment import LIVE_HEADERS
from src.sync_state import digest
from src.sync_workbook import ID_HEADER, headers
from tests.test_qa_fixes_2026 import document, env, put, report, rows, saved, synthetic_text


def test_review_saved_documents_and_publish_only_approved_via_annual_entrypoint(env, monkeypatch, capsys):
    base, registry = env
    source = put(base, document('71234'), 'receipted.pdf')
    protected = put(base, document('81754', amount='200', date='17/04/2026'), 'preserved.pdf')
    saved(env)
    workbook = report(base)
    catalog = registry / 'sync-primary.json'
    first, second = map(lambda path: digest(path.read_bytes()), (source, protected))
    assert all(json.loads(catalog.read_bytes())['documents'][sha]['identifier_refresh']['version'] == 2
               for sha in (first, second))
    with closing(openpyxl.load_workbook(workbook)) as wb:
        for ws, row in rows(wb):
            columns = headers(ws)
            if ws.cell(row, columns['מספר קבלה']).value == '81754':
                tax = ws.cell(row, columns['מספר אישור דיווח'])
                tax.value = '2024'
                tax.comment = Comment('Verified by operator', 'Synthetic reviewer')
        wb.save(workbook)

    def snapshot():
        with closing(openpyxl.load_workbook(workbook)) as wb:
            result = {}
            for ws, row in rows(wb):
                columns = headers(ws)
                result[ws.cell(row, columns['שם קובץ']).value] = [
                    (ws.cell(row, columns[label]).value,
                     ws.cell(row, columns[label]).comment.text
                     if ws.cell(row, columns[label]).comment else None)
                    for label in ('מספר קבלה', 'מספר אישור דיווח', ID_HEADER,
                                  'סכום', 'תאריך', LIVE_HEADERS[0])]
            return result

    original = snapshot()
    files_before = tuple(path.read_bytes() for path in (source, protected))
    from types import SimpleNamespace
    scope = SimpleNamespace(
        user='alpha', project=base, receipts=base/'receipts', reports=base/'reports',
        accounts=base/'accounts', runtime=registry, registry=registry/'organizations.json',
        identity_scope=lambda account: f'alpha/{account}', manifest=base/'data'/'mailed.json', require=lambda: None, require_account=lambda _: None,
    )
    monkeypatch.setattr(main.UserScope, 'open', lambda _: scope)
    monkeypatch.setenv('DONATION_RECEIPTS_REGISTRY_ROOT', str(registry))

    def reject_gmail(*args, **kwargs):
        raise AssertionError('The saved-source path must not construct Gmail')

    monkeypatch.setattr(main, 'GmailProvider', reject_gmail)
    monkeypatch.setattr('src.sync.run_gmail', reject_gmail)

    def extract(path):
        if path == source:
            raw = synthetic_text(path.read_bytes()).replace('קבלה מס 71234', ' - מקור58137 קבלה עבור תרומה')
            return raw + '\n71234 :מספר אישור דיווח התרומה לרשות המסים\n'
        return synthetic_text(path.read_bytes()) + '\n91374 :מספר אישור דיווח התרומה לרשות המסים\n'

    for reader in ('extract_text_from_pdf', 'extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr(f'src.receipt_summary.{reader}', extract)

    def cli(mode, *options):
        monkeypatch.setattr('sys.argv', ['src.main', '--user', 'alpha', '--mode', mode, '--account', 'primary', '--year', '2026', *options])
        capsys.readouterr()
        main.main()
        return capsys.readouterr().out

    before = catalog.read_bytes(), workbook.read_bytes()
    preview = json.loads(cli('identifier-review', '--preview-identifier', first, '--preview-identifier', second))
    assert (catalog.read_bytes(), workbook.read_bytes()) == before
    assert preview[0]['proposals']['receipt_number']['value'] == '58137'
    assert 'tax_report_number' in preview[1]['protected_fields']
    assert not preview[1]['proposals']
    assert json.loads(cli('identifier-review', '--refresh-identifier', first)) == preview[:1]
    assert workbook.read_bytes() == before[1]
    cli('full', '--resume-pending', '--refresh-base')
    assert snapshot() == original
    staged = catalog.read_bytes()
    with pytest.raises(SystemExit) as rejected:
        cli('identifier-review', '--approve-identifier', first, 'receipt_number', '58137', 'incorrect')
    assert rejected.value.code == 1
    assert catalog.read_bytes() == staged
    cli('identifier-review', '--approve-identifier', first, 'receipt_number', '58137', '71234')
    output = cli('full', '--resume-pending', '--refresh-base')
    assert 'corrections_published=1' in output
    after = snapshot()
    assert after['receipted.pdf'] == [('58137', None), *original['receipted.pdf'][1:]]
    assert after['preserved.pdf'] == original['preserved.pdf']
    pending = json.loads(cli('identifier-review'))
    assert [(item['field'], item['value']) for item in pending] == [('tax_report_number', '71234')]
    assert 'corrections_published=0' in cli('full', '--resume-pending', '--refresh-base')
    assert snapshot() == after
    assert tuple(path.read_bytes() for path in (source, protected)) == files_before
