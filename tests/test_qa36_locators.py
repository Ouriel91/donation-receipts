from argparse import Namespace
from contextlib import closing
import importlib.util
import json
from pathlib import Path

from src.sync_workbook import load
from tests.test_qa_fixes_2026 import document, env, put, report, saved


def test_csv_only_locates_source_and_inspection_is_read_only(env, monkeypatch, capsys):
    base, root = env
    pdf = put(base, document(), 'synthetic.pdf')
    saved(env)
    path = report(base)
    catalog = root/'sync-primary.json'
    before = path.read_bytes(), catalog.read_bytes(), pdf.read_bytes()
    locator = base/'locators.csv'
    locator.write_text('תא,קובץ,מספר_קבלה_מוצע_לבדיקה\nינואר!E2:F2,synthetic.pdf,QA_SENTINEL\n',
                       encoding='utf-8-sig')
    spec = importlib.util.spec_from_file_location('inspect_local_qa',
                                                 Path(__file__).parents[1]/'Inspect-LocalQA.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, 'RECEIPTS_DIR', base/'receipts')
    monkeypatch.setattr(tool, 'ACCOUNTS_DIR', base/'accounts')
    args = Namespace(account='primary', year=2026, sha=[], pdf=[], cell=[], locator_csv=locator,
                     links_month=None, oc_ids=None)
    capsys.readouterr()
    with closing(load(path)) as wb:
        tool.inspect(args, wb, path, json.loads(catalog.read_bytes()))
    lines = capsys.readouterr().out
    located, evidence = map(json.loads, lines.splitlines())
    assert located['result'] == 'located'
    assert evidence['extracted']['receipt_number'] == '1001'
    assert 'QA_SENTINEL' not in lines
    assert before == (path.read_bytes(), catalog.read_bytes(), pdf.read_bytes())
