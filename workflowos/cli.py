"""Command-line interface for WorkflowOS."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .audit import build_audit_log, read_audit_log, verify_audit_log, write_audit_log
from .control_plane import (
    AutomationCatalogError,
    build_event_execution_plan,
    build_health_report,
    build_schedule_execution_plan,
    resolve_communication_policy,
    validate_automation_catalog,
)
from .core import WorkflowError, build_case_from_email, evaluate_case, load_document
from .hostpoint_email import (
    HostpointSmtpConfig,
    check_hostpoint_connection,
    send_hostpoint_self_test,
)
from .technical_review import create_remediation_plan, evaluate_technical_review


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflowos",
        description=(
            "Evaluate evidence-driven cases and coordinate deterministic automations."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Create a case from sanitized email evidence and evaluate it"
    )
    run_parser.add_argument("--process", required=True, help="Process schema path")
    run_parser.add_argument("--email", required=True, help="Sanitized email evidence path")
    run_parser.add_argument(
        "--case-id", required=True, help="Non-sensitive case identifier"
    )
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
    request_parser.add_argument(
        "--case-id", required=True, help="Non-sensitive case identifier"
    )
    request_parser.add_argument(
        "--at", required=True, help="ISO-8601 creation timestamp with timezone"
    )

    catalog_parser = subparsers.add_parser(
        "validate-automation-catalog",
        help="Validate collectors, dependencies, safety guards and schedules",
    )
    catalog_parser.add_argument(
        "--catalog", default="automation.catalog.json", help="Automation catalog path"
    )

    event_parser = subparsers.add_parser(
        "plan-automation-events",
        help="Build an idempotent execution plan from collector events",
    )
    event_parser.add_argument(
        "--catalog", default="automation.catalog.json", help="Automation catalog path"
    )
    event_parser.add_argument(
        "--events", required=True, help="JSON document containing an events list"
    )
    event_parser.add_argument("--state", help="Optional runtime state JSON path")
    event_parser.add_argument(
        "--at", required=True, help="ISO-8601 planning timestamp with timezone"
    )

    schedule_parser = subparsers.add_parser(
        "plan-automation-schedule",
        help="Find schedule automations due in the Europe/Zurich minute",
    )
    schedule_parser.add_argument(
        "--catalog", default="automation.catalog.json", help="Automation catalog path"
    )
    schedule_parser.add_argument("--state", help="Optional runtime state JSON path")
    schedule_parser.add_argument(
        "--at", required=True, help="ISO-8601 planning timestamp with timezone"
    )

    health_parser = subparsers.add_parser(
        "automation-health",
        help="Report due retries and dead-letter automation failures",
    )
    health_parser.add_argument(
        "--catalog", default="automation.catalog.json", help="Automation catalog path"
    )
    health_parser.add_argument("--state", help="Optional runtime state JSON path")
    health_parser.add_argument(
        "--at", required=True, help="ISO-8601 report timestamp with timezone"
    )

    language_parser = subparsers.add_parser(
        "resolve-message-language",
        help="Resolve the recipient-specific language and Swiss spelling policy",
    )
    language_parser.add_argument(
        "--catalog", default="automation.catalog.json", help="Automation catalog path"
    )
    language_parser.add_argument(
        "--recipient", required=True, help="Recipient email address"
    )

    subparsers.add_parser(
        "check-hostpoint-smtp",
        help="Authenticate to Hostpoint SMTP without sending an email",
    )
    subparsers.add_parser(
        "send-hostpoint-self-test",
        help="Send one fixed test email from and to the A&F mailbox",
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


def _load_runtime_state(path: str | None) -> dict[str, object] | None:
    return load_document(path) if path else None


def _catalog_summary(catalog_path: str) -> dict[str, object]:
    catalog = validate_automation_catalog(load_document(catalog_path))
    enabled = sum(1 for item in catalog["automations"] if item["enabled"])
    return {
        "ok": True,
        "schema_version": catalog["schema_version"],
        "timezone": catalog["timezone"],
        "collectors": len(catalog["collectors"]),
        "automations": len(catalog["automations"]),
        "enabled_automations": enabled,
        "guarded_send_automations": sum(
            1
            for item in catalog["automations"]
            if item["action"]["mode"] == "guarded_send"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = _run(args)
        elif args.command == "verify-audit":
            result = verify_audit_log(read_audit_log(args.audit))
        elif args.command in {"create-af-work-plan", "create-af-technical-work"}:
            review = load_document(args.review)
            decision = evaluate_technical_review(review)
            result = create_remediation_plan(decision, args.case_id, args.at)
        elif args.command == "validate-automation-catalog":
            result = _catalog_summary(args.catalog)
        elif args.command == "plan-automation-events":
            events_document = load_document(args.events)
            result = build_event_execution_plan(
                load_document(args.catalog),
                events_document.get("events", []),
                state=_load_runtime_state(args.state),
                at=args.at,
            )
        elif args.command == "plan-automation-schedule":
            result = build_schedule_execution_plan(
                load_document(args.catalog),
                state=_load_runtime_state(args.state),
                at=args.at,
            )
        elif args.command == "automation-health":
            result = build_health_report(
                load_document(args.catalog),
                state=_load_runtime_state(args.state),
                at=args.at,
            )
        elif args.command == "resolve-message-language":
            result = resolve_communication_policy(
                load_document(args.catalog), args.recipient
            )
        elif args.command == "check-hostpoint-smtp":
            config = HostpointSmtpConfig.from_environment(
                require_live_enabled=False
            )
            result = check_hostpoint_connection(config)
        elif args.command == "send-hostpoint-self-test":
            config = HostpointSmtpConfig.from_environment()
            result = send_hostpoint_self_test(config)
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
    except (WorkflowError, AutomationCatalogError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
