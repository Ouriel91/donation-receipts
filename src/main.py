import argparse
from pathlib import Path

from src.harness_runner import run_harness


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--mode", required=True)
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.mode == "harness":
        run_harness(
            harness_dir=Path("harness/emails"),
            receipts_dir=Path("receipts"),
            manifest_path=Path("data/processed_messages.json"),
            dry_run=args.dry_run,
        )
    else:
        print(f"Mode '{args.mode}' not yet implemented.")


if __name__ == "__main__":
    main()