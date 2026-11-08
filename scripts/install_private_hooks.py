"""Explicitly install or inspect repository-local private-data hooks."""
import argparse
import os
from pathlib import Path
import subprocess
from src.private_git_guard import audit


def configure(repo, *, install=False):
    repo = Path(repo).absolute()
    result = audit(repo, history=True)
    if result['status'] != 'passed':
        raise ValueError('Private-data audit blocks hook installation')
    current = subprocess.run(['git', '-C', str(repo), 'config', '--get', 'core.hooksPath'],
                             capture_output=True, text=True)
    if current.returncode not in (0, 1):
        raise ValueError('Cannot inspect hook configuration')
    path = current.stdout.strip()
    hooks_valid = all((repo / '.githooks' / name).is_file()
                      and not (repo / '.githooks' / name).is_symlink()
                      and not (repo / '.githooks').is_symlink()
                      and (os.name == 'nt' or os.access(repo / '.githooks' / name, os.X_OK))
                      for name in ('pre-commit', 'pre-push'))
    if install:
        if path not in ('', '.githooks'):
            raise ValueError('Existing hook configuration must be preserved')
        if not hooks_valid:
            raise ValueError('Missing, non-executable or linked hook')
        subprocess.run(['git', '-C', str(repo), 'config', '--local', 'core.hooksPath', '.githooks'],
                       check=True, capture_output=True)
        path = '.githooks'
    return {'audit': 'passed', 'hooks_active': path == '.githooks' and hooks_valid}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = configure(args.repo, install=args.install)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('Private hooks: BLOCKED; existing configuration preserved')
        return 1
    print('Private audit passed; hooks ' + ('active' if result['hooks_active'] else 'NOT installed'))
    return 0 if result['hooks_active'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
