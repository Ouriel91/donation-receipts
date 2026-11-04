"""Trusted transport wrapper; pytest and its report remain untrusted evidence."""
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import xml.etree.ElementTree as ET

resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024, 256 * 1024))
paths = ('src/organization_cache.py', 'tests/test_organization_cache.py')
before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
with open('/tmp/output', 'wb') as output:
    result = subprocess.run(['python', '-I', '-m', 'pytest', '-q',
        '-o', 'pythonpath=/input', '-p', 'no:cacheprovider', '--tb=short',
        paths[1], '--basetemp=/tmp/tests', '--junitxml=/tmp/report.xml'],
        stdout=output, stderr=subprocess.STDOUT, timeout=280)
after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
report = Path('/tmp/report.xml')
xml = None
if report.is_file() and not report.is_symlink() and report.stat().st_size <= 128 * 1024:
    xml = report.read_text()
    if '<!DOCTYPE' in xml or '<!ENTITY' in xml:
        xml = None
    else:
        try:
            root = ET.fromstring(xml)
            for node in root.iter():
                node.text = node.tail = None
                node.attrib = {k: v for k, v in node.attrib.items() if k in
                    ('tests', 'failures', 'errors', 'skipped', 'name', 'classname')}
            xml = ET.tostring(root, encoding='unicode')
        except ET.ParseError:
            xml = None
print(json.dumps({'exit': result.returncode, 'before': before, 'after': after,
    'xml': xml, 'output': Path('/tmp/output').read_text(errors='replace')[:16384]}))
