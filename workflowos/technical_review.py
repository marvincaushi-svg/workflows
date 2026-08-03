"""Evidence-gated technical review for a document-ready electrical case."""

from __future__ import annotations

import copy
import re
import unicodedata
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
    "safety_report_rasi_sina": "Signed RaSi/SiNa safety report",
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

SB_COMPLETION_HANDOFF = {
    "id": "sb-completion-handoff",
    "type": "publish_completion_documentation_to_sb_monday",
    "depends_on": ("commissioning",),
    "trigger": "installation_completed_and_safety_report_signed",
}

SB_HANDOFF_DOCUMENTS = (
    "tag_grid_connection_application",
    "installation_notice_ia",
    "single_line_diagram",
)

SB_COMPLETION_DOCUMENTS = ("safety_report_rasi_sina",)

REQUIRED_DOCUMENT_IDENTITY_FIELDS = (
    "customer_name",
    "installation_address.street",
    "installation_address.house_number",
    "installation_address.postal_code",
    "installation_address.city",
    "project_reference",
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
                "case_context": None,
                "document_identity_check": {
                    "status": "waiting_for_case_context",
                    "required_fields": list(REQUIRED_DOCUMENT_IDENTITY_FIELDS),
                    "checked_fields": [],
                    "missing_fields": [],
                    "mismatched_fields": [],
                },
                "email_delivery": {
                    "recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                    "channel": "email",
                    "trigger": (
                        "all_required_documents_uploaded_and_grid_operator_"
                        "practices_accepted_and_document_identity_verified"
                    ),
                    "send_mode": "automatic_after_document_validation",
                    "status": "waiting_for_case_context",
                    "attachment_document_types": [],
                    "attachment_refs_sha256": [],
                },
                "completion_rule": (
                    "accepted_documentation_emailed_to_sb"
                ),
            }
        )

    if "commissioning" in included_streams:
        completion_dependencies = [
            f"{case_id}.{dependency}.1"
            for dependency in SB_COMPLETION_HANDOFF["depends_on"]
        ]
        work_items.append(
            {
                "work_id": f"{case_id}.{SB_COMPLETION_HANDOFF['id']}.1",
                "type": SB_COMPLETION_HANDOFF["type"],
                "status": "blocked",
                "assigned_to_role": AF_TECHNICAL_OWNER_ROLE,
                "depends_on": completion_dependencies,
                "trigger": SB_COMPLETION_HANDOFF["trigger"],
                "delivery_recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                "document_repository": "monday",
                "deliverables": [
                    {
                        "document_type": "safety_report_rasi_sina",
                        "description": CONTROL_DELIVERABLES["safety_report_rasi_sina"],
                        "owner_role": AF_TECHNICAL_OWNER_ROLE,
                        "recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                        "acceptance": (
                            "content_verified_professionally_signed_and_uploaded_to_monday"
                        ),
                        "status": "waiting_for_monday_upload",
                    }
                ],
                "notification_source": "monday",
                "notification_event": "document_uploaded",
                "notification_refs": [],
                "installation_completed": False,
                "professional_signoff_required": True,
                "case_context": None,
                "document_identity_check": {
                    "status": "waiting_for_case_context",
                    "required_fields": list(REQUIRED_DOCUMENT_IDENTITY_FIELDS),
                    "checked_fields": [],
                    "missing_fields": [],
                    "mismatched_fields": [],
                },
                "email_delivery": {
                    "recipient_role": SB_ASSIGNMENT_OWNER_ROLE,
                    "channel": "email",
                    "trigger": (
                        "installation_completed_and_signed_safety_report_uploaded_"
                        "and_document_identity_verified"
                    ),
                    "send_mode": "automatic_after_document_validation",
                    "status": "waiting_for_case_context",
                    "attachment_document_types": [],
                    "attachment_refs_sha256": [],
                },
                "completion_rule": "signed_safety_report_emailed_to_sb",
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


def configure_sb_document_handoff(
    handoff: dict[str, Any],
    monday_case: dict[str, Any],
) -> dict[str, Any]:
    """Bind an authoritative Monday case and verified SB recipient to a handoff."""

    spec = _get_handoff_spec(handoff)
    if monday_case.get("source") != "monday":
        raise WorkflowError("handoff case source must be monday")

    case_id = _require_string(monday_case.get("case_id"), "monday_case.case_id")
    expected_prefix = f"{case_id}.{spec['id']}."
    if not _require_string(handoff.get("work_id"), "handoff.work_id").startswith(
        expected_prefix
    ):
        raise WorkflowError("Monday case_id does not match handoff work_id")

    identity = _require_mapping(monday_case.get("identity"), "monday_case.identity")
    flattened_identity = _flatten_identity(identity, "monday_case.identity")
    missing = [
        field
        for field in REQUIRED_DOCUMENT_IDENTITY_FIELDS
        if not flattened_identity.get(field)
    ]
    if missing:
        raise WorkflowError(
            "Monday case omits required identity fields: " + ", ".join(missing)
        )

    recipient = _require_mapping(
        monday_case.get("sb_email_recipient"), "monday_case.sb_email_recipient"
    )
    recipient_name = _require_string(
        recipient.get("name"), "monday_case.sb_email_recipient.name"
    )
    recipient_email = _require_string(
        recipient.get("email"), "monday_case.sb_email_recipient.email"
    )
    if not EMAIL_RE.fullmatch(recipient_email):
        raise WorkflowError("SB email recipient must contain a valid email address")
    if recipient.get("organization_role") != SB_ASSIGNMENT_OWNER_ROLE:
        raise WorkflowError("Email recipient organization_role must be sb_energetica")
    if recipient.get("resolution_status") != "verified":
        raise WorkflowError("SB email recipient must be verified before automation")

    updated = copy.deepcopy(handoff)
    updated["case_context"] = {
        "source": "monday",
        "case_id": case_id,
        "identity": copy.deepcopy(identity),
        "sb_email_recipient": {
            "name": recipient_name,
            "email": recipient_email,
            "organization_role": SB_ASSIGNMENT_OWNER_ROLE,
            "resolution_status": "verified",
        },
    }
    identity_check = _require_mapping(
        updated.get("document_identity_check"), "handoff.document_identity_check"
    )
    identity_check["status"] = "waiting_for_documents"
    email_delivery = _require_mapping(
        updated.get("email_delivery"), "handoff.email_delivery"
    )
    email_delivery["recipient_name"] = recipient_name
    email_delivery["recipient_email"] = recipient_email
    email_delivery["status"] = "waiting_for_documents"
    return updated


def record_monday_document_notification(
    handoff: dict[str, Any],
    notification: dict[str, Any],
) -> dict[str, Any]:
    """Record a sanitized Monday upload and gate automatic SB email delivery."""

    spec = _get_handoff_spec(handoff)
    required_documents = spec["documents"]
    if notification.get("sanitized") is not True:
        raise WorkflowError("Monday notification must declare sanitized=true")
    if notification.get("source") != "monday":
        raise WorkflowError("notification source must be monday")
    if notification.get("event_type") != "document_uploaded":
        raise WorkflowError("notification event_type must be document_uploaded")

    document_type = _require_string(
        notification.get("document_type"), "notification.document_type"
    )
    if document_type not in required_documents:
        raise WorkflowError(f"Unsupported SB handoff document: {document_type}")
    if notification.get("content_verified") is not True:
        raise WorkflowError("Monday upload must be content_verified before handoff")
    if notification.get("latest_version") is not True:
        raise WorkflowError("Monday upload must be the latest document version")
    if notification.get("identity_extraction_verified") is not True:
        raise WorkflowError(
            "Monday upload identity extraction must be verified before handoff"
        )
    if spec["type"] == SB_COMPLETION_HANDOFF["type"]:
        if notification.get("professional_signoff_verified") is not True:
            raise WorkflowError(
                "RaSi/SiNa professional signoff must be verified before handoff"
            )
    document_identity = _require_mapping(
        notification.get("document_identity"), "notification.document_identity"
    )
    _flatten_identity(document_identity, "notification.document_identity")
    gate_field = spec["gate_field"]
    gate_value = notification.get(gate_field)
    if not isinstance(gate_value, bool):
        raise WorkflowError(
            f"notification.{gate_field} must be a boolean"
        )
    event_ref = _require_string(
        notification.get("event_ref_sha256"), "notification.event_ref_sha256"
    )
    if not SHA256_RE.fullmatch(event_ref):
        raise WorkflowError(
            "notification.event_ref_sha256 must be a lowercase SHA-256 digest"
        )
    attachment_ref = _require_string(
        notification.get("attachment_ref_sha256"),
        "notification.attachment_ref_sha256",
    )
    if not SHA256_RE.fullmatch(attachment_ref):
        raise WorkflowError(
            "notification.attachment_ref_sha256 must be a lowercase SHA-256 digest"
        )

    updated = copy.deepcopy(handoff)
    notification_refs = _require_list(
        updated.get("notification_refs"), "handoff.notification_refs"
    )
    if event_ref in notification_refs:
        return updated

    if updated.get("case_context") is None:
        raise WorkflowError(
            "Configure the authoritative Monday case before recording documents"
        )

    deliverables = _require_list(updated.get("deliverables"), "handoff.deliverables")
    matching = [
        item
        for item in deliverables
        if isinstance(item, dict) and item.get("document_type") == document_type
    ]
    if len(matching) != 1:
        raise WorkflowError(f"handoff must contain one deliverable for {document_type}")
    matching[0]["status"] = "uploaded_to_monday"
    matching[0]["document_identity"] = copy.deepcopy(document_identity)
    matching[0]["attachment_ref_sha256"] = attachment_ref
    notification_refs.append(event_ref)
    updated[gate_field] = bool(updated.get(gate_field) or gate_value)

    documents_ready = all(
        isinstance(item, dict) and item.get("status") == "uploaded_to_monday"
        for item in deliverables
    )
    email_delivery = _require_mapping(
        updated.get("email_delivery"), "handoff.email_delivery"
    )
    if documents_ready:
        identity_result = _evaluate_handoff_document_identity(updated)
        updated["document_identity_check"] = identity_result
    else:
        identity_result = {
            "status": "waiting_for_documents",
            "required_fields": list(REQUIRED_DOCUMENT_IDENTITY_FIELDS),
            "checked_fields": [],
            "missing_fields": [],
            "mismatched_fields": [],
        }

    if (
        documents_ready
        and updated[gate_field]
        and identity_result["status"] == "verified"
    ):
        updated["status"] = "email_send_requested"
        email_delivery["status"] = "send_requested"
        email_delivery["attachment_document_types"] = list(required_documents)
        email_delivery["attachment_refs_sha256"] = [
            item["attachment_ref_sha256"] for item in deliverables
        ]
    elif documents_ready and identity_result["status"] == "blocked":
        updated["status"] = "blocked_document_identity"
        email_delivery["status"] = "blocked_document_validation"
        email_delivery["attachment_document_types"] = []
        email_delivery["attachment_refs_sha256"] = []
    else:
        email_delivery["status"] = "waiting_for_documents"

    return updated


def record_sb_email_delivery(
    handoff: dict[str, Any],
    confirmation: dict[str, Any],
) -> dict[str, Any]:
    """Record the email adapter's proof that the automatically requested email was sent."""

    _get_handoff_spec(handoff)
    email_delivery = _require_mapping(
        handoff.get("email_delivery"), "handoff.email_delivery"
    )
    if email_delivery.get("status") == "sent":
        return copy.deepcopy(handoff)
    if email_delivery.get("status") != "send_requested":
        raise WorkflowError("SB email can be confirmed only after send_requested")
    if confirmation.get("source") != "email_adapter":
        raise WorkflowError("Email confirmation source must be email_adapter")
    if confirmation.get("delivery_status") != "sent":
        raise WorkflowError("Email confirmation delivery_status must be sent")

    recipient_email = _require_string(
        confirmation.get("recipient_email"), "confirmation.recipient_email"
    )
    if _normalize_identity_value(recipient_email) != _normalize_identity_value(
        _require_string(email_delivery.get("recipient_email"), "recipient_email")
    ):
        raise WorkflowError("Email confirmation recipient does not match SB recipient")
    message_ref = _require_string(
        confirmation.get("message_ref_sha256"), "confirmation.message_ref_sha256"
    )
    if not SHA256_RE.fullmatch(message_ref):
        raise WorkflowError(
            "confirmation.message_ref_sha256 must be a lowercase SHA-256 digest"
        )

    updated = copy.deepcopy(handoff)
    updated["status"] = "completed"
    updated_email = _require_mapping(
        updated.get("email_delivery"), "handoff.email_delivery"
    )
    updated_email["status"] = "sent"
    updated_email["message_ref_sha256"] = message_ref
    return updated


def _evaluate_handoff_document_identity(handoff: dict[str, Any]) -> dict[str, Any]:
    case_context = _require_mapping(handoff.get("case_context"), "case_context")
    case_identity = _require_mapping(case_context.get("identity"), "case_identity")
    sources: dict[str, dict[str, str]] = {
        "monday_case": _flatten_identity(case_identity, "case_identity")
    }

    deliverables = _require_list(handoff.get("deliverables"), "handoff.deliverables")
    for deliverable in deliverables:
        item = _require_mapping(deliverable, "handoff.deliverable")
        document_type = _require_string(
            item.get("document_type"), "handoff.deliverable.document_type"
        )
        document_identity = _require_mapping(
            item.get("document_identity"),
            f"handoff.deliverable.{document_type}.document_identity",
        )
        sources[document_type] = _flatten_identity(
            document_identity, f"document_identity.{document_type}"
        )

    missing_fields: list[str] = []
    for field in REQUIRED_DOCUMENT_IDENTITY_FIELDS:
        for source_name, values in sources.items():
            if not values.get(field):
                missing_fields.append(f"{source_name}:{field}")

    checked_fields: list[str] = []
    mismatched_fields: list[str] = []
    all_fields = sorted({field for values in sources.values() for field in values})
    for field in all_fields:
        observed = [values[field] for values in sources.values() if values.get(field)]
        if len(observed) < 2:
            continue
        checked_fields.append(field)
        if len(set(observed)) != 1:
            mismatched_fields.append(field)

    return {
        "status": "blocked" if missing_fields or mismatched_fields else "verified",
        "required_fields": list(REQUIRED_DOCUMENT_IDENTITY_FIELDS),
        "checked_fields": checked_fields,
        "missing_fields": sorted(missing_fields),
        "mismatched_fields": mismatched_fields,
    }


def _flatten_identity(value: dict[str, Any], label: str) -> dict[str, str]:
    flattened: dict[str, str] = {}

    def visit(current: Any, path: str) -> None:
        if isinstance(current, dict):
            for key, nested in current.items():
                if not isinstance(key, str) or not key.strip():
                    raise WorkflowError(f"{label} contains an invalid field name")
                visit(nested, f"{path}.{key}" if path else key)
            return
        if isinstance(current, (list, tuple, set)) or current is None:
            raise WorkflowError(f"{label}.{path} must be a scalar value")
        normalized = _normalize_identity_value(current)
        if normalized:
            flattened[path] = normalized

    visit(value, "")
    return flattened


def _normalize_identity_value(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    return " ".join(normalized.casefold().strip().split())


def _get_handoff_spec(handoff: dict[str, Any]) -> dict[str, Any]:
    handoff_type = handoff.get("type")
    if handoff_type == SB_DOCUMENT_HANDOFF["type"]:
        return {
            **SB_DOCUMENT_HANDOFF,
            "documents": SB_HANDOFF_DOCUMENTS,
            "gate_field": "grid_operator_practices_accepted",
        }
    if handoff_type == SB_COMPLETION_HANDOFF["type"]:
        return {
            **SB_COMPLETION_HANDOFF,
            "documents": SB_COMPLETION_DOCUMENTS,
            "gate_field": "installation_completed",
        }
    raise WorkflowError("handoff must be a supported SB document handoff work item")
