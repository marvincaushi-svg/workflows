"""Evidence-gated technical review for a document-ready electrical case."""

from __future__ import annotations

from typing import Any

from .core import (
    AF_TECHNICAL_OWNER_ROLE,
    CASE_ID_RE,
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


def create_remediation_work(
    decision: dict[str, Any],
    case_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Assign all technical production and grid coordination to A&F."""

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

    return {
        "work_id": f"{case_id}.technical-remediation.1",
        "case_id": case_id,
        "type": "produce_af_technical_package",
        "status": "open",
        "assigned_to_role": AF_TECHNICAL_OWNER_ROLE,
        "assignment_source_role": SB_ASSIGNMENT_OWNER_ROLE,
        "available_project_data_provider_role": SB_ASSIGNMENT_OWNER_ROLE,
        "grid_operator_manager_role": AF_TECHNICAL_OWNER_ROLE,
        "created_at": timestamp,
        "source_decision": "changes_required",
        "deliverables": [
            {
                "control_id": control_id,
                "description": CONTROL_DELIVERABLES[control_id],
                "owner_role": AF_TECHNICAL_OWNER_ROLE,
                "acceptance": "content_verified",
                "status": "requested",
            }
            for control_id in missing
        ],
        "completion_rule": "all_deliverables_content_verified",
        "professional_signoff_required": True,
    }
