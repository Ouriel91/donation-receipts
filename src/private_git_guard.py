"""Fail-closed Git index/history audit; no import-time actions.

.gitignore cannot protect tracked or force-added data. Local hooks prevent
accidents unless bypassed; CI detects after push. Hook installation is explicit.
No Agent Loop approval, Docker isolation, Git config, refs or history changes.
Detection is heuristic: unknown schemas, encodings, encryption and formatted IDs
can evade it. Exact reviewed byte pins never bypass secret scanning. Host tests
must verify real index/history/hooks; inspection cannot replace secret rotation."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib
MAX_BLOB = 512 * 1024
PRIVATE_PARTS = frozenset({'data', 'private', 'cache', 'caches', 'runtime', 'receipts', 'reports', 'accounts', 'secrets', 'tokens', 'logs', 'section46_evidence', 'evidence', 'snapshots', 'backups', 'backup', '_archive', '_qa', '.aws', '.agents', '.codex'})
PRIVATE_SUFFIXES = frozenset({'.csv', '.tsv', '.xlsx', '.xls', '.xlsm', '.xlsb', '.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db', '.log', '.zip', '.bundle', '.bak'})
TEXT_SUFFIXES = frozenset({'.py', '.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.ini', '.cfg', '.ps1', '.sh', '.xml', '.example', '.wsb'})
SAFE_NAMES = frozenset({'.gitignore', '.gitattributes', 'pre-commit', 'pre-push', 'LICENSE', 'Dockerfile', '.dockerignore', '.fictional-agent-workspace'})
SYNTHETIC_BINARIES = {'harness/attachments/fake_receipt.pdf': '1bec9fc5ea4469bd894d2f45ec34a44693c321fa1c34eb8187397ae13850456a'}
SECRET = re.compile('github_pat_[A-Za-z0-9_]{20,}|GOCSPX-[A-Za-z0-9_-]{10,}|ya29\\.[A-Za-z0-9_-]{12,}|1//[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|-----BEGIN [^-\\r\\n]*PRIVATE KEY(?: BLOCK)?-----|\\bgh[pousr]_[A-Za-z0-9]{20,}\\b|\\bsk-[A-Za-z0-9_-]{20,}\\b|\\bAKIA[A-Z0-9]{16}\\b')
REVIEWED_SYNTHETIC_SOURCES = {'tests/test_registry_recovery.py': {'92771ae4fd492906ea1bd8ca4d8334524fbdd4a2f04c13140bb0489acc2132cb', '46609ee49237d84f629221868866a5552213c10d7915005d26ed8c7cefdaf6ed'}, 'src/registry_csv.py': {'3459ab31a06c553d9ce1a3b1c9216791ec8aa67c162340a8d1ef08de7400ea8c', '0d3719faf8f757573d2258015b10bb4f20999cfa08783fb661bc34c1a150407c', '981724168f4b1fa3dbf1d7bd9e2c3b97dc90ce4586b42a152e5e5a29d088da2b'}, 'agent_loop/tasks/official_registry_acceptance.py': {'6a2d17958f6ca39223ca6ed245a1c4e2ec350ffdcfb2e21ac442e9f3ba403ef9'}, 'agent_loop/tasks/organization_cache_v2_acceptance.py': {'0a06b39eed247a874a59e728de0dd4cd78e4700b3d1eb5fecec07eba15a3bc1c', '169da8ccf56ff7e69598d8a5507079062e559c07585940d6579db38545646c29', '6bd0c6e3371b7797ce5027e7312d83e3c21332cb3d6d1fe60c84460feb068d67'}, 'agent_loop/tasks/private_guard_acceptance.py': {'d26eed2a4be279d9b08bec33a5ebc4c2eadc0e68f431d83e31a79028f53afa8a'}, 'agent_loop/tasks/registry_csv_acceptance.py': {'57a1716569a4bcfe54401fb152fa5d9761022512e27c9d25a02f54ec01095f83'}, 'agent_loop/tasks/section46_acceptance.py': {'5cccdb07f399e27ae0b76e59c3ec34aea5938ae0e55cfc9905774d0df2b733ae', '99abb64f4042437b71783aff9c48ba6971c864dd2ea73b7bd8b34a41fe05eb22', 'db2ba72a620872e9adce71020e525e003c3a24ccaa31d10c099f26b6a64110da'}, 'agent_loop/tasks/weekly_registry_acceptance.py': {'56d4022a910ff55d8dedbf90aeeb07d35ee9ff5d5168c1bf1b6e9a1b477115e1', 'ae7b08ef60dc2a21cf751f17c843edef0a1bb57db0561ef325ff42f3c46b0f0b', 'ce5b2d7aae4fff9feed1f6f20e6c554b63c8c4fbbb179593b181c039231c9874', 'd95a1c2d0cf5f626180e2be2e7bddf57247e11aa86f04c49398619b7384fb5aa'}, 'debug_registration.py': {'40a445137b8cd181d2248e45b04f9e33351cda581c2170486af17e2fc71cdb24'}, 'docs/AGENT_LOOP_ISOLATION.md': {'65f6553aa199f606382f83f5b586427f8958125f7752460ef3a203ee1fc2c8a1', 'b64c397245bfe00ea05b5b1856e9e03353b478a911389bbd45191e54c4e5b1ff'}, 'scripts/compare_pdf_engines.py': {'466f0205a7569bb989ed178fbfea4cb2399b8c26f90022889e1b1dd27d2c482d'}, 'scripts/debug_registration_extraction.py': {'0b6fdd12af3339d44e89cc88807dc5195afcb703015e4be8b68e1adaca10421f'}, 'scripts/smoke_organization_enrichment.py': {'21daec69a990912d1c5b79f82ca7483b84554cbf7b34da89ef5c17f199c06495', '9c983e05a6cbf178c426535f04bef5673b16c5a2566ca58d4adecdeaac313777'}, 'src/organization_cache.py': {'ff78f47e78237a609267bc04b63f5c719dc41eaab25eee0864e062761b7cff51', 'ade1f43f3882cff9ca5f9740a131b71d1b21e1633b1e0274d2cfa79040643f1c', '29a184709f7997de2755c5e3d84ed292ec4c0373d6a1b13a98b0d92a994ecd56', '58a08f2facdaebdcc606fce633bc0e28d22694bd99a80b8cfe834218016a12ce'}, 'src/organization_enrichment.py': {'2a62921863af1e3c72e1cd801248b608a1ca5ffc3d095df915ef68a139c42f34', '4bef9311a9168d455b8d458aa33b14cb28d45181b86de9af421942cec3e636a2', 'd39324d48f1f4de4b7aec8b810e6d27ec1f6f584e75fe2ce4791da4d0c1c3187'}, 'src/private_git_guard.py': {'6d1d9cc8e68028e9a52d5b48218eee63779a9279fbe7faec919b39c093696ea5'}, 'src/providers/gmail_auth.py': {'d61879b9cb6190d8b16728387324dc9ae5fd5187819a1209ba54ec9eaf431526'}, 'src/receipt_deduplication.py': {'4f2b9a6cfc04081d1cf61a0e05973b3626e768893fd0ad687f258fd843929ccb'}, 'src/receipt_metadata_extractor.py': {'05136cbbcdc1fefcfc4613ef84d9584d781ecab0f28669e15126738af998f116'}, 'src/receipt_summary.py': {'35abad776000eaf687b6d10a62b410cabb269e87fced8aaf85c2c1717f90cc31'}, 'src/registry_sync.py': {'c1427641c7597b539b521bc57231601f475b9d9e16e865e79b71c7b6bd8795ed'}, 'src/section46.py': {'58ff22b34cf1130794d819bbb036009f9331b76b192e1af9948059a59b12c766', '7c6394cd884f0b605892eebc8568bdcab19b82f853550fa8495de14bfc0cba0a'}, 'src/section46_cli.py': {'493a1127032505a0aed3572e84c16b63c0441a1f06871911b5eac52eb967406d'}, 'tests/test_account_config.py': {'b99a322b6a7e5bf85d7b06fe21bb4798d9b1de67cdfbe4e1bea9d9e4f05e4fa4'}, 'tests/test_extraction_quality.py': {'32e3ea7576be1e1629ed59554a6ba8b736d4723fe29a473fb50b06c5988e2a9e'}, 'tests/test_gmail_auth.py': {'3c640b40bbb2866706d77dfecbb90ccce62922e15d9b05d281dcf445fdc0e6e0'}, 'tests/test_gmail_provider.py': {'633523f43673735a78c258ed56b71a33b740f6d9ac71eeb22aee736943442dbc'}, 'tests/test_organization_cache.py': {'2cf2ff7b4366b336f6f11967d00f6b1eae8692440ad785197242330ced87810f', '7116a341d39f35ca47966977f72cc325b9fb46d382aa7a64a395fa8caedaa14e'}, 'tests/test_organization_enrichment.py': {'073846505eec3721b1105be5450f9452d22ab2d0d47deb81b52a2e827f10382e', '495be3bf53a06669d531219bdf47a1ec98beee7dcc484b76bd050133ff265883'}, 'tests/test_private_git_guard.py': {'0a99753e1982c77876f7fd272596419f24701f622863b3f15e8979e67a2ed8a6', '0e8d30386ed0d35f05b48f219ce932fa579d888b88287019e9b1ea35026c78b3', '838ed867f2a4d2dc197e2ba5ee8e50fb24a7c91d79b88d9cfe26dff7e88678c6', '988b35e1189321d6af6fcca1551734d45f2de3ed62cef0f5592e5bccab33b451', 'fec68b66b53f584a5c9107371f2f89aa106ff61662ff666031cf76a8328b7c98', '165d9eb6a801a82c5a3017973e938a1fa7bc83a899441e038d114a76ae392cbe'}, 'tests/test_pymupdf_fallback.py': {'6d405f9d092461d9f9f51e0c45a24af4d3f1eb55827eb84488e3e2a105637e49'}, 'tests/test_receipt_deduplication.py': {'a71ca6116065cfe7d0dc2ce6e19cd92087405d03a24a16164a3241515d6e0d1b'}, 'tests/test_receipt_metadata_extractor.py': {'872d2fccd5ea29938895ccdeb9df62b1d8165a22e45799d44b452ec323b35642'}, 'tests/test_receipt_summary.py': {'1d0e036e7d97f3f268846fe5283efcefa28e0f52b8a3f5e8b174da87945216b3'}, 'tests/test_registry_csv.py': {'02a21ee2aec5bd7494dd97c9940c4efb311e20ee94e7cca318eff4eb7a94903f', '7427cfb0d05e83731cff594636f2a8fdb38f843feef6e81807ff452153a888be', 'bd8e7bd7681994712a0d9bbba128ea93213f8fc7dd1c7a147633c06fb900a34d'}, 'tests/test_section46.py': {'0f9cd529948a8d290d6bbda82b26b1e49f3b6dd7ffe1256cdb1274a9c47d35b0', '1e83aef33b866b7c6ebf0f5548f310e95892e637590cba60385a4d66fc91f021', '269def154c5b1332f7bd5ba138aaec322e90ba25ec9903b62a1a10957dea33b5', '895fc45f27bb28239255113624bddf54bc375b025f229360fc9c65aebd094c19', '9e741cf3c16890cefef9d7377be854b2fd2decda48c5667d711bcb66f0535838', 'b9ecbc850ca701144cb3f519a143d3b47b62996ba88602d07d86fea9d69b7066'}, 'tests/test_weekly_registry_sync.py': {'06f443dbd55f792133b9dec8245db2b3aeda3e0afcc2b87234f8b03782fffddd', '27d8067ca93b4720406a22cc67a31b9da00370a49894c4577f63e2631eee3191', '45e7ac00eba514eceaa02cb21796032ec285fe765158a0481257669454b689bb', '99341eb6f3e0e07888800383e865d15593263a36219e33b8fada3f4f706fa2c3', 'd13a4c4f781402aedc42ded6a5afa6ec6ad210b13142e87f9e121ac9fc845f3b', 'f0c85ab9c3050ecaac6e8937eb61d80fc9d81fa9cb26b295e5ee455b5d4f8b2f'}}
# Phase 4 source/synthetic fixtures: exact reviewed bytes; secret checks still run.
REVIEWED_SYNTHETIC_SOURCES.update({
    'src/enrich_2026.py': REVIEWED_SYNTHETIC_SOURCES.get('src/enrich_2026.py', set()) | {'b35a011895c15596ac1c6703687c1c0d5ff299dc1ca8a3600ff45681452a2c27'},
    'src/organization_enrichment.py': REVIEWED_SYNTHETIC_SOURCES.get('src/organization_enrichment.py', set()) | {'6830b165d084ea664f5f7093deceb1624729aa49152a60857b4aa90478c8a2cb'},
    'src/section46_live.py': REVIEWED_SYNTHETIC_SOURCES.get('src/section46_live.py', set()) | {'bc2075250640e26512a8454795c959702d13b925f6147143aa0a54a3fbfb6e52'},
    'src/private_runtime.py': REVIEWED_SYNTHETIC_SOURCES.get('src/private_runtime.py', set()) | {'2ee81f8e4be626abd13dadf35a8465dc39fc92252d367fcd9bf8f0344a909330'},
    'src/registry_sync.py': REVIEWED_SYNTHETIC_SOURCES.get('src/registry_sync.py', set()) | {'63b9e28fa4433eea10dbb7b1364003ad91f1f22246fcf3d759f59bffdca50dbe'},
    'src/section46_cli.py': REVIEWED_SYNTHETIC_SOURCES.get('src/section46_cli.py', set()) | {'6ff448360ccb7c96951a9482fa31eca554a1e8fe61e8fb97cd5d0a082a9c1f34'},
    'src/section46_recheck.py': REVIEWED_SYNTHETIC_SOURCES.get('src/section46_recheck.py', set()) | {'4a275b65dbed742b67adef2ca8dacf049a5aea794d9b4bf5fa56bece4eab475a'},
    'tests/test_registry_root.py': REVIEWED_SYNTHETIC_SOURCES.get('tests/test_registry_root.py', set()) | {'1d47439598f0e5256772ff0285d7d170c8168b215ff521ae91e454485b886566'},
    'tests/test_section46_recheck.py': REVIEWED_SYNTHETIC_SOURCES.get('tests/test_section46_recheck.py', set()) | {'0c14835088da43e9d1cfbad947d1d895d9d7e683986ccf0303082246f283c310'},
    'tests/test_registry_recovery.py': REVIEWED_SYNTHETIC_SOURCES.get('tests/test_registry_recovery.py', set()) | {'0220f735c6a524376194fcdf6a195974ef68dc5ecc48c3ea52fe62c92e157b61'},
    'tests/test_section46_live.py': REVIEWED_SYNTHETIC_SOURCES.get('tests/test_section46_live.py', set()) | {'ff6bcca184de9873a729b1a5d005cc0d05b8914d9665bf8d36cd73001a4d4d8c'},
})

SANITIZED_TEXT = {'agent_loop/tasks/organization_cache.md': {'2eee4b737c6d8fcc6ad6bcc63466db342073468bac3e464aea3a182350baa5cd', 'a9120984e74b4fbade540d3127aaa6441f9877be7ebee3a3cdf1cbeabf79392d', 'b8d2a677655a1fc3e05ef36fa70ade8cd2c7371cd6b998ad0dfdd8f0b3ca8c6c'}, 'agent_loop/tasks/official_registry.md': {'f984b37df9462111d6f563b6cb3dae23e4d546fec181193cbe99ee558b5c9489'}, 'agent_loop/acceptance_ids.json': {'ecd82f5016ec4b60a80c4247a01bc14b325e38d30ae0419a39d03939c82d5e7b'}, 'docs/AGENT_LOOP_PRODUCT.md': {'bc165a585d87602ecd88ea7334ae2c74525e66fec89aaefe7d4aea8e8ecde132'}, 'docs/OFFLINE_ORGANIZATION_ENRICHMENT.md': {'557e532a51dc78a8b37081f94b731a75c2ebd34b45a1696f3ca7a94ea02e1545'}}
SANITIZED_TEXT.update({'docs/PHASE4.md': {'9f7d72a739cff94cb67e0b20fb73a606b57eee9c2c444b4529ab6118abd761ed'}})
PRIVATE_KEYS = frozenset(bytes.fromhex('6f7267616e697a6174696f6e73 7461785f65766964656e6365 617070726f76616c5f706572696f6473 64656e69616c5f706572696f6473 7265676973747279 7265636f726473 71756172616e74696e65 6f6666696369616c5f6e616d65 726567697374726174696f6e5f6e756d626572 7461785f66696c655f6e756d626572 65766964656e63655f736861323536 6163636573735f746f6b656e 726566726573685f746f6b656e 636c69656e745f736563726574 707269766174655f6b6579 646f6e6f725f6e616d65 646f6e6f725f6964'.replace(' ', '20')).decode('ascii').split())
OID_PATTERN = '(?:[0-9a-f]{40}|[0-9a-f]{64})'

def _marker(text):
    key = '["\\\']?(?:' + '|'.join(sorted(PRIVATE_KEYS)) + ')["\\\']?'
    pattern = '(?<![A-Za-z0-9_-])' + key + '\\s*(?::|=|>)' + '|<' + key + '(?=\\s|/|>)' + '|^[ \\t]*\\[\\[?\\s*' + key + '(?=\\s*[\\].])' + '|^[ \\t]*' + key + '\\s*\\.[\\w.-]+\\s*='
    if re.search(pattern, text, re.I | re.M):
        return True
    for line in text.splitlines():
        if line.lstrip().startswith('['):
            try:
                pending = [tomllib.loads(line)]
                while pending:
                    item = pending.pop()
                    if isinstance(item, dict):
                        if any((k.lower() in PRIVATE_KEYS for k in item)):
                            return True
                        pending.extend(item.values())
                    elif isinstance(item, list):
                        pending.extend(item)
            except tomllib.TOMLDecodeError:
                pass
            except RecursionError:
                return True
        if re.search('[,;\\t|]', line):
            cells = re.split('[,;\\t|]', line)
            if any((cell.strip().strip('"\'').strip().lower() in PRIVATE_KEYS for cell in cells)):
                return True
    return False

def _json(text):
    return json.loads(text, object_pairs_hook=lambda pairs: pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))

def _inspect(value, depth=0):
    """Return (secret, operational, exhausted), including decoded strings."""
    if depth > 24:
        return (False, False, True)
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8-sig')
        except UnicodeError:
            return (False, True, False)
    if isinstance(value, str):
        secret = bool(SECRET.search(value))
        operational = value.lower() in PRIVATE_KEYS or _marker(value) or bool(re.search('(?<![A-Za-z0-9])[0-9]{9}(?![A-Za-z0-9])', value))
        try:
            decoded = _json(value)
        except (ValueError, RecursionError):
            return (secret, operational, False)
        if isinstance(decoded, str) and decoded == value:
            return (secret, operational, False)
        child = _inspect(decoded, depth + 1)
        return (secret or child[0], operational or child[1], child[2])
    if type(value) is int:
        return (False, 10 ** 8 <= value < 10 ** 9, False)
    if isinstance(value, dict):
        values = list(value.keys()) + list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return (False, False, False)
    secret = operational = exhausted = False
    for item in values:
        result = _inspect(item, depth + 1)
        secret |= result[0]
        operational |= result[1]
        exhausted |= result[2]
    return (secret, operational, exhausted)

def _check_blob(name, raw, mode):
    if not isinstance(name, str) or not isinstance(raw, bytes):
        return 'invalid_blob_input'
    path = PurePosixPath(name)
    if name in {'.githooks/pre-commit', '.githooks/pre-push'} and mode != '100755':
        return 'non_executable_hook'
    if mode not in {'100644', '100755'} or not name or name.startswith('/') or ('\\' in name) or any((ord(c) < 32 or ord(c) == 127 for c in name)) or any((p in {'', '.', '..'} for p in name.split('/'))) or any((part.lower() in PRIVATE_PARTS for part in path.parts)) or (path.suffix.lower() in PRIVATE_SUFFIXES) or path.name.lower().startswith(('.env', 'credentials', '_'.join(('client', 'secret')), 'token')):
        return 'private_path_or_mode'
    if len(raw) > MAX_BLOB:
        return 'oversize_blob'
    if SECRET.search(raw.decode('latin-1')):
        return 'secret_or_binary_content'
    if name in SYNTHETIC_BINARIES:
        return None if hashlib.sha256(raw).hexdigest() == SYNTHETIC_BINARIES[name] else 'modified_synthetic_binary'
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in SAFE_NAMES:
        return 'unrecognized_file_type'
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        return 'non_utf8_blob'
    if any((ord(c) < 32 and c not in '\t\r\n\x0c' for c in text)):
        return 'secret_or_binary_content'
    try:
        document = _json(text)
    except (ValueError, RecursionError):
        if path.suffix.lower() == '.json':
            return 'malformed_json_content'
        document = None
    secret, operational, exhausted = _inspect(text)
    for line in text.splitlines():
        result = _inspect(line)
        secret |= result[0]
        operational |= result[1]
        exhausted |= result[2]
    result = _inspect(document)
    secret |= result[0]
    operational |= result[1]
    exhausted |= result[2]
    tree = None
    if path.suffix.lower() == '.py':
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant):
                    result = _inspect(node.value)
                    secret |= result[0]
                    operational |= result[1]
                    exhausted |= result[2]
                if isinstance(node, ast.keyword) and node.arg and (node.arg.lower() in PRIVATE_KEYS):
                    operational = True
        except (SyntaxError, ValueError, TypeError, RecursionError):
            return 'unparseable_python'
    if secret:
        return 'secret_or_binary_content'
    if exhausted:
        return 'content_inspection_limit'
    digest = hashlib.sha256(raw).hexdigest()
    if digest in SANITIZED_TEXT.get(name, ()) or digest in REVIEWED_SYNTHETIC_SOURCES.get(name, ()):
        return None
    if path.suffix.lower() in {'.json', '.yaml', '.yml', '.toml', '.xml', '.txt'} and re.search('cache|evidence|credential', path.stem, re.I):
        return 'private_export_filename'
    if re.search('(?<![A-Za-z0-9])[0-9]{9}(?![A-Za-z0-9])', text):
        return 'unreviewed_identifier_content'
    scan_text = text
    for _ in range(8):
        scan_text = scan_text.replace('\\"', '"').replace("\\'", "'")
    if _marker(scan_text):
        return 'operational_text_content'
    if operational:
        return 'operational_python_literal' if tree is not None else 'operational_json_content'
    return None

def check_blob(name, raw, mode='100644'):
    """Return None or a bounded reason code; inspection failures block."""
    try:
        return _check_blob(name, raw, mode)
    except Exception:
        return 'content_inspection_failed'

def _validated_index(value):
    """Validate a hook-selected index; never substitute a different index."""
    if not isinstance(value, str) or not value or len(value) > 4096 or any((ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid index selection')
    selected = os.path.abspath(value)
    info = os.lstat(selected)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Invalid index file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(selected, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_size < 32:
            raise ValueError('Invalid index file')
        header = os.read(fd, 12)
        if len(header) != 12 or header[:4] != b'DIRC' or int.from_bytes(header[4:8], 'big') not in {2, 3, 4}:
            raise ValueError('Invalid index header')
    finally:
        os.close(fd)
    return selected

def _audit(repo, *, history=False, extra_revisions=()):
    extra_revisions = tuple(extra_revisions)
    if extra_revisions and (not history):
        raise ValueError('Revisions require history audit')
    if any((not isinstance(rev, str) or re.fullmatch(OID_PATTERN, rev) is None or (not rev.strip('0')) for rev in extra_revisions)):
        raise ValueError('Invalid revision')
    inherited = os.environ.copy()
    selected_index = None
    if 'GIT_INDEX_FILE' in inherited:
        selected_index = _validated_index(inherited['GIT_INDEX_FILE'])
    env = {key: value for key, value in inherited.items() if not key.startswith('GIT_')}
    env.update({'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'})
    index_env = env.copy()
    if selected_index is not None:
        index_env['GIT_INDEX_FILE'] = selected_index

    def git(*args, allowed=(0,), index=False):
        result = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args], check=False, capture_output=True, env=index_env if index else env, timeout=60)
        if getattr(result, 'returncode', None) not in allowed:
            raise ValueError('Git audit failed')
        if not isinstance(getattr(result, 'stdout', None), bytes):
            raise ValueError('Invalid Git output')
        return result.stdout

    def config_values(key):
        return git('config', '--local', '--get-regexp', key, allowed=(0, 1)).strip()
    top = git('rev-parse', '--show-toplevel').decode('utf-8').strip()
    if not top:
        raise ValueError('No work tree')
    repo = top
    if config_values('^extensions\\.partialclone$') or config_values('^remote\\..*\\.(promisor|partialclonefilter)$'):
        raise ValueError('Partial repository')
    if history and git('rev-parse', '--is-shallow-repository').strip() != b'false':
        raise ValueError('Incomplete history')
    checked = set()
    violations = set()
    count = 0

    def inspect(mode, oid, name, kind='blob'):
        key = (mode, oid, name)
        if key in checked:
            return
        checked.add(key)
        if kind != 'blob' or mode not in {'100644', '100755'}:
            violations.add('private_path_or_mode')
            return
        if re.fullmatch(OID_PATTERN, oid) is None:
            raise ValueError('Invalid object')
        size = git('cat-file', '-s', oid).strip()
        if not size.isdigit() or len(size) > 20:
            raise ValueError('Invalid object size')
        if int(size) > MAX_BLOB:
            violations.add('oversize_blob')
            return
        blob = git('cat-file', 'blob', oid)
        if len(blob) != int(size):
            raise ValueError('Inconsistent object')
        reason = check_blob(name.decode('utf-8'), blob, mode)
        if reason:
            violations.add(reason)
    entries = git('ls-files', '--full-name', '--stage', '-z', index=True)
    if entries and (not entries.endswith(b'\x00')):
        raise ValueError('Incomplete index listing')
    for entry in entries.split(b'\x00'):
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1)
        mode, oid, stage = meta.decode('ascii').split()
        count += 1
        if stage not in {'0', '1', '2', '3'}:
            raise ValueError('Invalid index stage')
        if stage != '0':
            violations.add('unmerged_index')
            continue
        inspect(mode, oid, name)
    if history:
        for revision in extra_revisions:
            if git('cat-file', '-t', revision + '^{}').strip() != b'commit':
                violations.add('non_commit_push_target')
        if 'non_commit_push_target' not in violations:
            refs = git('for-each-ref', '--format=%(refname)').decode('utf-8').splitlines()
            head = git('rev-parse', '--verify', '--quiet', 'HEAD', allowed=(0, 1)).strip()
            roots = ['--all']
            if head:
                if re.fullmatch(OID_PATTERN, head.decode('ascii')) is None:
                    raise ValueError('Invalid HEAD')
                roots.append('HEAD')
            else:
                symbolic = git('symbolic-ref', '-q', 'HEAD').decode('utf-8').strip()
                if not symbolic.startswith('refs/heads/') or symbolic in refs:
                    raise ValueError('Unresolved HEAD')
            roots.extend(extra_revisions)
            commits = git('rev-list', *roots).decode('ascii').splitlines()
            for commit in commits:
                if re.fullmatch(OID_PATTERN, commit) is None:
                    raise ValueError('Invalid commit listing')
                listing = git('ls-tree', '--full-tree', '-r', '-z', commit)
                if listing and (not listing.endswith(b'\x00')):
                    raise ValueError('Incomplete tree listing')
                for entry in listing.split(b'\x00'):
                    if not entry:
                        continue
                    meta, name = entry.split(b'\t', 1)
                    mode, kind, oid = meta.decode('ascii').split()
                    inspect(mode, oid, name, kind)
    return {'status': 'blocked' if violations else 'passed', 'index_entries': count, 'violations': sorted(violations)}

def audit(repo, *, history=False, extra_revisions=()):
    """Audit index blobs and commit trees; failures return sanitized blocked results."""
    try:
        return _audit(repo, history=history, extra_revisions=extra_revisions)
    except Exception:
        return {'status': 'blocked', 'index_entries': 0, 'violations': ['repository_audit_failed']}

def _push_revisions(stream):
    protocol = stream.read(65537)
    if isinstance(protocol, bytes):
        protocol = protocol.decode('ascii')
    if not isinstance(protocol, str) or len(protocol) > 65536 or (not protocol.isascii()):
        raise ValueError('Invalid hook protocol')
    if protocol and (not protocol.endswith('\n')):
        raise ValueError('Invalid hook protocol')

    def valid_ref(ref):
        if not ref.startswith('refs/') or ref.endswith(('/', '.', '.lock')):
            return False
        return not any((c.isspace() or ord(c) < 32 or ord(c) == 127 or (c in '~^:?*[\\') for c in ref)) and (not any((x in ref for x in ('..', '@{', '//')))) and all((part and (not part.startswith('.')) and (not part.endswith('.lock')) for part in ref.split('/')))
    revisions = []
    for line in protocol.split('\n')[:-1]:
        parts = line.split(' ')
        if len(parts) != 4:
            raise ValueError('Invalid hook protocol')
        local, oid, remote, old = parts
        if re.fullmatch(OID_PATTERN, oid) is None or re.fullmatch('[0-9a-f]{' + str(len(oid)) + '}', old) is None or (not valid_ref(remote)) or (not (valid_ref(local) or local in {'(delete)', 'HEAD'} or re.fullmatch('[0-9a-f]{' + str(len(oid)) + '}', local))):
            raise ValueError('Invalid hook protocol')
        deleted = not oid.strip('0')
        if deleted != (local == '(delete)'):
            raise ValueError('Invalid hook protocol')
        if not deleted:
            revisions.append(oid)
    return tuple(dict.fromkeys(revisions))

class _ArgumentParser(argparse.ArgumentParser):

    def error(self, message):
        self.exit(2, 'Private-data guard: BLOCKED (invalid arguments)\n')

def main(argv=None):
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--pre-push-input', action='store_true')
    args = parser.parse_args(argv)
    try:
        revisions = _push_revisions(getattr(sys.stdin, 'buffer', sys.stdin)) if args.pre_push_input else ()
        result = audit(args.repo, history=args.history or args.pre_push_input, extra_revisions=revisions)
    except Exception:
        print('Private-data guard: BLOCKED (repository could not be safely audited)')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'passed' else 1
if __name__ == '__main__':
    raise SystemExit(main())
