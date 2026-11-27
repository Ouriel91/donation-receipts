from contextlib import closing
import json
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import openpyxl
from openpyxl.comments import Comment
from src.annual_excel_summary import summary_name
from src.organization_enrichment import LIVE_HEADERS
from src.sync_workbook import ID_HEADER, INDEX, headers
from tests.test_qa_fixes_2026 import env, document, put, rows, report, saved, synthetic_text

UNKNOWN = 'לא ידוע / דורש בירור'

def test_legacy_status_and_amount_gates_and_formula_cache(env):
    base, root = env
    for n in range(3):
        put(base, document(number=str(17001+n), date='08/04/2026'), f'sample{n}.pdf')
    saved(env)
    path=report(base)
    with closing(openpyxl.load_workbook(path)) as wb:
        index = {cell.value: r for r, (cell, _) in enumerate(wb[INDEX].iter_rows(min_row=2,max_col=2),2)}
        for n,(ws,r) in enumerate(rows(wb)):
            cols=headers(ws)
            key=ws.cell(r,cols[ID_HEADER]).value
            index_cell=wb[INDEX].cell(index[key],2)
            entry=json.loads(index_cell.value)
            entry['managed'].pop(LIVE_HEADERS[0],None)
            if n==1:
                entry['managed'][LIVE_HEADERS[0]]=UNKNOWN
            index_cell.value=json.dumps(entry)
            ws.cell(r,cols[LIVE_HEADERS[0]]).value=UNKNOWN
            ws.cell(r,cols['סכום']).value=(None, None, 71)[n]
            if n==2:
                ws.cell(r,cols[LIVE_HEADERS[0]]).comment=Comment('Reviewed','Tester')
        wb.save(path)
    for _ in range(2):
        saved(env)
        with closing(openpyxl.load_workbook(path)) as wb:
            status=[]; amounts=[]
            for ws,r in rows(wb):
                cols=headers(ws)
                status.append(ws.cell(r,cols[LIVE_HEADERS[0]]).value)
                amounts.append(ws.cell(r,cols['סכום']).value)
            assert status == [UNKNOWN,'מאושר',UNKNOWN]
            assert amounts == [None,None,71]
            assert wb.calculation.calcMode=='auto' and wb.calculation.fullCalcOnLoad
            assert wb[summary_name(2026)]['C11'].value.text.startswith('=SUMPRODUCT')
        with closing(openpyxl.load_workbook(path,data_only=True)) as wb:
            assert wb[summary_name(2026)]['C11'].value is None
    with ZipFile(path) as file:
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        array=[c for name in file.namelist() if name.startswith('xl/worksheets/sheet') and name.endswith('.xml')
               for c in ET.fromstring(file.read(name)).findall('.//m:c',ns)
               if c.get('r')=='C11' and c.find('m:f',ns) is not None and c.find('m:f',ns).get('t')=='array']
        assert len(array)==1 and array[0].find('m:v',ns).text is None

def test_lowercase_oc_distinct_shas_stay_active(env,monkeypatch):
    base,root=env
    original=put(base,document(date='19/07/2026'),'204017o.pdf')
    copy=put(base,document(date='19/07/2026',variant='copy'),'204017c.pdf')
    def text(path):
        return synthetic_text(path.read_bytes())+('אישור דיווח התרומה לרשות המסים: 00701\n' if path==copy else '')
    for reader in ('extract_text_from_pdf','extract_text_from_pdf_pymupdf'):
        monkeypatch.setattr(f'src.receipt_summary.{reader}',text)
    for _ in range(2):
        saved(env)
        with closing(openpyxl.load_workbook(report(base))) as wb:
            assert len(rows(wb))==2
    assert original.is_file() and copy.is_file()
