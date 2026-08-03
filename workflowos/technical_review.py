"""Evidence-gated technical review for a document-ready electrical case."""

from __future__ import annotations

from typing import Any

from .core import WorkflowError, _require_list, _require_mapping, _require_string


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
