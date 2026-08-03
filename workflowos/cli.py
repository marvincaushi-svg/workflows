"""Command-line interface for the WorkflowOS MVP."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .audit import build_audit_log, read_audit_log, verify_audit_log, write_audit_log
from .core import WorkflowError, build_case_from_email, evaluate_case, load_document
from .hostpoint_email import HostpointSmtpConfig, check_hostpoint_connection
from .technical_review import create_remediation_plan, evaluate_technical_review


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflowos",
        description="Evaluate evidence-driven cases and produce verifiable audit logs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Create a case from sanitized email evidence and evaluate it"
    )
    run_parser.add_argument("--process", required=True, help="Process schema path")
    run_parser.add_argument("--email", required=True, help="Sanitized email evidence path")
    run_parser.add_argument("--case-id", required=True, help="Non-sensitive case identifier")
    run_parser.add_argument("--audit", required=True, help="Output JSONL audit path")
    run_parser.add_argument(
        "--at", required=True, help="ISO-8601 evaluation timestamp with timezone"
    )

    verify_parser = subparsers.add_parser(
        "verify-audit", help="Verify the audit log hash chain"
    )
    verify_parser.add_argument("--audit", required=True, help="JSONL audit path")

    request_parser = subparsers.add_parser(
        "create-af-work-plan",
        aliases=["create-af-technical-work"],
        help="Create ordered A&F-owned workstreams from a sanitized review",
    )
    request_parser.add_argument("--review", required=True, help="Technical review path")
    request_parser.add_argument("--case-id", required=True, help="Non-sensitive case identifier")
    request_parser.add_argument(
        "--at", required=True, help="ISO-8601 creation timestamp with timezone"
    )
    subparsers.add_parser(
        "check-hostpoint-smtp",
        help="Authenticate to Hostpoint SMTP without sending an email",
    )
    return parser


def _run(args: argparse.Namespace) -> dict[str, object]:
    process_document = load_document(args.process)
    email_document = load_document(args.email)
    case = build_case_from_email(process_document, email_document, args.case_id)
    evaluation = evaluate_case(process_document, case)
    events = build_audit_log(case, evaluation, args.at)
    write_audit_log(args.audit, events)
    verification = verify_audit_log(events)

    evaluated_case = dict(case)
    evaluated_case["status"] = evaluation["status"]
    return {
        "case": evaluated_case,
        "evaluation": evaluation,
        "audit": {"path": args.audit, **verification},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = _run(args)
        elif args.command == "verify-audit":
            result = verify_audit_log(read_audit_log(args.audit))
        elif args.command == "check-hostpoint-smtp":
            config = HostpointSmtpConfig.from_environment(
                require_live_enabled=False
            )
            result = check_hostpoint_connection(config)
        else:
            review = load_document(args.review)
            decision = evaluate_technical_review(review)
            result = create_remediation_plan(decision, args.case_id, args.at)
    except WorkflowError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
