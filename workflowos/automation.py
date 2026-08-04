"""Runtime boundary for Monday document events and guarded SB email delivery."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any

from .core import SHA256_RE, WorkflowError, _require_mapping, _require_string
from .technical_review import (
    SB_COMPLETION_DOCUMENTS,
    SB_COMPLETION_HANDOFF,
    SB_DOCUMENT_HANDOFF,
    SB_HANDOFF_DOCUMENTS,
    configure_sb_document_handoff,
    record_monday_document_notification,
    record_sb_email_delivery,
)


AUTOMATION_MODES = {"test", "live"}
SUPPORTED_DOCUMENT_TYPES = set(SB_HANDOFF_DOCUMENTS + SB_COMPLETION_DOCUMENTS)
HANDOFF_KEYS = {
    "tag_grid_connection_application": "accepted_practices",
    "installation_notice_ia": "accepted_practices",
    "single_line_diagram": "accepted_practices",
    "safety_report_rasi_sina": "installation_completion",
}
HANDOFF_TYPES = {
    "accepted_practices": SB_DOCUMENT_HANDOFF["type"],
    "installation_completion": SB_COMPLETION_HANDOFF["type"],
}


def initialize_automation_state(
    plan: dict[str, Any], monday_case: dict[str, Any]
) -> dict[str, Any]:
    """Bind both SB handoffs to one authoritative Monday item."""

    case_id = _require_string(monday_case.get("case_id"), "monday_case.case_id")
    board_id = _require_string(monday_case.get("board_id"), "monday_case.board_id")
    item_id = _require_string(monday_case.get("item_id"), "monday_case.item_id")
    if plan.get("case_id") != case_id:
        raise WorkflowError("Automation plan and Monday case_id do not match")

    work_items = plan.get("work_items")
    if not isinstance(work_items, list):
        raise WorkflowError("plan.work_items must be a list")

    handoffs: dict[str, dict[str, Any]] = {}
    for key, handoff_type in HANDOFF_TYPES.items():
        matching = [
            item
            for item in work_items
            if isinstance(item, dict) and item.get("type") == handoff_type
        ]
        if len(matching) != 1:
            raise WorkflowError(
                f"Automation plan must contain one {handoff_type} work item"
            )
        handoffs[key] = configure_sb_document_handoff(matching[0], monday_case)

    return {
        "schema_version": "1.0",
        "source": "monday",
        "case_id": case_id,
        "board_id": board_id,
        "item_id": item_id,
        "handoffs": handoffs,
        "asset_locators": {},
        "last_result": {"status": "waiting_for_documents"},
    }


def handle_monday_file_event(
    state: dict[str, Any],
    event: dict[str, Any],
    verification: dict[str, Any],
    config: dict[str, Any],
    *,
    email_sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Process one file-column event without allowing an unverified email send."""

    runtime = _validate_config(config)
    updated = copy.deepcopy(state)
    if updated.get("source") != "monday":
        raise WorkflowError("Automation state source must be monday")

    state_board_id = _require_string(updated.get("board_id"), "state.board_id")
    if state_board_id != runtime["expected_board_id"]:
        raise WorkflowError("Automation state board does not match configured board")
    if event.get("source") != "monday":
        raise WorkflowError("Automation event source must be monday")
    if event.get("event_type") != "file_column_changed":
        raise WorkflowError("Automation event_type must be file_column_changed")

    for field in ("board_id", "item_id", "case_id"):
        observed = _require_string(event.get(field), f"event.{field}")
        expected = _require_string(updated.get(field), f"state.{field}")
        if observed != expected:
            raise WorkflowError(f"Automation event {field} does not match state")

    column_id = _require_string(event.get("column_id"), "event.column_id")
    document_type = runtime["document_columns"].get(column_id)
    if document_type is None:
        result = {
            "status": "ignored_unmapped_column",
            "column_id": column_id,
            "email_sent": False,
        }
        updated["last_result"] = result
        return {"state": updated, "result": result}

    event_ref = _require_sha256(event.get("event_ref_sha256"), "event.event_ref_sha256")
    attachment_ref = _require_sha256(
        event.get("attachment_ref_sha256"), "event.attachment_ref_sha256"
    )
    asset_locator = _require_string(event.get("asset_locator"), "event.asset_locator")

    processed_events = _require_mapping(
        updated.setdefault("processed_events", {}), "state.processed_events"
    )
    previous_attachment_ref = processed_events.get(event_ref)
    if previous_attachment_ref is not None:
        if previous_attachment_ref != attachment_ref:
            raise WorkflowError(
                "Duplicate Monday event has a different attachment reference"
            )
        result = {
            "status": "ignored_duplicate_event",
            "email_sent": False,
            "event_ref_sha256": event_ref,
        }
        updated["last_result"] = result
        return {"state": updated, "result": result}

    notification = {
        "sanitized": True,
        "source": "monday",
        "event_type": "document_uploaded",
        "document_type": document_type,
        "content_verified": verification.get("content_verified"),
        "latest_version": verification.get("latest_version"),
        "identity_extraction_verified": verification.get(
            "identity_extraction_verified"
        ),
        "document_identity": verification.get("document_identity"),
        "event_ref_sha256": event_ref,
        "attachment_ref_sha256": attachment_ref,
    }
    if document_type == "safety_report_rasi_sina":
        notification["professional_signoff_verified"] = verification.get(
            "professional_signoff_verified"
        )
        notification["installation_completed"] = event.get(
            "installation_completed"
        )
    else:
        notification["grid_operator_practices_accepted"] = event.get(
            "grid_operator_practices_accepted"
        )

    handoff_key = HANDOFF_KEYS[document_type]
    handoffs = _require_mapping(updated.get("handoffs"), "state.handoffs")
    handoff = _require_mapping(handoffs.get(handoff_key), f"state.handoffs.{handoff_key}")
    handoffs[handoff_key] = record_monday_document_notification(
        handoff, notification
    )
    asset_locators = _require_mapping(
        updated.get("asset_locators"), "state.asset_locators"
    )
    asset_locators[document_type] = asset_locator
    processed_events[event_ref] = attachment_ref

    current_handoff = handoffs[handoff_key]
    if current_handoff.get("status") == "completed":
        result = {
            "status": "already_completed",
            "handoff": handoff_key,
            "email_sent": False,
        }
        updated["last_result"] = result
        return {"state": updated, "result": result}

    if current_handoff.get("status") != "email_send_requested":
        result = {
            "status": current_handoff.get("status", "blocked"),
            "handoff": handoff_key,
            "email_sent": False,
            "document_identity_check": copy.deepcopy(
                current_handoff.get("document_identity_check")
            ),
        }
        updated["last_result"] = result
        return {"state": updated, "result": result}

    email_request = _build_email_request(current_handoff, asset_locators)
    if runtime["mode"] == "test":
        result = {
            "status": "ready_test_no_email_sent",
            "handoff": handoff_key,
            "email_sent": False,
            "email_request": email_request,
        }
        updated["last_result"] = result
        return {"state": updated, "result": result}

    if runtime["allow_external_email"] is not True:
        raise WorkflowError(
            "Live email delivery requires allow_external_email=true"
        )
    if email_sender is None:
        raise WorkflowError("Live email delivery requires an email_sender adapter")

    confirmation = email_sender(copy.deepcopy(email_request))
    if not isinstance(confirmation, dict):
        raise WorkflowError("email_sender must return a confirmation object")
    handoffs[handoff_key] = record_sb_email_delivery(
        current_handoff, confirmation
    )
    result = {
        "status": "completed",
        "handoff": handoff_key,
        "email_sent": True,
        "idempotency_key": email_request["idempotency_key"],
    }
    updated["last_result"] = result
    return {"state": updated, "result": result}


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    expected_board_id = _require_string(
        config.get("expected_board_id"), "config.expected_board_id"
    )
    mode = _require_string(config.get("mode"), "config.mode")
    if mode not in AUTOMATION_MODES:
        raise WorkflowError("config.mode must be test or live")
    allow_external_email = config.get("allow_external_email", False)
    if not isinstance(allow_external_email, bool):
        raise WorkflowError("config.allow_external_email must be a boolean")

    raw_columns = _require_mapping(
        config.get("document_columns"), "config.document_columns"
    )
    document_columns: dict[str, str] = {}
    for column_id, document_type_value in raw_columns.items():
        normalized_column_id = _require_string(column_id, "config.document_column_id")
        document_type = _require_string(
            document_type_value,
            f"config.document_columns.{normalized_column_id}",
        )
        if document_type not in SUPPORTED_DOCUMENT_TYPES:
            raise WorkflowError(f"Unsupported document column type: {document_type}")
        document_columns[normalized_column_id] = document_type
    if set(document_columns.values()) != SUPPORTED_DOCUMENT_TYPES:
        raise WorkflowError(
            "config.document_columns must map TAG, IA, schema and RaSi/SiNa"
        )

    return {
        "expected_board_id": expected_board_id,
        "mode": mode,
        "allow_external_email": allow_external_email,
        "document_columns": document_columns,
    }


