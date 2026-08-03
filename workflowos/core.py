"""Deterministic intake and checklist evaluation for the WorkflowOS MVP."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
ACCEPTED_CLASSIFICATIONS = {"confirmed_from_filename", "content_verified"}
KNOWN_CLASSIFICATIONS = ACCEPTED_CLASSIFICATIONS | {
    "signature_not_verified",
    "unverified",
}
SB_ASSIGNMENT_OWNER_ROLE = "sb_energetica"
AF_TECHNICAL_OWNER_ROLE = "af_elektro"
AF_TECHNICAL_DELIVERABLES = (
    "tag_grid_connection_application",
    "installation_notice_ia",
    "single_line_diagram",
    "system_sizing",
)


class WorkflowError(ValueError):
    """Raised when process or evidence data violates the executable contract."""


def load_document(path: str | Path) -> dict[str, Any]:
    """Load a JSON document.

    ``process.schema.yaml`` intentionally uses JSON syntax, which is valid YAML 1.2,
    so the MVP has no runtime dependency on a YAML parser.
    """

    document_path = Path(path)
    try:
        with document_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise WorkflowError(f"Document not found: {document_path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(
            f"Invalid JSON-compatible YAML in {document_path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(value, dict):
        raise WorkflowError(f"Document root must be an object: {document_path}")
    return value


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorkflowError(f"{field} must be a list")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{field} must be a non-empty string")
    return value.strip()


def _require_timestamp(value: Any, field: str) -> str:
    timestamp = _require_string(value, field)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise WorkflowError(f"{field} must include a timezone")
    return timestamp


def validate_process(document: dict[str, Any]) -> dict[str, Any]:
    _require_string(document.get("schema_version"), "schema_version")
    process = _require_mapping(document.get("process"), "process")
    _require_string(process.get("id"), "process.id")
    _require_string(process.get("readiness_scope"), "process.readiness_scope")

    goal = _require_mapping(process.get("goal"), "process.goal")
    _require_string(goal.get("id"), "process.goal.id")
    _require_string(goal.get("description"), "process.goal.description")

    responsibilities = _require_mapping(
        process.get("responsibilities"), "process.responsibilities"
    )
    expected_roles = {
        "assignment_owner": SB_ASSIGNMENT_OWNER_ROLE,
        "available_project_data_provider": SB_ASSIGNMENT_OWNER_ROLE,
        "technical_document_owner": AF_TECHNICAL_OWNER_ROLE,
        "grid_operator_manager": AF_TECHNICAL_OWNER_ROLE,
    }
    for field, expected_role in expected_roles.items():
        observed_role = _require_string(
            responsibilities.get(field), f"process.responsibilities.{field}"
        )
        if observed_role != expected_role:
            raise WorkflowError(
                f"process.responsibilities.{field} must be {expected_role}"
            )
    deliverables = [
        _require_string(value, f"process.responsibilities.af_elektro_deliverables[{index}]")
        for index, value in enumerate(
            _require_list(
                responsibilities.get("af_elektro_deliverables"),
                "process.responsibilities.af_elektro_deliverables",
            )
        )
    ]
    if tuple(deliverables) != AF_TECHNICAL_DELIVERABLES:
        raise WorkflowError(
            "process.responsibilities.af_elektro_deliverables must contain TAG, IA, "
            "single-line diagram and system sizing"
        )

    intake = _require_mapping(process.get("intake"), "process.intake")
    required_channel = _require_string(
        intake.get("required_channel"), "process.intake.required_channel"
    )
    if required_channel != "email":
        raise WorkflowError("The MVP supports email-first intake only")
    project_data_policy = _require_string(
        intake.get("project_data_policy"), "process.intake.project_data_policy"
    )
    if project_data_policy != "accept_available_without_making_it_mandatory":
        raise WorkflowError(
            "SB project data must remain optional input for A&F technical production"
        )

    checklist = _require_mapping(process.get("checklist"), "process.checklist")
    required_artifacts = _require_list(
        checklist.get("required_artifacts"), "process.checklist.required_artifacts"
    )
    required_facts = _require_list(
        checklist.get("required_facts", []), "process.checklist.required_facts"
    )

    artifact_ids: set[str] = set()
    artifact_types: set[str] = set()
    for index, item_value in enumerate(required_artifacts):
        item = _require_mapping(
            item_value, f"process.checklist.required_artifacts[{index}]"
        )
        item_id = _require_string(
            item.get("id"), f"process.checklist.required_artifacts[{index}].id"
        )
        artifact_type = _require_string(
            item.get("artifact_type"),
            f"process.checklist.required_artifacts[{index}].artifact_type",
        )
        if item_id in artifact_ids:
            raise WorkflowError(f"Duplicate checklist id: {item_id}")
        if artifact_type in artifact_types:
            raise WorkflowError(f"Duplicate required artifact type: {artifact_type}")
        artifact_ids.add(item_id)
        artifact_types.add(artifact_type)
    if artifact_types != {"assignment_email"}:
        raise WorkflowError(
            "Only the SB assignment email may be mandatory at assignment intake"
        )

    fact_paths: set[str] = set()
    for index, item_value in enumerate(required_facts):
        item = _require_mapping(item_value, f"process.checklist.required_facts[{index}]")
        path = _require_string(
            item.get("path"), f"process.checklist.required_facts[{index}].path"
        )
        if path in fact_paths:
            raise WorkflowError(f"Duplicate required fact path: {path}")
        fact_paths.add(path)
    if required_facts:
        raise WorkflowError(
            "Project facts supplied by SB must remain optional at assignment intake"
        )

    states = _require_mapping(process.get("states"), "process.states")
    for state_name in ("initial", "ready", "blocked"):
        _require_string(states.get(state_name), f"process.states.{state_name}")

    return process


def normalize_email_evidence(raw_email: dict[str, Any]) -> dict[str, Any]:
    """Normalize observed email evidence without linking or enriching it.

    This is the Researcher boundary: only a strict allow-list is copied. Names,
    subject, body and attachment filenames are deliberately not propagated.
    """

    if raw_email.get("source_type") != "email":
        raise WorkflowError("source_type must be email")
    if raw_email.get("sanitized") is not True:
        raise WorkflowError("Public MVP input must declare sanitized=true")
    _require_string(raw_email.get("schema_version"), "schema_version")

    provider = _require_string(raw_email.get("provider"), "provider")
    message_ref = _require_string(
        raw_email.get("message_ref_sha256"), "message_ref_sha256"
    )
    if not SHA256_RE.fullmatch(message_ref):
        raise WorkflowError("message_ref_sha256 must be a lowercase SHA-256 digest")

    source_party_ref = _require_string(
        raw_email.get("source_party_ref"), "source_party_ref"
    )

    received_at = _require_timestamp(raw_email.get("received_at"), "received_at")
    observations_value = _require_list(raw_email.get("observations", []), "observations")
    attachments_value = _require_list(raw_email.get("attachments", []), "attachments")
    coverage_value = _require_list(raw_email.get("source_coverage", []), "source_coverage")

    observations: list[dict[str, Any]] = []
    observation_paths: set[str] = set()
    for index, observation_value in enumerate(observations_value):
        observation = _require_mapping(observation_value, f"observations[{index}]")
        path = _require_string(observation.get("path"), f"observations[{index}].path")
        if path in observation_paths:
            raise WorkflowError(f"Duplicate observation path: {path}")
        if "value" not in observation or observation["value"] is None:
            raise WorkflowError(f"observations[{index}].value must be observed")
        source_locator = _require_string(
            observation.get("source_locator"),
            f"observations[{index}].source_locator",
        )
        observations.append(
            {
                "path": path,
                "value": copy.deepcopy(observation["value"]),
                "source_locator": source_locator,
            }
        )
        observation_paths.add(path)

    attachments: list[dict[str, Any]] = []
    source_indexes: set[int] = set()
    for index, attachment_value in enumerate(attachments_value):
        attachment = _require_mapping(attachment_value, f"attachments[{index}]")
        source_index = attachment.get("source_index")
        if not isinstance(source_index, int) or isinstance(source_index, bool) or source_index < 1:
            raise WorkflowError(f"attachments[{index}].source_index must be a positive integer")
        if source_index in source_indexes:
            raise WorkflowError(f"Duplicate attachment source_index: {source_index}")
        classification = _require_string(
            attachment.get("classification"),
            f"attachments[{index}].classification",
        )
        if classification not in KNOWN_CLASSIFICATIONS:
            raise WorkflowError(
                f"attachments[{index}].classification is not supported: {classification}"
            )
        artifact_types_value = attachment.get("artifact_types")
        if artifact_types_value is None:
            artifact_types = [
                _require_string(
                    attachment.get("artifact_type"),
                    f"attachments[{index}].artifact_type",
                )
            ]
        else:
            artifact_types = [
                _require_string(value, f"attachments[{index}].artifact_types[{type_index}]")
                for type_index, value in enumerate(
                    _require_list(
                        artifact_types_value, f"attachments[{index}].artifact_types"
                    )
                )
            ]
            if not artifact_types:
                raise WorkflowError(
                    f"attachments[{index}].artifact_types must not be empty"
                )
            if len(set(artifact_types)) != len(artifact_types):
                raise WorkflowError(
                    f"attachments[{index}].artifact_types contains duplicates"
                )
        attachments.append(
            {
                "source_index": source_index,
                "mime_type": _require_string(
                    attachment.get("mime_type"), f"attachments[{index}].mime_type"
                ),
                "artifact_types": artifact_types,
                "classification": classification,
            }
        )
        source_indexes.add(source_index)

    source_coverage: list[dict[str, str]] = []
    coverage_providers: set[str] = set()
    for index, coverage_item_value in enumerate(coverage_value):
        coverage_item = _require_mapping(
            coverage_item_value, f"source_coverage[{index}]"
        )
        coverage_provider = _require_string(
            coverage_item.get("provider"), f"source_coverage[{index}].provider"
        )
        if coverage_provider in coverage_providers:
            raise WorkflowError(f"Duplicate source coverage provider: {coverage_provider}")
        normalized_coverage = {
            "provider": coverage_provider,
            "status": _require_string(
                coverage_item.get("status"), f"source_coverage[{index}].status"
            ),
        }
        if coverage_item.get("reason_code") is not None:
            normalized_coverage["reason_code"] = _require_string(
                coverage_item.get("reason_code"),
                f"source_coverage[{index}].reason_code",
            )
        source_coverage.append(normalized_coverage)
        coverage_providers.add(coverage_provider)

    return {
        "source_type": "email",
        "provider": provider,
        "message_ref_sha256": message_ref,
        "received_at": received_at,
        "source_party_ref": source_party_ref,
        "sanitized": True,
        "observations": observations,
        "attachments": attachments,
        "source_coverage": source_coverage,
    }


def build_case_from_email(
    process_document: dict[str, Any], raw_email: dict[str, Any], case_id: str
) -> dict[str, Any]:
    """Create a Case by linking normalized email evidence to typed artifacts."""

    process = validate_process(process_document)
    normalized = normalize_email_evidence(raw_email)
    if not CASE_ID_RE.fullmatch(case_id):
        raise WorkflowError(
            "case_id must be 3-80 lowercase letters, digits, dots, underscores or hyphens"
        )

    email_artifact_id = f"{case_id}-email"
    artifacts: list[dict[str, Any]] = [
        {
            "artifact_id": email_artifact_id,
            "artifact_type": "assignment_email",
            "status": "available",
            "source": {
                "provider": normalized["provider"],
                "message_ref_sha256": normalized["message_ref_sha256"],
            },
        }
    ]
    for attachment in normalized["attachments"]:
        for type_index, artifact_type in enumerate(attachment["artifact_types"], start=1):
            artifacts.append(
                {
                    "artifact_id": (
                        f"{case_id}-attachment-{attachment['source_index']:03d}"
                        f"-claim-{type_index:02d}"
                    ),
                    "artifact_type": artifact_type,
                    "status": "available",
                    "classification": attachment["classification"],
                    "source": {
                        "artifact_id": email_artifact_id,
                        "attachment_index": attachment["source_index"],
                        "mime_type": attachment["mime_type"],
                    },
                }
            )

    facts = [
        {
            "path": observation["path"],
            "value": copy.deepcopy(observation["value"]),
            "source": {
                "artifact_id": email_artifact_id,
                "locator": observation["source_locator"],
            },
        }
        for observation in normalized["observations"]
    ]

    return {
        "case_id": case_id,
        "process_id": process["id"],
        "goal_id": process["goal"]["id"],
        "created_at": normalized["received_at"],
        "source_channel": "email",
        "source_party_ref": normalized["source_party_ref"],
        "assignment_owner_role": process["responsibilities"]["assignment_owner"],
        "available_project_data_provider_role": process["responsibilities"][
            "available_project_data_provider"
        ],
        "technical_document_owner_role": process["responsibilities"][
            "technical_document_owner"
        ],
        "grid_operator_manager_role": process["responsibilities"][
            "grid_operator_manager"
        ],
        "sanitized": True,
        "status": process["states"]["initial"],
        "artifacts": artifacts,
        "facts": facts,
        "source_coverage": copy.deepcopy(normalized["source_coverage"]),
    }


def evaluate_case(
    process_document: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate the Blueprint checklist and return a deterministic decision."""

    process = validate_process(process_document)
    if case.get("process_id") != process["id"]:
        raise WorkflowError("Case process_id does not match the process definition")

    artifacts = _require_list(case.get("artifacts"), "case.artifacts")
    facts = _require_list(case.get("facts"), "case.facts")
    available_types = {
        artifact.get("artifact_type")
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("status") == "available"
        and (
            artifact.get("artifact_type") == "assignment_email"
            or artifact.get("classification") in ACCEPTED_CLASSIFICATIONS
        )
    }
    observed_fact_paths = {
        fact.get("path") for fact in facts if isinstance(fact, dict) and "value" in fact
    }

    missing_artifacts = [
        item["artifact_type"]
        for item in process["checklist"]["required_artifacts"]
        if item["artifact_type"] not in available_types
    ]
    missing_facts = [
        item["path"]
        for item in process["checklist"]["required_facts"]
        if item["path"] not in observed_fact_paths
    ]

    email_first_passed = (
        case.get("source_channel") == process["intake"]["required_channel"]
    )
    decisions = [
        {
            "rule_id": "R-EMAIL-FIRST",
            "outcome": "pass" if email_first_passed else "fail",
            "details": {
                "required_channel": process["intake"]["required_channel"],
                "observed_channel": case.get("source_channel"),
            },
        },
        {
            "rule_id": "R-ARTIFACT-CHECKLIST",
            "outcome": "pass" if not missing_artifacts else "fail",
            "details": {"missing_artifacts": missing_artifacts},
        },
        {
            "rule_id": "R-REQUIRED-FACTS",
            "outcome": "pass" if not missing_facts else "fail",
            "details": {"missing_facts": missing_facts},
        },
    ]
    ready = all(decision["outcome"] == "pass" for decision in decisions)

    return {
        "case_id": case.get("case_id"),
        "process_id": process["id"],
        "goal_id": process["goal"]["id"],
        "readiness_scope": process["readiness_scope"],
        "responsibilities": copy.deepcopy(process["responsibilities"]),
        "status": process["states"]["ready"] if ready else process["states"]["blocked"],
        "missing_artifacts": missing_artifacts,
        "missing_facts": missing_facts,
        "decisions": decisions,
    }
