"""Resolve user-private paths once, before any product I/O."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re

from src.private_runtime import new_bytes, registry_root, safe_path


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('Invalid user or account identifier')
    name = value.lower()
    if name in {'manual', 'combined', 'con', 'prn', 'aux', 'nul'} or re.fullmatch(r'(com|lpt)[0-9]', name):
        raise ValueError('Reserved user or account identifier')
    return name


def mailbox(value):
    if not isinstance(value, str) or not re.fullmatch(r'[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+', value):
        raise ValueError('A Gmail mailbox address is required')
    return value.casefold()


@dataclass(frozen=True)
class UserScope:
    user: str
    project: Path

    @classmethod
    def open(cls, user, project=None):
        return cls(identifier(user), safe_path(project if project is not None else Path(__file__).absolute().parents[1]))

    @property
    def accounts(self):
        return safe_path(self.project / 'accounts' / self.user)

    @property
    def receipts(self):
        return safe_path(self.project / 'receipts' / self.user)

    @property
    def reports(self):
        return safe_path(self.project / 'reports' / self.user)

    @property
    def runtime(self):
        configured = os.getenv('DONATION_RECEIPTS_PRIVATE_ROOT')
        base = configured if configured is not None else self.project / 'runtime'
        if not Path(base).is_absolute():
            raise ValueError('Private runtime root must be absolute')
        return safe_path(Path(base) / 'users' / self.user)

    @property
    def manifest(self):
        return safe_path(self.runtime / 'processed_messages.json')

    @property
    def registry(self):
        return safe_path(registry_root() / 'organizations.json')

    def create(self):
        self.accounts.mkdir(parents=True, exist_ok=True)
        new_bytes(self.accounts / '.user', (self.user + '\n').encode())

    def require(self):
        if safe_path(self.accounts / '.user').read_bytes() != (self.user + '\n').encode():
            raise ValueError('Invalid user data')

    def account(self, account):
        path = safe_path(self.accounts / identifier(account))
        for name in ('config.json', 'credentials.json', 'token.json', 'mailbox.txt'):
            safe_path(path / name)
        return path

    def claim_mailbox(self, account, address):
        key = hashlib.sha256(address.encode()).hexdigest()
        owner = safe_path(self.project / 'accounts' / '.mailboxes' / key)
        binding = f'{self.user}/{account}\n'.encode()
        try:
            new_bytes(owner, binding)
        except FileExistsError:
            if owner.read_bytes() != binding:
                raise ValueError('Mailbox already belongs to another configured account')

    def configure(self, account, address):
        self.require()
        account, address = identifier(account), mailbox(address)
        path = self.account(account)
        if (path / 'mailbox.txt').exists():
            if (path / 'mailbox.txt').read_text(encoding='utf-8').strip() != address:
                raise ValueError('Use a new account identifier for a different mailbox')
            self.require_account(account)
            return
        self.claim_mailbox(account, address)
        new_bytes(path / 'mailbox.txt', (address + '\n').encode())

    def identity_scope(self, account):
        if safe_path(self.account(account) / '.legacy-identity').is_file():
            return account
        return f'{self.user}/{account}'

    def require_account(self, account):
        path = self.account(account)
        address = mailbox(safe_path(path / 'mailbox.txt').read_text(encoding='utf-8').strip())
        key = hashlib.sha256(address.encode()).hexdigest()
        owner = safe_path(self.project / 'accounts' / '.mailboxes' / key)
        if owner.read_text(encoding='utf-8') != f'{self.user}/{account}\n':
            raise ValueError('Mailbox owner mismatch')
        safe_path(self.receipts / account)
        safe_path(self.reports / account)
        return path



def annual_report(reports_dir, account, year):
    if account == 'combined':
        return safe_path(Path(reports_dir) / account / str(year) / f'donation_{year}_combined.xlsx')
    name = 'manual_receipts' if account == 'manual' else 'donation_summary'
    return safe_path(Path(reports_dir) / account / str(year) / f'{name}_{year}.xlsx')
