from argparse import Namespace
from contextlib import closing
import importlib.util
from pathlib import Path
import json

import pymupdf

from src.sync_workbook import load
from tests.test_qa_fixes_2026 import env, saved


def test_label_evidence_reads_real_pdf_and_redacts_in_place(env, monkeypatch, capsys):
    base, root = env
    source = base/'receipts/primary/2026/01_January/example.pdf'
    source.parent.mkdir(parents=True)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_htmlbox(page.rect, 'קבלה עבור תרומה 63819<br>מספר אישור דיווח התרומה לרשות המסים:<br>'
                            '71826<br>מספר עמותה: 580000001<br>03/01/2026<br>₪100<br>PrivatePerson')
        pdf.save(source)
    from src import pdf_text_extractor
    from src import receipt_summary
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf', pdf_text_extractor.extract_text_from_pdf)
    monkeypatch.setattr(receipt_summary, 'extract_text_from_pdf_pymupdf', pdf_text_extractor.extract_text_from_pdf_pymupdf)
    saved(env)
    workbook = base/'reports/primary/2026/donation_summary_2026.xlsx'
    catalog = root/'sync-primary.json'
    before = [item.read_bytes() for item in (workbook, catalog, source)]
    csv = base/'locator.csv'
    csv.write_text('תא,קובץ,הצעה\nינואר!E2:F2,example.pdf,QA_SENTINEL\n', encoding='utf-8-sig')
    spec = importlib.util.spec_from_file_location('pdf_label_evidence',
                                                Path(__file__).parents[1]/'Collect-PdfLabelEvidence.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, 'RECEIPTS_DIR', base/'receipts')
    monkeypatch.setattr(tool, 'ACCOUNTS_DIR', base/'accounts')
    args = Namespace(year=2026, account='primary', csv=csv, case=['ינואר!E2:F2'],
                     amount_cell=[], oc=None, max_lines=30, private_raw=False)
    capsys.readouterr()
    with closing(load(workbook)) as wb:
        tool.run(args, workbook, json.loads(before[1]), wb)
    output = capsys.readouterr().out
    assert all(secret not in output for secret in ('63819','71826','580000001','PrivatePerson','QA_SENTINEL','example.pdf'))
    record = json.loads(output)
    assert record['product_fields']['receipt_number'].startswith('<n')
    assert record['product_fields']['tax_report_number'].startswith('<n')
    assert record['layout']['status'] == 'ok' and record['layout']['lines']
    assert record['readers']['pypdf']['raw']['lines']
    assert record['readers']['pymupdf']['normalized']['lines']
    assert before == [item.read_bytes() for item in (workbook, catalog, source)]
