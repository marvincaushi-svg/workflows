"""Hash-chained audit log for WorkflowOS decisions and events."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from .core import WorkflowError, _require_timestamp


GENESIS_HASH = "0" * 64


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_event(event: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _chain_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chained: list[dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    for sequence, raw_event in enumerate(events, start=1):
        event = copy.deepcopy(raw_event)
        event["sequence"] = sequence
        event["previous_hash"] = previous_hash
        event["event_hash"] = _hash_event(event)
        chained.append(event)
        previous_hash = event["event_hash"]
    return chained


def build_audit_log(
    case: dict[str, Any], evaluation: dict[str, Any], evaluated_at: str
) -> list[dict[str, Any]]:
    """Build the four-event audit trail for the end-to-end MVP path."""

    evaluation_time = _require_timestamp(evaluated_at, "evaluated_at")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise WorkflowError("case.case_id is required for the audit log")

    message_ref = None
    for artifact in case.get("artifacts", []):
        if artifact.get("artifact_type") == "assignment_email":
            message_ref = artifact.get("source", {}).get("message_ref_sha256")
            break

    raw_events = [
        {
            "event_type": "case.created",
            "occurred_at": case.get("created_at"),
            "case_id": case_id,
            "actor": "case_builder",
            "data": {
                "process_id": case.get("process_id"),
                "goal_id": case.get("goal_id"),
                "source_channel": case.get("source_channel"),
            },
        },
        {
            "event_type": "evidence.normalized",
            "occurred_at": case.get("created_at"),
            "case_id": case_id,
            "actor": "researcher",
            "data": {
                "message_ref_sha256": message_ref,
                "artifact_count": len(case.get("artifacts", [])),
                "fact_paths": [fact.get("path") for fact in case.get("facts", [])],
                "source_coverage": copy.deepcopy(case.get("source_coverage", [])),
            },
        },
        {
            "event_type": "checklist.evaluated",
            "occurred_at": evaluation_time,
            "case_id": case_id,
            "actor": "validator",
            "data": {
                "decisions": copy.deepcopy(evaluation.get("decisions", [])),
                "missing_artifacts": copy.deepcopy(
                    evaluation.get("missing_artifacts", [])
                ),
                "missing_facts": copy.deepcopy(evaluation.get("missing_facts", [])),
            },
        },
        {
            "event_type": "case.status.changed",
            "occurred_at": evaluation_time,
            "case_id": case_id,
            "actor": "orchestrator",
            "data": {
                "from": case.get("status"),
                "to": evaluation.get("status"),
                "readiness_scope": evaluation.get("readiness_scope"),
            },
        },
    ]
    return _chain_events(raw_events)


def verify_audit_log(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify order, previous hashes and event hashes; raise on tampering."""

    if not events:
        raise WorkflowError("Audit log is empty")
    expected_previous = GENESIS_HASH
    for expected_sequence, event in enumerate(events, start=1):
        if event.get("sequence") != expected_sequence:
            raise WorkflowError(
                f"Audit sequence mismatch at event {expected_sequence}"
            )
        if event.get("previous_hash") != expected_previous:
            raise WorkflowError(
                f"Audit previous_hash mismatch at event {expected_sequence}"
            )
        stored_hash = event.get("event_hash")
        calculated_hash = _hash_event(event)
        if not isinstance(stored_hash, str) or not secrets.compare_digest(
            stored_hash, calculated_hash
        ):
            raise WorkflowError(f"Audit hash mismatch at event {expected_sequence}")
        expected_previous = stored_hash

    return {
        "valid": True,
        "event_count": len(events),
        "head_hash": expected_previous,
    }


def write_audit_log(path: str | Path, events: list[dict[str, Any]]) -> None:
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(_canonical_json(event))
            handle.write("\n")


def read_audit_log(path: str | Path) -> list[dict[str, Any]]:
    audit_path = Path(path)
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise WorkflowError(f"Audit log not found: {audit_path}") from exc

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"Invalid audit JSON on line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise WorkflowError(f"Audit line {line_number} must be an object")
        events.append(event)
    return events
