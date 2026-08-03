"""Evidence-gated technical review for a document-ready electrical case."""

from __future__ import annotations

import copy
from typing import Any

from .core import (
    AF_TECHNICAL_OWNER_ROLE,
    CASE_ID_RE,
    SHA256_RE,
    SB_ASSIGNMENT_OWNER_ROLE,
    WorkflowError,
    _require_list,
    _require_mapping,
    _require_string,
    _require_timestamp,
)


REQUIRED_CONTROLS = (
    "tag_grid_connection_application",
    "installation_notice_ia",
    "single_line_diagram",
    "system_sizing",
    "cable_sizing",
    "ac_dc_protections",
    "earthing_bonding",
    "short_circuit_data",
    "maximum_dc_voltage",
    "battery_installation",
    "grid_operator_requirements",
    "commissioning_measurements",
)
VALID_RESULTS = {"verified", "missing", "non_compliant"}

CONTROL_DELIVERABLES = {
    "tag_grid_connection_application": "TAG grid-connection application",
    "installation_notice_ia": "IA installation notice",
    "single_line_diagram": "Single-line AC/DC diagram",
    "system_sizing": "PV system sizing and design calculations",
    "cable_sizing": "Cable sizing with sections, lengths and installation methods",
    "ac_dc_protections": "Coordinated AC/DC protection, RCD and SPD schedule",
    "earthing_bonding": "Earthing and equipotential bonding design",
    "short_circuit_data": "Prospective short-circuit values and breaking-capacity check",
    "maximum_dc_voltage": "Maximum DC voltage calculation at project minimum temperature",
    "battery_installation": "Battery location and installation conditions",
    "grid_operator_requirements": "Direct grid-operator coordination, submissions, approval and connection requirements",
    "commissioning_measurements": "Final inspection and measurement record",
}

WORKSTREAMS = (
    {
        "id": "technical-design",
        "type": "produce_af_technical_design",
        "control_ids": (
            "single_line_diagram",
            "system_sizing",
            "cable_sizing",
            "ac_dc_protections",
            "earthing_bonding",
            "short_circuit_data",
            "maximum_dc_voltage",
            "battery_installation",
        ),
        "depends_on": (),
    },
    {
        "id": "grid-operator-coordination",
        "type": "manage_grid_operator_coordination",
        "control_ids": (
            "tag_grid_connection_application",
            "installation_notice_ia",
            "grid_operator_requirements",
        ),
        "depends_on": ("technical-design",),
        "external_counterparty_role": "grid_operator",
    },
    {
        "id": "commissioning",
        "type": "perform_commissioning_verification",
        "control_ids": ("commissioning_measurements",),
        "depends_on": ("technical-design", "grid-operator-coordination"),
    },
)

SB_DOCUMENT_HANDOFF = {
    "id": "sb-document-handoff",
    "type": "publish_accepted_documentation_to_sb_monday",
    "depends_on": ("grid-operator-coordination",),
    "trigger": "grid_operator_practices_accepted",
}

SB_HANDOFF_DOCUMENTS = (
    "tag_grid_connection_application",
    "installation_notice_ia",
    "single_line_diagram",
)


