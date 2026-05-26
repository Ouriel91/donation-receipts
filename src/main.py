import argparse
from pathlib import Path

from src.config import HARNESS_DIR, MANIFEST_PATH, RECEIPTS_DIR
from src.harness_runner import run_harness


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--mode", required=True)
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.mode == "harness":
        run_harness(
            harness_dir=HARNESS_DIR,
            receipts_dir=RECEIPTS_DIR,
            manifest_path=MANIFEST_PATH,
            dry_run=args.dry_run,
        )
    else:
        print(f"Mode '{args.mode}' not yet implemented.")


if __name__ == "__main__":
    main()