"""Evidence-gated technical review for a document-ready electrical case."""

from __future__ import annotations

from typing import Any

from .core import (
    CASE_ID_RE,
    WorkflowError,
    _require_list,
    _require_mapping,
    _require_string,
    _require_timestamp,
)


REQUIRED_CONTROLS = (
    "single_line_diagram",
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
    "single_line_diagram": "Single-line AC/DC diagram",
    "cable_sizing": "Cable sizing with sections, lengths and installation methods",
    "ac_dc_protections": "Coordinated AC/DC protection, RCD and SPD schedule",
    "earthing_bonding": "Earthing and equipotential bonding design",
    "short_circuit_data": "Prospective short-circuit values and breaking-capacity check",
    "maximum_dc_voltage": "Maximum DC voltage calculation at project minimum temperature",
    "battery_installation": "Battery location and installation conditions",
    "grid_operator_requirements": "Grid-operator approval and connection requirements",
    "commissioning_measurements": "Final inspection and measurement record",
}


def evaluate_technical_review(document: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable decision without inferring missing electrical data."""

    _require_string(document.get("schema_version"), "schema_version")
    if document.get("sanitized") is not True:
        raise WorkflowError("Public technical review input must declare sanitized=true")
    if document.get("document_intake_status") != "ready":
        raise WorkflowError("Technical review requires document_intake_status=ready")

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


def create_remediation_work(
    decision: dict[str, Any],
    case_id: str,
    recipient_role: str,
    created_at: str,
) -> dict[str, Any]:
    """Turn missing controls into one assignable, evidence-gated work item."""

    if not CASE_ID_RE.fullmatch(case_id):
        raise WorkflowError("case_id must be a non-sensitive stable identifier")
    recipient = _require_string(recipient_role, "recipient_role")
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

    return {
        "work_id": f"{case_id}.technical-remediation.1",
        "case_id": case_id,
        "type": "request_technical_evidence",
        "status": "open",
        "assigned_to_role": recipient,
        "created_at": timestamp,
        "source_decision": "changes_required",
        "deliverables": [
            {
                "control_id": control_id,
                "description": CONTROL_DELIVERABLES[control_id],
                "acceptance": "content_verified",
                "status": "requested",
            }
            for control_id in missing
        ],
        "completion_rule": "all_deliverables_content_verified",
        "professional_signoff_required": True,
    }
