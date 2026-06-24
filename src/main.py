import argparse
import shutil
import sys
from pathlib import Path

from src.config import ACCOUNTS_DIR, HARNESS_DIR, MANIFEST_PATH, RECEIPTS_DIR, REPORTS_DIR
from src.gmail_runner import run_gmail
from src.harness_runner import run_harness
from src.providers.gmail_provider import GmailProvider
from src.providers.harness_provider import HarnessProvider


def _rebuild_year_dir(
    receipts_dir: Path, account: str, year: int, dry_run: bool = False
) -> None:
    target = receipts_dir / account / str(year)
    if not target.exists():
        print(f"[rebuild] {target} does not exist, skipping")
        return
    if dry_run:
        print(f"[rebuild] would delete {target} (dry run)")
        return

    # Collect all descendant paths once, deepest first.
    all_paths = sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True)

    for path in all_paths:
        if path.is_file():
            try:
                path.unlink()
            except OSError as e:
                print(f"[rebuild] warning: could not delete {path}: {e}")

    for path in all_paths:
        if path.is_dir():
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"[rebuild] warning: could not remove directory {path}: {e}")

    try:
        target.rmdir()
        print(f"[rebuild] deleted {target}")
    except FileNotFoundError:
        print(f"[rebuild] deleted {target}")
    except OSError as e:
        print(f"[rebuild] warning: {target} could not be fully removed: {e}")


def _compute_gmail_stats(results: list[dict]) -> dict:
    stats = {
        "emails_fetched": len(results),
        "processed": 0,
        "saved_receipts": 0,
        "skipped_duplicate": 0,
        "skipped_low_confidence": 0,
        "skipped_no_supported_attachments": 0,
        "errors": 0,
    }
    for r in results:
        s = r.get("status", "")
        if s in stats:
            stats[s] += 1
        if s == "processed":
            stats["saved_receipts"] += len(r.get("planned_paths", []))
        stats["errors"] += len(r.get("skipped_attachments", []))
    return stats


def _print_gmail_summary(stats: dict, dry_run: bool, query: str | None, account: str) -> None:
    col_w = len("skipped_no_supported_attachments")
    lines = [
        ("account", account),
        ("query", query or "(default)"),
        ("dry_run", str(dry_run)),
        None,
        ("emails_fetched", stats["emails_fetched"]),
        ("processed", stats["processed"]),
        ("saved_receipts", stats["saved_receipts"]),
        ("skipped_duplicate", stats["skipped_duplicate"]),
        ("skipped_low_confidence", stats["skipped_low_confidence"]),
        ("skipped_no_supported_attachments", stats["skipped_no_supported_attachments"]),
        ("errors (attachment failures)", stats["errors"]),
    ]
    print("\n[gmail summary]")
    for item in lines:
        if item is None:
            print()
        else:
            label, value = item
            print(f"  {label:<{col_w}} : {value}")
    print()


def _print_summary_stats(
    output_path: Path, counts: dict[str, int], account: str, year: int
) -> None:
    total = sum(counts.values())
    ready = counts.get("ready", 0)
    warning = counts.get("warning", 0)
    if total > 0:
        usability = f"{(ready + warning) / total * 100:.1f}%"
    else:
        usability = "N/A"
    col_w = len("total_receipts")
    print("\n[summary]")
    print(f"  {'account':<{col_w}} : {account}")
    print(f"  {'year':<{col_w}} : {year}")
    print(f"  {'workbook':<{col_w}} : {output_path}")
    print()
    print(f"  {'total_receipts':<{col_w}} : {total}")
    print(f"  {'ready':<{col_w}} : {ready}")
    print(f"  {'warning':<{col_w}} : {warning}")
    print(f"  {'critical':<{col_w}} : {counts.get('critical', 0)}")
    print(f"  {'usability':<{col_w}} : {usability}")
    if not total:
        print("  (no receipts found)")
    print()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--mode", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--account", default=None)
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--query", default=None,
                        help="Gmail search query (default: newer_than:7d has:attachment)")
    parser.add_argument("--reprocess", action="store_true",
                        help="Re-process messages already in the manifest (backfill mode)")
    parser.add_argument("--rebuild", action="store_true",
                        help="Delete receipts/<account>/<year>/ before running (implies --reprocess)")

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
        if args.rebuild and not args.year:
            print("--rebuild requires --year")
            sys.exit(1)
        if args.rebuild:
            _rebuild_year_dir(RECEIPTS_DIR, args.account, args.year, dry_run=args.dry_run)
        results = run_gmail(
            provider=GmailProvider(ACCOUNTS_DIR / args.account, query=args.query),
            receipts_dir=RECEIPTS_DIR,
            manifest_path=MANIFEST_PATH,
            dry_run=args.dry_run,
            reprocess=args.reprocess or args.rebuild,
        )
        _print_gmail_summary(
            _compute_gmail_stats(results),
            dry_run=args.dry_run,
            query=args.query,
            account=args.account,
        )
    elif args.mode == "summary":
        if not args.account:
            print("--account is required for summary mode")
            sys.exit(1)
        if not args.year:
            print("--year is required for summary mode")
            sys.exit(1)
        from src.account_config import load_account_config
        from src.receipt_summary import generate_summary_workbook
        config = load_account_config(ACCOUNTS_DIR, args.account)
        output_path, counts = generate_summary_workbook(
            receipts_dir=RECEIPTS_DIR,
            reports_dir=REPORTS_DIR,
            account=args.account,
            year=args.year,
            config=config,
        )
        _print_summary_stats(output_path, counts, account=args.account, year=args.year)
    else:
        print(f"Mode '{args.mode}' not yet implemented.")


if __name__ == "__main__":
    main()