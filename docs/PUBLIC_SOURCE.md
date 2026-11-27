# Public source checkout

The Windows launcher reads the checkout's source manifest. It lists 51 source
members, all byte-identical to the corresponding accepted runtime members. Git
attributes preserve these bytes on checkout. The installed Production
manifest has a separate identity and remains managed by its local source-update
package.

Use CPython 3.14 and create a virtual environment from the checkout. On Windows,
run these commands in PowerShell.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements-dev-lock.txt
.\.venv\Scripts\python.exe -m src.main --help
```

For development, substitute `requirements-dev.txt` for `requirements.txt`.
The constraints reproduce the verification dependency versions. On Linux,
select `python3.14` and use `.venv/bin/python` in the corresponding commands.
Dependency installation and targeted validation were exercised on Linux x86-64.

Local synthetic regression selection:

```text
python -m pytest -q -k "not product_delta and not productdelta"
```

Use the virtual environment's Python. The historical suite retains known
operator-disposed failures. The seven public equivalents preserve fixture
structure and assertions using generated values. The diagnostic probe for
the restricted acceptance tool runs with that tool in the separate owner
test environment.
