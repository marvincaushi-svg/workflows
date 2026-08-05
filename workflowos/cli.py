"""Command-line interface for MERAVIQA's WorkflowOS engine."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from .audit import build_audit_log, read_audit_log, verify_audit_log, write_audit_log
from .control_plane import (
    AutomationCatalogError,
    build_daily_operations_brief,
    build_event_execution_plan,
    build_health_report,
    build_schedule_execution_plan,
    record_execution_outcome,
    resolve_communication_policy,
    validate_automation_catalog,
)
from .core import WorkflowError, build_case_from_email, evaluate_case, load_document
from .document_archive import (
    MondayArchiveReconciliation,
    archive_pdf_file,
    inspect_document_archive,
    reconcile_archived_monday_upload,
    retry_archived_monday_upload,
)
from .hostpoint_email import (
    HostpointSmtpConfig,
    check_hostpoint_connection,
    send_hostpoint_self_test,
)
from .monday_pipeline import (
    DeliveryReconciliation,
    inspect_delivery_outbox,
    reconcile_delivery_state,
)
from .monday_uploads import MondayGuardedUploader, MondayUploadConfig
from .technical_review import create_remediation_plan, evaluate_technical_review


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meraviqa",
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
        "--catalog", required=True, help="Tenant automation catalog path"
    )

    event_parser = subparsers.add_parser(
        "plan-automation-events",
        help="Build an idempotent execution plan from collector events",
    )
    event_parser.add_argument(
        "--catalog", required=True, help="Tenant automation catalog path"
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
        help="Find automations due in the tenant-configured timezone",
    )
    schedule_parser.add_argument(
        "--catalog", required=True, help="Tenant automation catalog path"
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
        "--catalog", required=True, help="Tenant automation catalog path"
    )
    health_parser.add_argument("--state", help="Optional runtime state JSON path")
    health_parser.add_argument(
        "--at", required=True, help="ISO-8601 report timestamp with timezone"
    )
    health_parser.add_argument(
        "--archive-root",
        help="Optional MERAVIQA archive root to include the document backlog",
    )

    brief_parser = subparsers.add_parser(
        "daily-operations-brief",
        help="Compose the morning brief of ready, blocked, retrying and failed work",
    )
    brief_parser.add_argument(
        "--catalog", required=True, help="Tenant automation catalog path"
    )
    brief_parser.add_argument("--state", help="Optional runtime state JSON path")
    brief_parser.add_argument(
        "--events", help="Optional JSON document containing an events list"
    )
    brief_parser.add_argument(
        "--archive-root",
        help="Optional MERAVIQA archive root to include the document backlog",
    )
    brief_parser.add_argument(
        "--at", required=True, help="ISO-8601 brief timestamp with timezone"
    )

    language_parser = subparsers.add_parser(
        "resolve-message-language",
        help="Resolve the recipient-specific language and Swiss spelling policy",
    )
    language_parser.add_argument(
        "--catalog", required=True, help="Tenant automation catalog path"
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
        help="Send one fixed test email to the configured tenant mailbox",
    )
    outbox_parser = subparsers.add_parser(
        "inspect-email-outbox",
        help="Inspect delivery state without showing email contents",
    )
    outbox_parser.add_argument(
        "--state", required=True, help="Automation state path"
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile-email-delivery",
        help="Record verified evidence for an uncertain email delivery",
    )
    reconcile_parser.add_argument(
        "--state", required=True, help="Automation state path"
    )
    reconcile_parser.add_argument("--idempotency-key", required=True)
    reconcile_parser.add_argument(
        "--outcome",
        required=True,
        choices=("confirmed-sent", "confirmed-not-sent"),
    )
    reconcile_parser.add_argument(
        "--checked-at",
        required=True,
        help="ISO-8601 evidence timestamp with timezone",
    )
    reconcile_parser.add_argument(
        "--checked-by-ref",
        required=True,
        help="Non-sensitive operator or procedure reference",
    )
    reconcile_parser.add_argument("--evidence-ref-sha256", required=True)
    reconcile_parser.add_argument("--message-ref-sha256")

    archive_parser = subparsers.add_parser(
        "inspect-document-archive",
        help="Inspect archived PDFs awaiting a Monday outcome",
    )
    archive_parser.add_argument(
        "--archive-root", required=True, help="MERAVIQA archive root path"
    )
    archive_parser.add_argument(
        "--tenant-id", help="Restrict the report to one tenant archive"
    )

    archive_reconcile_parser = subparsers.add_parser(
        "reconcile-monday-upload",
        help="Record verified evidence for an uncertain Monday archive upload",
    )
    archive_reconcile_parser.add_argument(
        "--archive-root", required=True, help="MERAVIQA archive root path"
    )
    archive_reconcile_parser.add_argument("--tenant-id", required=True)
    archive_reconcile_parser.add_argument(
        "--case-id", required=True, help="Non-sensitive case identifier"
    )
    archive_reconcile_parser.add_argument(
        "--content-sha256", required=True, help="SHA-256 of the archived PDF"
    )
    archive_reconcile_parser.add_argument(
        "--outcome",
        required=True,
        choices=("confirmed-uploaded", "confirmed-not-uploaded"),
    )
    archive_reconcile_parser.add_argument(
        "--checked-at",
        required=True,
        help="ISO-8601 evidence timestamp with timezone",
    )
    archive_reconcile_parser.add_argument(
        "--checked-by-ref",
        required=True,
        help="Non-sensitive operator or procedure reference",
    )
    archive_reconcile_parser.add_argument("--evidence-ref-sha256", required=True)

    upload_check_parser = subparsers.add_parser(
        "check-monday-upload",
        help="Verify the Monday write binding without uploading a file",
    )
    upload_check_parser.add_argument(
        "--item-id", required=True, help="Monday item id to verify"
    )

    retry_parser = subparsers.add_parser(
        "retry-monday-upload",
        help="Perform the single authorized retry for an archived PDF",
    )
    retry_parser.add_argument(
        "--archive-root", required=True, help="MERAVIQA archive root path"
    )
    retry_parser.add_argument("--tenant-id", required=True)
    retry_parser.add_argument(
        "--case-id", required=True, help="Non-sensitive case identifier"
    )
    retry_parser.add_argument(
        "--content-sha256", required=True, help="SHA-256 of the archived PDF"
    )

    archive_add_parser = subparsers.add_parser(
        "archive-document",
        help="Archive one verified PDF, optionally publishing it to Monday",
    )
    archive_add_parser.add_argument(
        "--archive-root", required=True, help="MERAVIQA archive root path"
    )
    archive_add_parser.add_argument("--tenant-id", required=True)
    archive_add_parser.add_argument(
        "--case-id", required=True, help="Non-sensitive case identifier"
    )
    archive_add_parser.add_argument(
        "--case-name", required=True, help="Project name used as the folder name"
    )
    archive_add_parser.add_argument("--monday-item-id", required=True)
    archive_add_parser.add_argument("--monday-column-id", required=True)
    archive_add_parser.add_argument("--document-type", required=True)
    archive_add_parser.add_argument(
        "--pdf", required=True, help="Path of the PDF to archive"
    )
    archive_add_parser.add_argument(
        "--publish-to-monday",
        action="store_true",
        help="Also upload to Monday; requires the upload switch",
    )

    outcome_parser = subparsers.add_parser(
        "record-automation-outcome",
        help="Record the outcome of one planned work item and persist state",
    )
    outcome_parser.add_argument(
        "--catalog", required=True, help="Tenant automation catalog path"
    )
    outcome_parser.add_argument(
        "--state", required=True, help="Runtime state JSON path, read and updated"
    )
    outcome_parser.add_argument(
        "--work-item", required=True, help="JSON path of one planned work item"
    )
    outcome_parser.add_argument(
        "--outcome", required=True, choices=("succeeded", "failed")
    )
    outcome_parser.add_argument(
        "--error-code", help="Required when the outcome is failed"
    )
    outcome_parser.add_argument(
        "--at", required=True, help="ISO-8601 outcome timestamp with timezone"
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


def _load_runtime_state_if_present(path: str) -> dict[str, object] | None:
    """Start from an empty state the first time an outcome is recorded."""

    return load_document(path) if Path(path).is_file() else None


def _write_runtime_state(path: str, state: dict[str, object]) -> None:
    """Persist control-plane runtime state atomically, in the shape it is read."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        state, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _archive_report(
    archive_root: str | None, catalog_document: dict[str, object]
) -> dict[str, object] | None:
    """Inspect the archive of the catalog tenant only, when a root is given."""

    if not archive_root:
        return None
    catalog = validate_automation_catalog(catalog_document)
    return inspect_document_archive(
        archive_root, tenant_id=catalog["tenant"]["id"]
    )


