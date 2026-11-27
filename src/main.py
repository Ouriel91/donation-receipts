import argparse
import re
import shutil
import sys
from pathlib import Path

from src.config import HARNESS_DIR
from src.user_scope import UserScope, identifier
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


def _gmail_query(year: int | None, query: str | None) -> str | None:
    if query is not None or year is None:
        return query
    return f"after:{year:04d}/01/01 before:{year + 1:04d}/01/01 has:attachment"


def _run_product(scope, year, account):
    import subprocess

    command = [sys.executable, '-B', '-m', 'src.main', '--user', scope.user, '--year', str(year)]
    accounts = [account] if account else [identifier(p.name) for p in scope.accounts.iterdir()
                                          if not p.name.startswith('.') and p.is_dir()]
    accounts.sort()
    for source in accounts:
        scope.require_account(source)
    for source in accounts:
        args = command + ['--account', source]
        subprocess.run(args + ['--mode', 'full'], cwd=scope.project, check=True)
        subprocess.run(args + ['--mode', 'potential-receipts'], cwd=scope.project, check=True)
    for mode in ('manual', 'combined'):
        subprocess.run(command + ['--mode', mode], cwd=scope.project, check=True)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--mode", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user")
    parser.add_argument("--mailbox")
    manual_queue = parser.add_mutually_exclusive_group()
    manual_queue.add_argument("--diagnose-review", action="store_true")
    manual_queue.add_argument("--retry-review", action="store_true")
    parser.add_argument("--fresh-extraction", action="store_true")
    parser.add_argument("--manual-decisions", type=Path)
    parser.add_argument("--account", default=None)
    parser.add_argument("--year", type=int, default=None,
                        help="In gmail mode, search this calendar year unless --query is supplied")
    parser.add_argument("--query", default=None,
                        help="Gmail search query (default: newer_than:7d has:attachment)")
    parser.add_argument("--reprocess", action="store_true",
                        help="Re-process messages already in the manifest (backfill mode)")
    parser.add_argument("--rebuild", action="store_true",
                        help="Delete receipts/<account>/<year>/ before running (implies --reprocess)")

    parser.add_argument("--relocate-report", action="store_true",
                        help="Move the existing potential receipts workbook locally")
    parser.add_argument("--delta", action="store_true", help="Checkpoint-based incremental discovery and publication")
    parser.add_argument("--plan", action="store_true", help="Sync only: read private checkpoint and pending counts without network or writes")
    parser.add_argument("--allowed-years", nargs="+", type=int, help="Sync only: authorized report years (default: requested year); checked before API/report writes")
    parser.add_argument("--resume-pending", action="store_true", help="Full or annual sync: reuse persisted source data without Gmail access")
    parser.add_argument("--refresh-base", action="store_true", help="Full resume only: build a fresh private base from current canonical and validated saved sources")
    parser.add_argument("--approve-identifier", nargs=4, action="append",
                        metavar=("SHA256", "FIELD", "VALUE", "PUBLISHED_BEFORE"),
                        help="Identifier review only: approve a local proposal with the verified workbook baseline (- for a blank value or baseline)")
    parser.add_argument("--clear-manual-tax", nargs=4, action="append",
                        metavar=("SHA256", "PUBLICATION_ID", "PUBLISHED_VALUE", "REVIEW_REASON"))
    parser.add_argument("--exclude-manual", nargs=2, metavar=("SHA256", "REVIEW_REASON"))
    parser.add_argument("--restore-manual", nargs=2, metavar=("SHA256", "REVIEW_REASON"))
    parser.add_argument("--approve-amount", nargs=4, action="append",
                        metavar=("SHA256", "PUBLICATION_ID", "VALUE", "PUBLISHED_BEFORE"))
    parser.add_argument("--approve-section46", nargs=4, action="append",
                        metavar=("PUBLICATION_ID", "PUBLISHED_BEFORE", "ORGANIZATION", "DATE"))
    parser.add_argument("--revoke-amount", action="append", metavar="SHA256")
    parser.add_argument("--revoke-section46", action="append", metavar="PUBLICATION_ID")
    parser.add_argument("--approve-oc-pair", nargs=2, action="append",
                        metavar=("ORIGINAL_PUBLICATION_ID", "COPY_PUBLICATION_ID"),
                        help="Attest that two locally reviewed numeric O/C PDFs represent one donation")
    parser.add_argument("--revoke-oc-pair", action="append", metavar="ORIGINAL_PUBLICATION_ID")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--preview-identifier", action="append", metavar="SHA256")
    selection.add_argument("--refresh-identifier", action="append", metavar="SHA256")
    args = parser.parse_args()
    if args.mode == "product" and args.delta:
        parser.error("Product supports Full processing in this release candidate")
    if args.rebuild:
        parser.error("Destructive rebuild is disabled for user data")
    if args.relocate_report and args.delta:
        parser.error("--relocate-report cannot be used with --delta")
    if not args.user:
        parser.error("Select a user with --user")
    try:
        scope = UserScope.open(args.user)
        manual_review = args.mode == 'identifier-review' and args.account == 'manual'
        if args.account is not None and not manual_review:
            args.account = identifier(args.account)
        if args.mode == 'create-user':
            scope.create()
            return
        scope.require()
        if args.mode == 'configure-account':
            scope.configure(args.account, args.mailbox)
            return
        if args.account and not manual_review:
            scope.require_account(args.account)
        accounts_dir = scope.accounts
        receipts_dir = scope.receipts
        reports_dir = scope.reports
        manifest_path = scope.manifest
    except (ValueError, OSError) as exc:
        parser.exit(1, f"User or account setup incomplete ({type(exc).__name__})\n")
    if args.exclude_manual or args.restore_manual:
        if (args.mode != 'manual' or args.account is not None or args.year is None or not 1900 <= args.year <= 9998
                or args.exclude_manual and args.restore_manual
                or args.diagnose_review or args.retry_review or args.fresh_extraction or args.manual_decisions):
            parser.error("Use one inclusion decision with --mode manual and --year")
        from src.manual_exclusion import record_exclusion
        sha, reason = args.exclude_manual or args.restore_manual
        record_exclusion(scope, args.year, sha, bool(args.exclude_manual), reason)
        return
    if args.diagnose_review or args.retry_review:
        if args.mode != 'manual' or (args.diagnose_review and args.fresh_extraction) or args.manual_decisions:
            parser.error("Manual queue operations require --mode manual without other review options")
    if args.mode in {'manual', 'combined', 'product'}:
        if args.year is None or not 1900 <= args.year <= 9998:
            parser.error("Select a report year")
        if args.mode != 'manual' and (args.fresh_extraction or args.manual_decisions):
            parser.error("Manual extraction and decisions require --mode manual")
        try:
            if args.mode == 'manual':
                from src.manual_receipts import diagnose_review, run_manual
                if args.diagnose_review:
                    import json
                    for row in diagnose_review(scope, args.year):
                        print(json.dumps(row, ensure_ascii=True))
                else:
                    print(run_manual(scope, args.year, fresh=args.fresh_extraction,
                                     decisions=args.manual_decisions, retry_review=args.retry_review))
            elif args.mode == 'combined':
                from src.combined_report import run_combined
                print(run_combined(scope, args.year))
            else:
                _run_product(scope, args.year, args.account)
        except Exception as exc:
            from src.sync_state import SyncError
            code = exc.code if isinstance(exc, SyncError) else type(exc).__name__
            parser.exit(1, f"Product operation incomplete ({code})\n")
        return
    if args.fresh_extraction or args.manual_decisions:
        parser.error("Manual extraction and decisions require --mode manual")
    mutation = any((args.clear_manual_tax, args.approve_identifier, args.approve_amount, args.approve_section46,
                    args.revoke_amount, args.revoke_section46, args.approve_oc_pair, args.revoke_oc_pair))
    selected = args.preview_identifier or args.refresh_identifier
    if selected and mutation:
        parser.error("Inspect or refresh sources separately from approving or revoking corrections")
    if any((args.clear_manual_tax, args.approve_identifier, args.approve_amount, args.approve_section46,
            args.revoke_amount, args.revoke_section46, args.approve_oc_pair, args.revoke_oc_pair, selected)) and args.mode != "identifier-review":
        parser.error("Historical approvals require --mode identifier-review")
    if args.relocate_report and args.mode != "potential-receipts":
        parser.error("--relocate-report requires --mode potential-receipts")
    if args.mode == "identifier-review":
        if not args.account or args.year is None:
            parser.error("identifier-review requires --account and --year")
        from src.sync_state import SyncError, refresh_selected_identifiers, review_identifiers
        import json
        from zipfile import BadZipFile
        try:
            decisions = [
                (sha, field, "" if value == "-" else value, "" if before == "-" else before)
                for sha, field, value, before in args.approve_identifier or ()
            ]
            amounts = [(sha, publication, value, "" if before == "-" else before)
                       for sha, publication, value, before in args.approve_amount or ()]
            if selected:
                result = refresh_selected_identifiers(
                    receipts_dir, reports_dir, accounts_dir, args.account, scope.runtime, args.year,
                    selected, preview=bool(args.preview_identifier),
                )
            else:
                result = review_identifiers(receipts_dir, args.account, scope.runtime, args.year,
                                            decisions, amounts=amounts, eligibility=args.approve_section46 or (),
                                            revoke_amounts=args.revoke_amount or (),
                                            revoke_eligibility=args.revoke_section46 or (), reports_dir=reports_dir,
                                            oc_pairs=args.approve_oc_pair or (), revoke_oc=args.revoke_oc_pair or (),
                                            public_registry=scope.registry, clear_manual_tax=args.clear_manual_tax or ())
        except (ValueError, OSError, BadZipFile) as exc:
            parser.exit(1, f"Identifier review stopped: {exc.code if isinstance(exc, SyncError) else type(exc).__name__}\n")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return

    if args.refresh_base and (args.mode != "full" or not args.resume_pending or args.plan):
        parser.error("--refresh-base requires --mode full --resume-pending without --plan")
    if args.mode == "potential-receipts":
        if (not args.account or not re.fullmatch(r"[A-Za-z0-9_-]+", args.account)
                or args.year is None or not 1900 <= args.year <= 9998
                or any((args.query, args.rebuild, args.reprocess, args.dry_run,
                        args.plan, args.resume_pending, args.allowed_years))
                or (args.relocate_report and args.delta)):
            parser.error("potential-receipts requires --account and --year, with optional --delta or --relocate-report")
        from src.potential_receipts import DiscoveryError, relocate_report, run_discovery
        try:
            if args.relocate_report:
                status = relocate_report(reports_dir, args.account, args.year)
                print(f"Potential receipts report: {status}")
            else:
                count = run_discovery(accounts_dir, reports_dir, args.account, args.year, delta=args.delta, scoped=True)
                print(f"Potential receipts added: {count}")
        except Exception as exc:
            code = str(exc) if isinstance(exc, DiscoveryError) else type(exc).__name__
            parser.exit(1, f"Potential receipts operation incomplete ({code})\n")
        return
    if args.resume_pending and (args.mode not in ("sync", "full") or args.delta):
        parser.error("--resume-pending requires --mode full or annual --mode sync without --delta")
    if args.mode not in ("sync", "full") and (args.plan or args.delta or args.allowed_years is not None):
        parser.error("--plan, --delta and --allowed-years require --mode sync")

    if args.mode == "full":
        if not args.account or args.year is None or not 1900 <= args.year <= 9998:
            parser.error('full requires --account and --year')
        if args.delta or args.rebuild or args.query or args.dry_run:
            parser.error('full uses the existing annual Gmail query; Delta remains in sync mode')
        if args.resume_pending and args.reprocess:
            parser.error('full --resume-pending cannot use --reprocess')
        from src.logger import RunDiagnostics, stage, error_type
        diagnostic = None
        try:
            with RunDiagnostics(enabled=not args.plan,
                    log_dir=scope.runtime / "logs", forbidden=(scope.project / "receipts", scope.project / "reports", scope.project / "accounts", scope.project / "src")) as diagnostic:
                if diagnostic.enabled:
                    label = 'primary' if args.account == 'primary' else 'configured'
                    diagnostic.line(f'[START] Donation Receipts | account={label} | year={args.year} | mode=full')
                stage('CONFIG')
                from src.sync import run_sync
                from src.sync_state import SyncError
                from src.account_config import load_account_config
                from datetime import datetime
                from zoneinfo import ZoneInfo
                config = load_account_config(accounts_dir, args.account)
                zone = ZoneInfo(config.operating_timezone if config else 'Asia/Jerusalem')
                start = datetime(args.year, 1, 1, tzinfo=zone)
                end = datetime(args.year+1, 1, 1, tzinfo=zone)
                query = f'has:attachment after:{int(start.timestamp())-1} before:{int(end.timestamp())}'
                counts = run_sync(provider=None if args.resume_pending else GmailProvider(accounts_dir / args.account,
                        query=query), receipts_dir=receipts_dir,
                        reports_dir=reports_dir, accounts_dir=accounts_dir,
                        manifest_path=manifest_path, account=args.account, year=args.year,
                        plan=args.plan, allowed_years=args.allowed_years,
                        root=scope.runtime, public_registry=scope.registry, user=scope.user, identity_scope=scope.identity_scope(args.account),
                        full_report=True, reprocess=args.reprocess, resume_pending=args.resume_pending, refresh_base=args.refresh_base)
                canonical = 'configured canonical workbook'
                diagnostic.done(counts, canonical)
        except Exception as exc:
            if diagnostic is None:
                print(f'[FAILED] stage=START error={error_type(exc)} log=unavailable; no Gmail operation started', flush=True)
            elif not diagnostic.reported_failure:
                diagnostic.failed(exc)
            sys.exit(1)
    elif args.mode == "sync":
        if not args.account or (args.year is None and not args.delta):
            parser.error("sync requires --account and --year (or --delta for dynamic years)")
        if args.rebuild or args.reprocess or args.query or args.dry_run:
            parser.error("sync does not support rebuild, reprocess, query or dry-run")
        from src.delta_sync import run_delta
        from src.sync_state import SyncError
        try:
            counts = run_delta(
                provider=GmailProvider(accounts_dir / args.account,
                    query="has:attachment"),
                receipts_dir=receipts_dir, reports_dir=reports_dir,
                accounts_dir=accounts_dir, manifest_path=manifest_path,
                account=args.account, year=args.year, root=scope.runtime,
                public_registry=scope.registry, identity_scope=scope.identity_scope(args.account), plan=args.plan, allowed_years=args.allowed_years, baseline=not args.delta, resume_pending=args.resume_pending)
        except Exception as exc:
            code = exc.code if isinstance(exc, SyncError) else type(exc).__name__
            print(f"Sync incomplete ({code}); inspect locally and rerun.")
            if isinstance(exc, SyncError) and code == 'gmail_discovery_interrupted':
                print(str(exc))  # Controlled aggregate counts only; no provider exception text.
            sys.exit(1)
        print("Sync complete: " + str(counts))
    elif args.mode == "harness":
        run_harness(
            provider=HarnessProvider(HARNESS_DIR),
            receipts_dir=receipts_dir,
            manifest_path=manifest_path,
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
            _rebuild_year_dir(receipts_dir, args.account, args.year, dry_run=args.dry_run)
        query = _gmail_query(args.year, args.query)
        results = run_gmail(
            provider=GmailProvider(accounts_dir / args.account, query=query),
            receipts_dir=receipts_dir,
            manifest_path=manifest_path,
            dry_run=args.dry_run,
            reprocess=args.reprocess or args.rebuild,
        )
        _print_gmail_summary(
            _compute_gmail_stats(results),
            dry_run=args.dry_run,
            query=query,
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
        canonical = reports_dir / args.account / str(args.year) / f'donation_summary_{args.year}.xlsx'
        from src.private_runtime import safe_path
        if safe_path(canonical).exists():
            parser.error("A canonical workbook already exists; use Full or Delta")
        config = load_account_config(accounts_dir, args.account)
        output_path, counts = generate_summary_workbook(
            receipts_dir=receipts_dir,
            reports_dir=reports_dir,
            account=args.account,
            year=args.year,
            config=config,
        )
        _print_summary_stats(output_path, counts, account=args.account, year=args.year)
    else:
        print(f"Mode '{args.mode}' not yet implemented.")


if __name__ == "__main__":
    main()