def _build_email_request(
    handoff: dict[str, Any], asset_locators: dict[str, Any]
) -> dict[str, Any]:
    email_delivery = _require_mapping(
        handoff.get("email_delivery"), "handoff.email_delivery"
    )
    recipient_email = _require_string(
        email_delivery.get("recipient_email"), "handoff.recipient_email"
    )
    document_types = email_delivery.get("attachment_document_types")
    attachment_refs = email_delivery.get("attachment_refs_sha256")
    if not isinstance(document_types, list) or not isinstance(attachment_refs, list):
        raise WorkflowError("Email attachments must be lists")
    if len(document_types) != len(attachment_refs):
        raise WorkflowError("Email attachment types and refs must have equal length")

    locators: list[str] = []
    for document_type in document_types:
        locator = asset_locators.get(document_type)
        locators.append(
            _require_string(locator, f"asset_locator.{document_type}")
        )

    case_context = _require_mapping(handoff.get("case_context"), "case_context")
    case_id = _require_string(case_context.get("case_id"), "case_context.case_id")
    payload = {
        "case_id": case_id,
        "work_id": handoff.get("work_id"),
        "recipient_email": recipient_email,
        "attachment_refs_sha256": attachment_refs,
    }
    idempotency_key = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    template = (
        "accepted_grid_documents"
        if tuple(document_types) == SB_HANDOFF_DOCUMENTS
        else "signed_safety_report"
    )
    return {
        "source": "workflowos",
        "from_organization_role": _require_string(
            handoff.get("assigned_to_role"), "handoff.assigned_to_role"
        ),
        "recipient_name": email_delivery.get("recipient_name"),
        "recipient_email": recipient_email,
        "case_id": case_id,
        "template": template,
        "attachment_document_types": copy.deepcopy(document_types),
        "attachment_refs_sha256": copy.deepcopy(attachment_refs),
        "attachment_locators": locators,
        "idempotency_key": idempotency_key,
    }


def _require_sha256(value: Any, field: str) -> str:
    digest = _require_string(value, field)
    if not SHA256_RE.fullmatch(digest):
        raise WorkflowError(f"{field} must be a lowercase SHA-256 digest")
    return digest
