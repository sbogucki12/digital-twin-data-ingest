"""CLI entry point: python -m digital_twin_ingest {auth|run|costs}"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_file_config, load_settings


def _cmd_auth(args: argparse.Namespace) -> int:
    from .drive import DriveClient, build_service
    from .google_auth import get_credentials

    s = load_settings()
    creds = get_credentials(s.client_secret_file, s.token_file, interactive=True)
    drive = DriveClient(build_service(creds))
    drive.assert_folder(s.inbox_folder_id, "Inbox")
    drive.assert_folder(s.root_folder_id, "Digital_Twin root")
    print("Google auth OK; Inbox and Digital_Twin root folders are reachable.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from .analyzer import Analyzer, FatalAnalysisError
    from .cost import CostLedger
    from .drive import DriveClient, build_service
    from .google_auth import get_credentials
    from .pipeline import new_run_id, run

    s = load_settings()
    creds = get_credentials(s.client_secret_file, s.token_file, interactive=False)
    run_id = new_run_id()
    mode = "LIVE — writing to Drive" if args.live else "DRY RUN — nothing written to Drive"
    cap = s.ingest.max_cost_per_run_usd
    print(f"Run {run_id} ({mode}); model {s.anthropic.model}; spend cap ${cap:.2f}")

    with CostLedger(s.ingest.cost_db) as ledger:
        analyzer = Analyzer(s, ledger, run_id)
        try:
            report = run(
                s, DriveClient(build_service(creds)), analyzer, ledger,
                run_id=run_id, dry_run=not args.live, limit=args.limit, doc_id=args.doc_id,
            )
        except FatalAnalysisError as exc:
            print(f"Stopped: {exc}. Check ANTHROPIC_API_KEY.", file=sys.stderr)
            print(f"Spent this run: ${ledger.run_total(run_id):.6f}")
            return 2

    for r in report.results:
        if r.preview_path:
            extra = f" -> {r.preview_path}"
        elif r.output_file_id:
            extra = f" -> {r.destination} ({r.output_file_id})"
        else:
            extra = f" ({r.reason})" if r.reason else ""
        print(f"  [{r.status:>8}] {r.doc_id}  ${r.cost_usd:.6f}{extra}")
    print("Summary:", report.counts() or "no docs in Inbox")
    print(f"Spent this run: ${report.spent_usd:.6f} of ${cap:.2f} cap")
    if report.cap_reached:
        print("SPEND CAP REACHED: remaining docs left in Inbox for the next run.")
    return 1 if any(r.status == "failed" for r in report.results) else 0


def _cmd_costs(args: argparse.Namespace) -> int:
    import os

    from dotenv import load_dotenv

    from .cost import CostLedger

    load_dotenv(Path(".env"), override=False)
    cfg = load_file_config(Path(os.environ.get("INGEST_CONFIG", "config.local.yaml")))
    db = cfg.ingest.cost_db
    if not db.exists():
        print(f"No cost ledger yet at {db}.")
        return 0
    with CostLedger(db) as ledger:
        rows = ledger.run_summaries(limit=args.runs)
        print(f"{'run_id':<28} {'started (UTC)':<26} {'calls':>5} {'in tok':>10} {'out tok':>9} {'USD':>10}")
        for r in rows:
            print(
                f"{r['run_id']:<28} {r['started_utc']:<26} {r['calls']:>5} "
                f"{r['input_tokens']:>10} {r['output_tokens']:>9} {r['cost_usd']:>10.6f}"
            )
        print(f"All-time estimated spend: ${ledger.total_cost():.6f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digital_twin_ingest")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="Authorize Google access and verify folder IDs")

    p_run = sub.add_parser("run", help="Process the Inbox (dry run by default)")
    p_run.add_argument("--live", action="store_true",
                       help="Upload .md files and move sources to the processed folder")
    p_run.add_argument("--limit", type=int, help="Process at most N docs")
    p_run.add_argument("--doc-id", help="Process a single Google Doc by ID")

    p_costs = sub.add_parser("costs", help="Show estimated Anthropic API spend per run")
    p_costs.add_argument("--runs", type=int, default=20)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:  # keep third-party HTTP noise (which can include URLs) out of logs
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    handlers = {"auth": _cmd_auth, "run": _cmd_run, "costs": _cmd_costs}
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:  # AuthError, DriveError
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