def evaluate_technical_review(document: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable decision without inferring missing electrical data."""

    _require_string(document.get("schema_version"), "schema_version")
    if document.get("sanitized") is not True:
        raise WorkflowError("Public technical review input must declare sanitized=true")
    if document.get("assignment_intake_status") != "ready":
        raise WorkflowError("Technical review requires assignment_intake_status=ready")

    controls = _require_list(document.get("controls"), "controls")
    by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(controls):
        control = _require_mapping(value, f"controls[{index}]")
        control_id = _require_string(control.get("id"), f"controls[{index}].id")
        if control_id in by_id:
            raise WorkflowError(f"Duplicate technical control: {control_id}")
        result = _require_string(control.get("result"), f"controls[{index}].result")
        if result not in VALID_RESULTS:
            raise WorkflowError(f"Unsupported result for {control_id}: {result}")
        _require_string(control.get("evidence"), f"controls[{index}].evidence")
        by_id[control_id] = control

    absent = [control_id for control_id in REQUIRED_CONTROLS if control_id not in by_id]
    if absent:
        raise WorkflowError(f"Technical review omits required controls: {', '.join(absent)}")

    non_compliant = [
        control_id
        for control_id in REQUIRED_CONTROLS
        if by_id[control_id]["result"] == "non_compliant"
    ]
    missing = [
        control_id
        for control_id in REQUIRED_CONTROLS
        if by_id[control_id]["result"] == "missing"
    ]
    if non_compliant:
        decision = "rejected"
    elif missing:
        decision = "changes_required"
    else:
        decision = "approved"

    return {
        "decision": decision,
        "verified_controls": [
            control_id
            for control_id in REQUIRED_CONTROLS
            if by_id[control_id]["result"] == "verified"
        ],
        "missing_controls": missing,
        "non_compliant_controls": non_compliant,
        "scope": "technical_document_review",
        "professional_signoff_required": True,
    }


def create_remediation_plan(
    decision: dict[str, Any],
    case_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Split missing controls into ordered A&F-owned operational workstreams."""

    if not CASE_ID_RE.fullmatch(case_id):
        raise WorkflowError("case_id must be a non-sensitive stable identifier")
    timestamp = _require_timestamp(created_at, "created_at")
    if decision.get("decision") != "changes_required":
        raise WorkflowError("Remediation work requires decision=changes_required")

    missing = _require_list(decision.get("missing_controls"), "missing_controls")
    if not missing:
        raise WorkflowError("changes_required must include missing controls")
    unknown = [control_id for control_id in missing if control_id not in CONTROL_DELIVERABLES]
    if unknown:
        raise WorkflowError(f"Unknown missing controls: {', '.join(unknown)}")
    if len(set(missing)) != len(missing):
        raise WorkflowError("missing_controls contains duplicates")

    included_streams = {
        stream["id"]
        for stream in WORKSTREAMS
        if any(control_id in missing for control_id in stream["control_ids"])
    }
    work_items: list[dict[str, Any]] = []
    for stream in WORKSTREAMS:
        stream_id = stream["id"]
        if stream_id not in included_streams:
            continue
        control_ids = [
            control_id
            for control_id in stream["control_ids"]
            if control_id in missing
        ]
        dependencies = [
            f"{case_id}.{dependency}.1"
            for dependency in stream["depends_on"]
            if dependency in included_streams
        ]
        work_item = {
            "work_id": f"{case_id}.{stream_id}.1",
            "type": stream["type"],
            "status": "blocked" if dependencies else "open",
            "assigned_to_role": AF_TECHNICAL_OWNER_ROLE,
            "depends_on": dependencies,
            "deliverables": [
                {
                    "control_id": control_id,
                    "description": CONTROL_DELIVERABLES[control_id],
                    "owner_role": AF_TECHNICAL_OWNER_ROLE,
                    "acceptance": "content_verified",
                    "status": "requested",
                }
                for control_id in control_ids
            ],
            "completion_rule": "all_deliverables_content_verified",
        }
        if "external_counterparty_role" in stream:
            work_item["external_counterparty_role"] = stream[
                "external_counterparty_role"
            ]
            work_item["external_counterparty_manager_role"] = (
                AF_TECHNICAL_OWNER_ROLE
            )
        work_items.append(work_item)

    if "grid-operator-coordination" in included_streams:
        handoff_dependencies = [
            f"{case_id}.{dependency}.1"
            for dependency in SB_DOCUMENT_HANDOFF["depends_on"]
        ]
        work_items.append(
            {
                "work_id": f"{case_id}.{SB_DOCUMENT_HANDOFF['id']}.1",
                "type": SB_DOCUMENT_HANDOFF["type"],
                "status": "blocked",
                "assigned_to_role": AF_TECHNICAL_OWNER_ROLE,
                "depends_on": handoff_dependencies,
                "trigger": SB_DOCUMENT_HANDOFF["trigger"],
                "delivery_recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                "document_repository": "monday",
                "deliverables": [
                    {
                        "document_type": document_type,
                        "description": CONTROL_DELIVERABLES[document_type],
                        "owner_role": AF_TECHNICAL_OWNER_ROLE,
                        "recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                        "acceptance": "content_verified_and_uploaded_to_monday",
                        "status": "waiting_for_monday_upload",
                    }
                    for document_type in SB_HANDOFF_DOCUMENTS
                ],
                "notification_source": "monday",
                "notification_event": "document_uploaded",
                "notification_refs": [],
                "grid_operator_practices_accepted": False,
                "email_delivery": {
                    "recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                    "channel": "email",
                    "trigger": (
                        "all_required_documents_uploaded_and_grid_operator_"
                        "practices_accepted"
                    ),
                    "send_mode": "human_approval_required",
                    "status": "waiting_for_documents",
                    "attachment_document_types": [],
                },
                "completion_rule": (
                    "accepted_documentation_emailed_to_sb"
                ),
            }
        )

    return {
        "plan_id": f"{case_id}.technical-remediation.1",
        "case_id": case_id,
        "type": "af_technical_remediation_plan",
        "status": "open",
        "assigned_to_role": AF_TECHNICAL_OWNER_ROLE,
        "assignment_source_role": SB_ASSIGNMENT_OWNER_ROLE,
        "available_project_data_provider_role": SB_ASSIGNMENT_OWNER_ROLE,
        "technical_document_owner_role": AF_TECHNICAL_OWNER_ROLE,
        "grid_operator_manager_role": AF_TECHNICAL_OWNER_ROLE,
        "created_at": timestamp,
        "source_decision": "changes_required",
        "work_items": work_items,
        "sb_request_policy": "only_explicit_missing_source_project_data",
        "technical_controls_do_not_create_sb_requests": True,
        "completion_rule": "all_work_items_completed",
        "professional_signoff_required": True,
    }


def create_remediation_work(
    decision: dict[str, Any],
    case_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Backward-compatible alias for the work-plan generator."""

    return create_remediation_plan(decision, case_id, created_at)


def record_monday_document_notification(
    handoff: dict[str, Any],
    notification: dict[str, Any],
) -> dict[str, Any]:
    """Record a sanitized Monday upload event and gate the SB email draft."""

    if handoff.get("type") != SB_DOCUMENT_HANDOFF["type"]:
        raise WorkflowError("handoff must be an SB document handoff work item")
    if notification.get("sanitized") is not True:
        raise WorkflowError("Monday notification must declare sanitized=true")
    if notification.get("source") != "monday":
        raise WorkflowError("notification source must be monday")
    if notification.get("event_type") != "document_uploaded":
        raise WorkflowError("notification event_type must be document_uploaded")

    document_type = _require_string(
        notification.get("document_type"), "notification.document_type"
    )
    if document_type not in SB_HANDOFF_DOCUMENTS:
        raise WorkflowError(f"Unsupported SB handoff document: {document_type}")
    if notification.get("content_verified") is not True:
        raise WorkflowError("Monday upload must be content_verified before handoff")
    practices_accepted = notification.get("grid_operator_practices_accepted")
    if not isinstance(practices_accepted, bool):
        raise WorkflowError(
            "notification.grid_operator_practices_accepted must be a boolean"
        )
    event_ref = _require_string(
        notification.get("event_ref_sha256"), "notification.event_ref_sha256"
    )
    if not SHA256_RE.fullmatch(event_ref):
        raise WorkflowError(
            "notification.event_ref_sha256 must be a lowercase SHA-256 digest"
        )

    updated = copy.deepcopy(handoff)
    notification_refs = _require_list(
        updated.get("notification_refs"), "handoff.notification_refs"
    )
    if event_ref in notification_refs:
        return updated

    deliverables = _require_list(updated.get("deliverables"), "handoff.deliverables")
    matching = [
        item
        for item in deliverables
        if isinstance(item, dict) and item.get("document_type") == document_type
    ]
    if len(matching) != 1:
        raise WorkflowError(f"handoff must contain one deliverable for {document_type}")
    matching[0]["status"] = "uploaded_to_monday"
    notification_refs.append(event_ref)
    updated["grid_operator_practices_accepted"] = bool(
        updated.get("grid_operator_practices_accepted") or practices_accepted
    )

    documents_ready = all(
        isinstance(item, dict) and item.get("status") == "uploaded_to_monday"
        for item in deliverables
    )
    email_delivery = _require_mapping(
        updated.get("email_delivery"), "handoff.email_delivery"
    )
    if documents_ready and updated["grid_operator_practices_accepted"]:
        updated["status"] = "ready_for_email_approval"
        email_delivery["status"] = "draft_ready"
        email_delivery["attachment_document_types"] = list(SB_HANDOFF_DOCUMENTS)
    else:
        email_delivery["status"] = "waiting_for_documents"

    return updated
