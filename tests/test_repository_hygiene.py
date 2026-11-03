"""Synthetic checks for first-run setup and private-file exclusion."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.account_config import load_account_config

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_PATHS = [
    "accounts/new_account/config.json",
    "accounts/new_account/nested/private.json",
    "accounts/new_account/token.json",
    "secrets/credentials.json",
    "credentials.json",
    "nested/client_secret_demo.json",
    "nested/token.json",
    "receipts/new_account/2031/01_January/source.pdf",
    "reports/new_account/2031/_archive/backup.xlsx",
    "data/nested/runtime.json",
    "runtime/state.json",
    "tokens/local.json",
    "logs/run.log",
    "cache/content",
    "tmp/analysis.json",
    "temp/local.txt",
    ".env",
    ".env.local",
    ".venv/pyvenv.cfg",
    "nested/__pycache__/cached.pyc",
    ".pytest_cache/state",
    "_qa/review.json",
    "_archive/copy.xlsx",
    "backups/local.bundle",
    "donation_summary_2031.xlsx",
    "baseline_2031_diagnosis.json",
    "task_validation_synthetic.json",
]

PUBLIC_PATHS = [
    "examples/account-config.example.json",
    "harness/emails/donation_with_pdf.json",
    "harness/emails/donation_without_attachment.json",
    "harness/emails/normal_email.json",
    "harness/attachments/fake_receipt.pdf",
    "docs/RUNBOOK.md",
    "tests/test_receipt_summary.py",
]

@pytest.fixture
def ignore_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git is required to verify ignore behavior")
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    return tmp_path

@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_private_outputs_are_ignored(ignore_repo, path):
    result = subprocess.run(
        ["git", "-C", str(ignore_repo), "check-ignore", "--no-index", "--quiet", path],
        capture_output=True,
    )
    assert result.returncode == 0

@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_synthetic_fixtures_and_docs_are_not_ignored(ignore_repo, path):
    result = subprocess.run(
        ["git", "-C", str(ignore_repo), "check-ignore", "--no-index", "--quiet", path],
        capture_output=True,
    )
    assert result.returncode == 1

def test_generic_template_creates_an_unconfigured_local_account(tmp_path):
    accounts = tmp_path / "accounts"
    account = accounts / "new_account"
    account.mkdir(parents=True)
    shutil.copyfile(ROOT / "examples/account-config.example.json", account / "config.json")
    config = load_account_config(accounts, "new_account")
    assert config is not None
    assert config.display_name == "Example account"
    assert config.expected_donor_names == []
    assert config.expected_donor_ids == []
    assert json.loads((account / "config.json").read_text(encoding="utf-8")) == {
        "display_name": "Example account",
        "expected_donor_names": [],
        "expected_donor_ids": [],
    }
