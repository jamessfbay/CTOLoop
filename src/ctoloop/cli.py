from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys

from .config import load_settings
from .service import CTOLoopService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="CTOLoop governed GitHub-to-Codex automation",
    )
    result.add_argument(
        "--config", default=os.environ.get("CTOLOOP_CONFIG", "config.toml"),
        help="Path to TOML config (default: config.toml)",
    )
    result.add_argument("--verbose", action="store_true")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check GitHub, Git, Codex, and repository configuration")
    sub.add_parser("bootstrap", help="Run checks and create GitHub labels")
    once = sub.add_parser("once", help="Process ready issues once")
    once.add_argument("--issue", type=int, help="Process a specific issue number")
    once.add_argument(
        "--repository", help="Limit processing to one configured owner/repository",
    )
    patch_issue = sub.add_parser(
        "patch-issue", help="Turn one reviewed Issue into a validated candidate pull request",
    )
    patch_issue.add_argument("--issue", type=int, required=True, help="GitHub Issue number")
    patch_issue.add_argument(
        "--repository", help="Limit processing to one configured owner/repository",
    )
    review = sub.add_parser(
        "review-pr", help="Run a read-only Codex review of a pull request",
    )
    review.add_argument("--pr", type=int, required=True, help="GitHub pull request number")
    review.add_argument(
        "--repository", help="Limit processing to one configured owner/repository",
    )
    review.add_argument(
        "--publish", action="store_true", help="Post the generated report as a PR comment",
    )
    review.add_argument(
        "--json", action="store_true", help="Print the schema-validated JSON result",
    )
    diagnose = sub.add_parser(
        "diagnose-ci", help="Run a read-only diagnosis of a failed GitHub Actions run",
    )
    diagnose.add_argument("--run-id", type=int, help="GitHub Actions run database ID")
    diagnose.add_argument(
        "--pr", type=int,
        help="PR context; if --run-id is omitted, use its latest failed run",
    )
    diagnose.add_argument(
        "--repository", help="Limit processing to one configured owner/repository",
    )
    diagnose.add_argument(
        "--publish", action="store_true", help="Post the diagnosis as a PR comment",
    )
    diagnose.add_argument(
        "--json", action="store_true", help="Print the schema-validated JSON result",
    )
    audit = sub.add_parser(
        "security-audit",
        help="Run Cloudflare's multi-phase security audit in static-only mode",
    )
    audit.add_argument(
        "--repository", help="Limit auditing to one configured owner/repository",
    )
    audit.add_argument(
        "--profile", choices=("quick", "standard", "deep"),
        help="Audit profile (default: configured profile)",
    )
    audit.add_argument(
        "--scope", action="append", default=[], metavar="PATH",
        help="Repository-relative scope path; repeat for multiple paths",
    )
    audit.add_argument(
        "--budget", type=int, help="Maximum number of delegated agent invocations",
    )
    audit.add_argument(
        "--issue", type=int, help="GitHub Issue to associate with this audit",
    )
    audit.add_argument(
        "--publish", action="store_true", help="Post the audit summary to --issue",
    )
    audit.add_argument(
        "--json", action="store_true", help="Print machine-readable audit metadata",
    )
    update = sub.add_parser(
        "update", help="Fast-forward a production checkout and copy tracked files",
    )
    update.add_argument(
        "--repository", help="Update one configured owner/repository",
    )
    sub.add_parser("watch", help="Continuously poll for ready issues")
    history = sub.add_parser("history", help="Show recent run state or one run's decision trail")
    history.add_argument(
        "--run-id", type=int,
        help="Show the append-only workflow decision trail for one run",
    )
    workflow_status = sub.add_parser(
        "workflow-status", help="Show one durable workflow's persisted state",
    )
    workflow_status.add_argument("--id", required=True, help="Durable workflow ID")
    workflow_event = sub.add_parser(
        "workflow-event", help="Wake a waiting workflow with an idempotent external event",
    )
    workflow_event.add_argument("--id", required=True, help="Durable workflow ID")
    workflow_event.add_argument("--key", required=True, help="Globally idempotent event key")
    workflow_event.add_argument("--type", required=True, dest="event_type")
    workflow_event.add_argument("--resource", required=True)
    workflow_event.add_argument("--payload-json", default="{}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings(pathlib.Path(args.config))
        service = CTOLoopService(settings)
        if args.command == "doctor":
            failures = service.doctor()
            if failures:
                for failure in failures:
                    logging.error(failure)
                return 1
            logging.info("All checks passed")
        elif args.command == "bootstrap":
            service.bootstrap()
            logging.info("Bootstrap complete")
        elif args.command == "once":
            count = service.run_once(args.issue, args.repository)
            logging.info("Processed %d issue(s)", count)
        elif args.command == "patch-issue":
            count = service.run_once(args.issue, args.repository)
            logging.info("Processed %d issue(s)", count)
        elif args.command == "review-pr":
            print(service.review_pr(
                args.pr, args.repository, publish=args.publish, json_output=args.json,
            ))
        elif args.command == "diagnose-ci":
            print(service.diagnose_ci(
                args.run_id, args.repository, pr_number=args.pr, publish=args.publish,
                json_output=args.json,
            ))
        elif args.command == "security-audit":
            print(service.security_audit(
                args.repository,
                profile=args.profile,
                scope_paths=tuple(args.scope),
                budget=args.budget,
                issue_number=args.issue,
                publish=args.publish,
                json_output=args.json,
            ))
        elif args.command == "update":
            commit = service.update_production(args.repository)
            logging.info("Production files updated to %s", commit)
        elif args.command == "watch":
            service.watch()
        elif args.command == "history":
            rows = (
                service.state.decisions(args.run_id)
                if args.run_id is not None else service.state.recent()
            )
            for row in rows:
                print("\t".join(str(value or "") for value in row))
        elif args.command == "workflow-status":
            print(service.workflow_status(args.id))
        elif args.command == "workflow-event":
            payload = json.loads(args.payload_json)
            if not isinstance(payload, dict):
                raise ValueError("--payload-json must decode to an object")
            accepted = service.notify_workflow(
                args.id, event_key=args.key, event_type=args.event_type,
                resource_id=args.resource, payload=payload,
            )
            print(json.dumps({"accepted": accepted}))
        return 0
    except Exception as exc:
        logging.exception("CTOLoop failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
