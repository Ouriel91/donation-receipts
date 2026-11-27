"""Preserve orphan Excel person metadata; never strip active threaded comments."""
from io import BytesIO
import posixpath
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from src.sync_state import SyncError

REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
PERSON_REL = 'http://schemas.microsoft.com/office/2017/10/relationships/person'
PERSON_CT = 'application/vnd.ms-excel.person+xml'
PERSON_NS = 'http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments'
PART = 'xl/persons/person.xml'


def xml(raw):
    if len(raw) > 2_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise SyncError('unsupported_person_metadata', 'Unsafe person metadata; original preserved')
    return ET.fromstring(raw)


def capture(package):
    names = package.namelist()
    people = [n for n in names if n.startswith('xl/persons/')]
    if not people:
        return None
    if people != [PART] or len(names) != len(set(names)) or any('threadedcomment' in n.lower() for n in names):
        raise SyncError('active_person_metadata', 'Active or unrecognized person metadata; original preserved')
    types = xml(package.read('[Content_Types].xml'))
    if any('threadedcomment' in item.get('ContentType','').lower() for item in types):
        raise SyncError('active_person_metadata', 'Active threaded comment content type')
    raw = package.read(PART)
    tree = xml(raw)
    if tree.tag != '{'+PERSON_NS+'}personList' or tree.attrib:
        raise SyncError('unsupported_person_metadata', 'Unrecognized person metadata')
    for person in tree:
        if (person.tag != '{'+PERSON_NS+'}person' or list(person)
                or set(person.attrib)-{'id','displayName','userId','providerId'}):
            raise SyncError('unsupported_person_metadata', 'Unrecognized person metadata')
    incoming = []
    for name in names:
        if not name.endswith('.rels'):
            continue
        for rel in xml(package.read(name)):
            if 'threadedcomment' in rel.get('Type','').lower():
                raise SyncError('active_person_metadata', 'Active threaded comment relationship')
            target = rel.get('Target', '')
            base = posixpath.dirname(posixpath.dirname(name)) if name != '_rels/.rels' else ''
            resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base, target))
            if resolved == PART:
                if (rel.tag != '{'+REL+'}Relationship' or name != 'xl/_rels/workbook.xml.rels' or rel.get('Type') != PERSON_REL
                        or rel.get('TargetMode') or set(rel.attrib)-{'Id','Type','Target'}):
                    raise SyncError('active_person_metadata', 'Unexpected person relationship')
                incoming.append(dict(rel.attrib))
    types = xml(package.read('[Content_Types].xml'))
    overrides = [dict(item.attrib) for item in types if item.tag == '{'+CT+'}Override' and item.get('PartName') == '/'+PART]
    if len(incoming) != 1 or len(overrides) != 1 or overrides[0].get('ContentType') != PERSON_CT:
        raise SyncError('unsupported_person_metadata', 'Person metadata binding unproven')
    return {'raw': raw, 'relationships': incoming, 'types': overrides}


def restore(path, saved):
    if not saved:
        return
    with ZipFile(path) as package:
        data = {n: package.read(n) for n in package.namelist()}
    if PART in data:
        raise SyncError('person_metadata_collision', 'Unexpected person metadata collision')
    rels = xml(data['xl/_rels/workbook.xml.rels'])
    used = {item.get('Id') for item in rels}
    for original in saved['relationships']:
        item = dict(original)
        # Relationship IDs are local; choose an unused ID without rewriting others.
        if item['Id'] in used:
            index = 1
            while 'rIdPerson'+str(index) in used:
                index += 1
            item['Id'] = 'rIdPerson'+str(index)
        ET.SubElement(rels, '{'+REL+'}Relationship', item)
    data['xl/_rels/workbook.xml.rels'] = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
    types = xml(data['[Content_Types].xml'])
    for item in saved['types']:
        ET.SubElement(types, '{'+CT+'}Override', item)
    data['[Content_Types].xml'] = ET.tostring(types, encoding='utf-8', xml_declaration=True)
    data[PART] = saved['raw']
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as package:
        for name, raw in data.items():
            package.writestr(name, raw)
    # Only unpublished staging candidate is written here, never original workbook.
    path.write_bytes(output.getvalue())
    with ZipFile(path) as package:
        checked = capture(package)
        if checked['raw'] != saved['raw'] or checked['types'] != saved['types']:
            raise SyncError('person_metadata_verification_failed', 'Person preservation failed')
