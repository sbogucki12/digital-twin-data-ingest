"""CLI entry point: python -m digital_twin_ingest {auth|run|migrate|costs}"""

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

    return _print_report(report, cap, empty_msg="no docs in Inbox",
                         cap_msg="remaining docs left in Inbox for the next run.")


def _print_report(report, cap: float, *, empty_msg: str, cap_msg: str, names=None) -> int:
    names = names or {}
    for r in report.results:
        if r.preview_path:
            extra = f" -> {r.preview_path}"
        elif r.output_file_id:
            extra = f" -> {r.destination} ({r.output_file_id})"
        else:
            extra = f" ({r.reason})" if r.reason else ""
        label = names.get(r.doc_id, r.doc_id)
        print(f"  [{r.status:>8}] {label}  ${r.cost_usd:.6f}{extra}")
    print("Summary:", report.counts() or empty_msg)
    print(f"Spent this run: ${report.spent_usd:.6f} of ${cap:.2f} cap")
    if report.cap_reached:
        print(f"SPEND CAP REACHED: {cap_msg}")
    return 1 if any(r.status == "failed" for r in report.results) else 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from .analyzer import Analyzer, FatalAnalysisError
    from .cost import CostLedger
    from .drive import DriveClient, build_service
    from .google_auth import get_credentials
    from .manifest import load_manifest, results_path, write_results
    from .migrate import migrate
    from .pipeline import new_run_id

    s = load_settings()
    manifest_path = Path(args.manifest)
    rows = load_manifest(manifest_path)
    creds = get_credentials(s.client_secret_file, s.token_file, interactive=False)
    run_id = new_run_id()
    mode = "LIVE — uploading + tagging originals in place" if args.live else "DRY RUN — nothing written to Drive"
    cap = s.ingest.max_cost_per_run_usd
    print(f"Migrate {run_id} ({mode}); {len(rows)} included row(s); "
          f"model {s.anthropic.model}; spend cap ${cap:.2f}")

    with CostLedger(s.ingest.cost_db) as ledger:
        analyzer = Analyzer(s, ledger, run_id)
        try:
            report = migrate(
                s, DriveClient(build_service(creds)), analyzer, ledger, rows,
                run_id=run_id, dry_run=not args.live, limit=args.limit,
            )
        except FatalAnalysisError as exc:
            print(f"Stopped: {exc}. Check ANTHROPIC_API_KEY.", file=sys.stderr)
            print(f"Spent this run: ${ledger.run_total(run_id):.6f}")
            return 2

    out = results_path(manifest_path)
    write_results(out, rows, report.results)
    code = _print_report(report, cap, empty_msg="no included rows",
                         cap_msg="re-run to continue; processed files are skipped automatically.",
                         names={r.id: r.name for r in rows})
    print(f"Results written to {out}")
    return code


def _load_kb(args: argparse.Namespace):
    import os

    from .validator import KnowledgeBase

    if args.drive:
        from .drive import DriveClient, build_service
        from .google_auth import get_credentials

        s = load_settings()
        creds = get_credentials(s.client_secret_file, s.token_file, interactive=False)
        drive = DriveClient(build_service(creds))
        return KnowledgeBase.from_drive(drive, drive.resolve_destinations(s.root_folder_id, s.destinations))
    if args.kb:
        return KnowledgeBase.from_directory(Path(args.kb))
    cfg_path = Path(os.environ.get("INGEST_CONFIG", "config.local.yaml"))
    root = load_file_config(cfg_path).ingest.preview_dir if cfg_path.exists() else Path("output")
    return KnowledgeBase.from_directory(root)


def _cmd_validate(args: argparse.Namespace) -> int:
    import json

    from .validator import ValidatorConfig, format_report, validate

    hits: list = list(args.doc or [])
    if args.retrieval:
        try:
            data = json.loads(Path(args.retrieval).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Error: cannot read retrieval file: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, list):
            print("Error: retrieval file must be a JSON list", file=sys.stderr)
            return 2
        hits.extend(data)
    draft = None
    if args.draft == "-":
        draft = sys.stdin.read()
    elif args.draft:
        draft = Path(args.draft).read_text(encoding="utf-8")

    try:
        kb = _load_kb(args)
        report = validate(args.query, hits, kb, draft, routed_category=args.category,
                          config=ValidatorConfig(strict=not args.lenient))
    except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2, default=str) if args.json else format_report(report))
    return 0 if report.approved else 1


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

    p_mig = sub.add_parser("migrate", help="Ingest files listed in a reviewed manifest (dry run by default)")
    p_mig.add_argument("--manifest", required=True, help="CSV manifest; only decision=include rows run")
    p_mig.add_argument("--live", action="store_true",
                       help="Upload .md files and tag originals as ingested (originals are not moved)")
    p_mig.add_argument("--limit", type=int, help="Process at most N files (skipped files don't count)")

    p_costs = sub.add_parser("costs", help="Show estimated Anthropic API spend per run")
    p_costs.add_argument("--runs", type=int, default=20)

    p_val = sub.add_parser("validate", help="Agent 2: audit retrieval + check a draft for hallucinations "
                                            "(offline, no API calls)")
    p_val.add_argument("--query", required=True, help="The user's question")
    p_val.add_argument("--doc", action="append", metavar="NOTE_ID",
                       help="A retrieved note ('<Category>/<file>.md' or a bare filename); repeatable")
    p_val.add_argument("--retrieval", metavar="JSON",
                       help="Agent 1 output: JSON list of {note_id, category?, matched_tags?}")
    p_val.add_argument("--draft", metavar="PATH", help="Drafted response to fact-check ('-' = stdin)")
    p_val.add_argument("--category", help="Category Agent 1 routed the query to")
    src = p_val.add_mutually_exclusive_group()
    src.add_argument("--kb", metavar="DIR", help="Local knowledge base directory (default: preview_dir)")
    src.add_argument("--drive", action="store_true", help="Read the knowledge base from Drive (read-only)")
    p_val.add_argument("--lenient", action="store_true", help="Only 'unsupported' claims fail (not 'weak')")
    p_val.add_argument("--json", action="store_true", help="Machine-readable report")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:  # keep third-party HTTP noise (which can include URLs) out of logs
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    handlers = {"auth": _cmd_auth, "run": _cmd_run, "migrate": _cmd_migrate, "costs": _cmd_costs,
                "validate": _cmd_validate}
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
