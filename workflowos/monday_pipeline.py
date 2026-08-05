"""Durable Monday-to-email orchestration with a fail-closed delivery outbox."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .automation import handle_monday_file_event
from .core import (
    SHA256_RE,
    WorkflowError,
    _require_mapping,
    _require_string,
    _require_timestamp,
)
from .monday_assets import MondayReadOnlyCollector
from .state_store import (
    load_automation_state,
    lock_automation_state,
    save_automation_state,
)
from .technical_review import record_sb_email_delivery


UNRESOLVED_DELIVERY_STATES = {
    "sending",
    "delivery_in_doubt",
    "retry_authorized",
}
RECONCILIATION_OUTCOMES = {"confirmed_sent", "confirmed_not_sent"}


@dataclass(frozen=True)
class MondayFileChange:
    """Non-secret identifiers and workflow facts for one Monday file change."""

    tenant_id: str
    item_id: str
    case_id: str
    column_id: str
    asset_id: str | None = None
    grid_operator_practices_accepted: bool = False
    installation_completed: bool = False


@dataclass(frozen=True)
class DeliveryReconciliation:
    """Auditable evidence used to resolve one uncertain SMTP outcome."""

    idempotency_key: str
    outcome: str
    checked_at: str
    checked_by_ref: str
    evidence_ref_sha256: str
    message_ref_sha256: str | None = None


class DurableMondayEmailPipeline:
    """Join the read-only collector, verifier, state store and email adapter.

    State is locked for the complete transition.  Before a live SMTP call the
    pipeline persists a ``sending`` outbox record.  If the process or adapter
    fails after that point, the record becomes (or remains) unresolved and a
    later invocation refuses to retry automatically.  This deliberately trades
    manual reconciliation for protection against duplicate client emails.
    """

    def __init__(
        self,
        collector: MondayReadOnlyCollector,
        automation_config: Mapping[str, Any],
        *,
        email_sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._collector = collector
        self._config = copy.deepcopy(dict(automation_config))
        self._email_sender = email_sender
        self._validate_runtime_mode()
        self._validate_binding()

    def process(
        self,
        state_path: str | Path,
        change: MondayFileChange,
        verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Process and persist one exact Monday asset without unsafe retries."""

        with lock_automation_state(state_path):
            state = load_automation_state(state_path)
            binding = self._collector.tenant_binding()
            _validate_change_binding(
                state,
                change,
                expected_tenant_id=binding["tenant_id"],
                expected_board_id=binding["expected_board_id"],
            )
            unresolved = _find_unresolved_delivery(state)
            if unresolved is not None:
                return {
                    "state": state,
                    "result": {
                        "status": "blocked_delivery_reconciliation",
                        "email_sent": False,
                        "idempotency_key": unresolved,
                    },
                }

            event = self._collector.collect_file_event(
                item_id=change.item_id,
                case_id=change.case_id,
                column_id=change.column_id,
                asset_id=change.asset_id,
            )
            event["grid_operator_practices_accepted"] = (
                change.grid_operator_practices_accepted
            )
            event["installation_completed"] = change.installation_completed

            guarded_config = copy.deepcopy(self._config)
            guarded_config.update({"mode": "test", "allow_external_email": False})
            prepared = handle_monday_file_event(
                state,
                event,
                dict(verification),
                guarded_config,
            )
            updated = prepared["state"]
            result = prepared["result"]

            if self._config.get("mode") != "live" or result.get("status") != (
                "ready_test_no_email_sent"
            ):
                save_automation_state(state_path, updated)
                return prepared

            if self._config.get("allow_external_email") is not True:
                raise WorkflowError(
                    "Live email delivery requires allow_external_email=true"
                )
            if self._email_sender is None:
                raise WorkflowError(
                    "Live email delivery requires an email_sender adapter"
                )

            request = _require_mapping(result.get("email_request"), "email_request")
            idempotency_key = _require_string(
                request.get("idempotency_key"), "email_request.idempotency_key"
            )
            handoff_key = _require_string(result.get("handoff"), "result.handoff")
            outbox = _require_mapping(
                updated.setdefault("delivery_outbox", {}), "state.delivery_outbox"
            )
            outbox[idempotency_key] = {
                "status": "sending",
                "handoff": handoff_key,
                "request_sha256": _canonical_sha256(request),
                "email_request": copy.deepcopy(request),
            }
            save_automation_state(state_path, updated)
            return self._deliver_outbox_entry(
                state_path,
                updated,
                idempotency_key,
                request,
            )

    def reconcile_delivery(
        self,
        state_path: str | Path,
        evidence: DeliveryReconciliation,
    ) -> dict[str, Any]:
        """Resolve an uncertain delivery without sending or querying Monday."""

        return reconcile_delivery_state(
            state_path,
            evidence,
            expected_tenant_id=self._collector.tenant_binding()["tenant_id"],
        )

    def retry_authorized_delivery(
        self,
        state_path: str | Path,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Perform one explicit retry after evidence confirmed no prior delivery."""

        with lock_automation_state(state_path):
            state = load_automation_state(state_path)
            _validate_state_tenant_binding(
                state, self._collector.tenant_binding()["tenant_id"]
            )
            outbox = _require_mapping(
                state.get("delivery_outbox"), "state.delivery_outbox"
            )
            entry = _require_mapping(
                outbox.get(idempotency_key), "state.delivery_outbox.retry_entry"
            )
            if entry.get("status") != "retry_authorized":
                raise WorkflowError("Email delivery retry has not been authorized")
            request = _validated_persisted_request(entry, idempotency_key)
            if self._email_sender is None:
                raise WorkflowError("Email delivery retry requires an email_sender adapter")

            entry["status"] = "sending"
            save_automation_state(state_path, state)
            return self._deliver_outbox_entry(
                state_path,
                state,
                idempotency_key,
                request,
            )

    def _deliver_outbox_entry(
        self,
        state_path: str | Path,
        state: dict[str, Any],
        idempotency_key: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        outbox = _require_mapping(state.get("delivery_outbox"), "state.delivery_outbox")
        entry = _require_mapping(outbox.get(idempotency_key), "outbox.entry")
        handoff_key = _require_string(entry.get("handoff"), "outbox.handoff")
        try:
            confirmation = self._email_sender(copy.deepcopy(request))
            if not isinstance(confirmation, dict):
                raise WorkflowError("email_sender must return a confirmation object")
            handoffs = _require_mapping(state.get("handoffs"), "state.handoffs")
            handoff = _require_mapping(
                handoffs.get(handoff_key), f"state.handoffs.{handoff_key}"
            )
            handoffs[handoff_key] = record_sb_email_delivery(handoff, confirmation)
        except Exception as exc:
            entry["status"] = "delivery_in_doubt"
            save_automation_state(state_path, state)
            raise WorkflowError(
                "Email delivery outcome is uncertain; manual reconciliation required"
            ) from exc

        _mark_entry_sent(entry, confirmation)
        completed = {
            "status": "completed",
            "handoff": handoff_key,
            "email_sent": True,
            "idempotency_key": idempotency_key,
        }
        state["last_result"] = completed
        save_automation_state(state_path, state)
        return {"state": state, "result": completed}

    def _validate_runtime_mode(self) -> None:
        mode = _require_string(self._config.get("mode"), "config.mode")
        if mode not in {"test", "live"}:
            raise WorkflowError("config.mode must be test or live")
        if not isinstance(self._config.get("allow_external_email", False), bool):
            raise WorkflowError("config.allow_external_email must be a boolean")

    def _validate_binding(self) -> None:
        binding = self._collector.tenant_binding()
        expected_tenant = _require_string(
            self._config.get("expected_tenant_id"), "config.expected_tenant_id"
        )
        expected_board = _require_string(
            self._config.get("expected_board_id"), "config.expected_board_id"
        )
        raw_columns = _require_mapping(
            self._config.get("document_columns"), "config.document_columns"
        )
        if binding["tenant_id"] != expected_tenant:
            raise WorkflowError("Monday collector and automation tenant do not match")
        if binding["expected_board_id"] != expected_board:
            raise WorkflowError("Monday collector and automation board do not match")
        if binding["document_columns"] != dict(raw_columns):
            raise WorkflowError("Monday collector and automation columns do not match")


def inspect_delivery_outbox(state_path: str | Path) -> dict[str, Any]:
    """Return a sanitized operational view without exposing email contents."""

    with lock_automation_state(state_path):
        state = load_automation_state(state_path)
        outbox = _require_mapping(
            state.get("delivery_outbox", {}), "state.delivery_outbox"
        )
        entries = []
        for idempotency_key, raw_entry in sorted(outbox.items()):
            key = _require_sha256(idempotency_key, "outbox.idempotency_key")
            entry = _require_mapping(raw_entry, f"outbox.{key}")
            status = _require_string(entry.get("status"), f"outbox.{key}.status")
            if status not in UNRESOLVED_DELIVERY_STATES | {"sent"}:
                raise WorkflowError("state.delivery_outbox contains an invalid status")
            snapshot = {
                "idempotency_key": key,
                "status": status,
                "handoff": _require_string(
                    entry.get("handoff"), f"outbox.{key}.handoff"
                ),
                "request_sha256": _require_sha256(
                    entry.get("request_sha256"), f"outbox.{key}.request_sha256"
                ),
                "action_required": _outbox_action(status),
            }
            if status == "sent":
                snapshot["message_ref_sha256"] = _require_sha256(
                    entry.get("message_ref_sha256"),
                    f"outbox.{key}.message_ref_sha256",
                )
            entries.append(snapshot)
        return {
            "status": "ok",
            "entry_count": len(entries),
            "unresolved_count": sum(
                entry["status"] in UNRESOLVED_DELIVERY_STATES
                for entry in entries
            ),
            "entries": entries,
        }


def _validate_state_tenant_binding(
    state: dict[str, Any], expected_tenant_id: str
) -> str:
    state_tenant_id = _require_string(state.get("tenant_id"), "state.tenant_id")
    if state_tenant_id != expected_tenant_id:
        raise WorkflowError("Automation state tenant does not match Monday collector")
    return state_tenant_id


def _validate_change_binding(
    state: dict[str, Any],
    change: MondayFileChange,
    *,
    expected_tenant_id: str,
    expected_board_id: str,
) -> None:
    """Reject a cross-case event before any Monday API or asset request."""

    if state.get("source") != "monday":
        raise WorkflowError("Automation state source must be monday")
    state_tenant_id = _validate_state_tenant_binding(state, expected_tenant_id)
    if change.tenant_id != state_tenant_id:
        raise WorkflowError("Monday change tenant_id does not match persisted state")
    state_board_id = _require_string(state.get("board_id"), "state.board_id")
    if state_board_id != expected_board_id:
        raise WorkflowError("Automation state board does not match Monday collector")
    state_item_id = _require_string(state.get("item_id"), "state.item_id")
    if change.item_id != state_item_id:
        raise WorkflowError("Monday change item_id does not match persisted state")
    state_case_id = _require_string(state.get("case_id"), "state.case_id")
    if change.case_id != state_case_id:
        raise WorkflowError("Monday change case_id does not match persisted state")


def reconcile_delivery_state(
    state_path: str | Path,
    evidence: DeliveryReconciliation,
    *,
    expected_tenant_id: str | None = None,
) -> dict[str, Any]:
    """Resolve uncertain delivery state without email or Monday adapters."""

    with lock_automation_state(state_path):
        state = load_automation_state(state_path)
        if expected_tenant_id is not None:
            _validate_state_tenant_binding(state, expected_tenant_id)
        outbox = _require_mapping(
            state.get("delivery_outbox"), "state.delivery_outbox"
        )
        entry = _require_mapping(
            outbox.get(evidence.idempotency_key),
            "state.delivery_outbox.reconciliation_entry",
        )
        if entry.get("status") not in {"sending", "delivery_in_doubt"}:
            raise WorkflowError("Email delivery is not awaiting reconciliation")
        reconciliation = _validate_reconciliation(evidence)

        if evidence.outcome == "confirmed_not_sent":
            _validated_persisted_request(entry, evidence.idempotency_key)
            entry["status"] = "retry_authorized"
            entry["reconciliation"] = reconciliation
            result = {
                "status": "retry_authorized",
                "email_sent": False,
                "idempotency_key": evidence.idempotency_key,
            }
            state["last_result"] = result
            save_automation_state(state_path, state)
            return {"state": state, "result": result}

        handoff_key = _require_string(entry.get("handoff"), "outbox.handoff")
        handoffs = _require_mapping(state.get("handoffs"), "state.handoffs")
        handoff = _require_mapping(
            handoffs.get(handoff_key), f"state.handoffs.{handoff_key}"
        )
        email_delivery = _require_mapping(
            handoff.get("email_delivery"), "handoff.email_delivery"
        )
        confirmation = {
            "source": "email_adapter",
            "delivery_status": "sent",
            "recipient_email": _require_string(
                email_delivery.get("recipient_email"), "recipient_email"
            ),
            "message_ref_sha256": evidence.message_ref_sha256,
        }
        handoffs[handoff_key] = record_sb_email_delivery(handoff, confirmation)
        _mark_entry_sent(entry, confirmation, reconciliation=reconciliation)
        result = {
            "status": "completed_reconciled",
            "handoff": handoff_key,
            "email_sent": True,
            "idempotency_key": evidence.idempotency_key,
        }
        state["last_result"] = result
        save_automation_state(state_path, state)
        return {"state": state, "result": result}


def _find_unresolved_delivery(state: dict[str, Any]) -> str | None:
    outbox = state.get("delivery_outbox", {})
    if not isinstance(outbox, dict):
        raise WorkflowError("state.delivery_outbox must be an object")
    unresolved = []
    for key, value in outbox.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise WorkflowError("state.delivery_outbox contains an invalid entry")
        status = value.get("status")
        if status not in {
            "sending",
            "delivery_in_doubt",
            "retry_authorized",
            "sent",
        }:
            raise WorkflowError("state.delivery_outbox contains an invalid status")
        if status in UNRESOLVED_DELIVERY_STATES:
            unresolved.append(key)
    if len(unresolved) > 1:
        raise WorkflowError("Multiple unresolved email deliveries require reconciliation")
    return unresolved[0] if unresolved else None


def _outbox_action(status: str) -> str:
    if status in {"sending", "delivery_in_doubt"}:
        return "reconcile_delivery"
    if status == "retry_authorized":
        return "perform_explicit_retry"
    return "none"


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_reconciliation(evidence: DeliveryReconciliation) -> dict[str, str]:
    idempotency_key = _require_sha256(
        evidence.idempotency_key, "reconciliation.idempotency_key"
    )
    outcome = _require_string(evidence.outcome, "reconciliation.outcome")
    if outcome not in RECONCILIATION_OUTCOMES:
        raise WorkflowError("reconciliation.outcome is not supported")
    checked_at = _require_timestamp(evidence.checked_at, "reconciliation.checked_at")
    checked_by_ref = _require_string(
        evidence.checked_by_ref, "reconciliation.checked_by_ref"
    )
    evidence_ref = _require_sha256(
        evidence.evidence_ref_sha256, "reconciliation.evidence_ref_sha256"
    )
    result = {
        "idempotency_key": idempotency_key,
        "outcome": outcome,
        "checked_at": checked_at,
        "checked_by_ref": checked_by_ref,
        "evidence_ref_sha256": evidence_ref,
    }
    if outcome == "confirmed_sent":
        result["message_ref_sha256"] = _require_sha256(
            evidence.message_ref_sha256,
            "reconciliation.message_ref_sha256",
        )
    elif evidence.message_ref_sha256 is not None:
        raise WorkflowError(
            "confirmed_not_sent reconciliation must not include a message reference"
        )
    return result


def _mark_entry_sent(
    entry: dict[str, Any],
    confirmation: dict[str, Any],
    *,
    reconciliation: dict[str, str] | None = None,
) -> None:
    entry["status"] = "sent"
    entry["message_ref_sha256"] = _require_sha256(
        confirmation.get("message_ref_sha256"), "confirmation.message_ref_sha256"
    )
    entry.pop("email_request", None)
    if reconciliation is not None:
        entry["reconciliation"] = reconciliation


def _validated_persisted_request(
    entry: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    request = _require_mapping(entry.get("email_request"), "outbox.email_request")
    expected_request_ref = _require_sha256(
        entry.get("request_sha256"), "outbox.request_sha256"
    )
    if _canonical_sha256(request) != expected_request_ref:
        raise WorkflowError("Persisted email request checksum does not match")
    if request.get("idempotency_key") != idempotency_key:
        raise WorkflowError("Persisted email request idempotency key does not match")
    return request


def _require_sha256(value: Any, field: str) -> str:
    digest = _require_string(value, field)
    if not SHA256_RE.fullmatch(digest):
        raise WorkflowError(f"{field} must be a lowercase SHA-256 digest")
    return digest