def _catalog_summary(catalog_path: str) -> dict[str, object]:
    catalog = validate_automation_catalog(load_document(catalog_path))
    enabled = sum(1 for item in catalog["automations"] if item["enabled"])
    return {
        "ok": True,
        "schema_version": catalog["schema_version"],
        "tenant_id": catalog["tenant"]["id"],
        "organization_name": catalog["tenant"]["organization_name"],
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
            catalog_document = load_document(args.catalog)
            result = build_health_report(
                catalog_document,
                state=_load_runtime_state(args.state),
                at=args.at,
                archive_report=_archive_report(
                    args.archive_root, catalog_document
                ),
            )
        elif args.command == "daily-operations-brief":
            catalog_document = load_document(args.catalog)
            events_document = (
                load_document(args.events) if args.events else {}
            )
            result = build_daily_operations_brief(
                catalog_document,
                state=_load_runtime_state(args.state),
                at=args.at,
                events=events_document.get("events", []),
                archive_report=_archive_report(
                    args.archive_root, catalog_document
                ),
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
        elif args.command == "inspect-email-outbox":
            result = inspect_delivery_outbox(args.state)
        elif args.command == "reconcile-email-delivery":
            reconciled = reconcile_delivery_state(
                args.state,
                DeliveryReconciliation(
                    idempotency_key=args.idempotency_key,
                    outcome=args.outcome.replace("-", "_"),
                    checked_at=args.checked_at,
                    checked_by_ref=args.checked_by_ref,
                    evidence_ref_sha256=args.evidence_ref_sha256,
                    message_ref_sha256=args.message_ref_sha256,
                ),
            )
            result = reconciled["result"]
        elif args.command == "inspect-document-archive":
            result = inspect_document_archive(
                args.archive_root, tenant_id=args.tenant_id
            )
        elif args.command == "reconcile-monday-upload":
            reconciled = reconcile_archived_monday_upload(
                args.archive_root,
                MondayArchiveReconciliation(
                    tenant_id=args.tenant_id,
                    case_id=args.case_id,
                    content_sha256=args.content_sha256,
                    outcome=args.outcome.replace("-", "_"),
                    checked_at=args.checked_at,
                    checked_by_ref=args.checked_by_ref,
                    evidence_ref_sha256=args.evidence_ref_sha256,
                ),
            )
            result = reconciled["result"]
        elif args.command == "check-monday-upload":
            config = MondayUploadConfig.from_environment(require_live_enabled=False)
            result = MondayGuardedUploader(config).verify_binding(args.item_id)
        elif args.command == "retry-monday-upload":
            uploader = MondayGuardedUploader(MondayUploadConfig.from_environment())
            retried = retry_archived_monday_upload(
                args.archive_root,
                args.tenant_id,
                args.case_id,
                args.content_sha256,
                publisher=uploader.publisher(),
                publisher_tenant_id=uploader.tenant_binding()["tenant_id"],
            )
            result = retried["result"]
        elif args.command == "archive-document":
            publisher = None
            publisher_tenant_id = None
            if args.publish_to_monday:
                uploader = MondayGuardedUploader(
                    MondayUploadConfig.from_environment()
                )
                publisher = uploader.publisher()
                publisher_tenant_id = uploader.tenant_binding()["tenant_id"]
            archived = archive_pdf_file(
                args.archive_root,
                args.pdf,
                tenant_id=args.tenant_id,
                case_id=args.case_id,
                case_name=args.case_name,
                monday_item_id=args.monday_item_id,
                monday_column_id=args.monday_column_id,
                document_type=args.document_type,
                publisher=publisher,
                publisher_tenant_id=publisher_tenant_id,
            )
            result = archived["result"]
        elif args.command == "record-automation-outcome":
            recorded = record_execution_outcome(
                load_document(args.catalog),
                load_document(args.work_item),
                state=_load_runtime_state_if_present(args.state),
                succeeded=args.outcome == "succeeded",
                at=args.at,
                error_code=args.error_code,
            )
            _write_runtime_state(args.state, recorded["state"])
            result = recorded["result"]
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
