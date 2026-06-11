import argparse
import sys
from pathlib import Path

from src.config import ACCOUNTS_DIR, HARNESS_DIR, MANIFEST_PATH, RECEIPTS_DIR
from src.gmail_runner import run_gmail
from src.harness_runner import run_harness
from src.providers.gmail_provider import GmailProvider
from src.providers.harness_provider import HarnessProvider


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--mode", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--account", default=None)

    args = parser.parse_args()

    if args.mode == "harness":
        run_harness(
            provider=HarnessProvider(HARNESS_DIR),
            receipts_dir=RECEIPTS_DIR,
            manifest_path=MANIFEST_PATH,
            dry_run=args.dry_run,
        )
    elif args.mode == "gmail":
        if not args.account:
            print("--account is required for gmail mode")
            sys.exit(1)
        run_gmail(
            provider=GmailProvider(ACCOUNTS_DIR / args.account),
            receipts_dir=RECEIPTS_DIR,
            manifest_path=MANIFEST_PATH,
            dry_run=args.dry_run,
        )
    else:
        print(f"Mode '{args.mode}' not yet implemented.")


if __name__ == "__main__":
    main()