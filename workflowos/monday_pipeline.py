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
from .core import WorkflowError, _require_mapping, _require_string
from .monday_assets import MondayReadOnlyCollector
from .state_store import (
    load_automation_state,
    lock_automation_state,
    save_automation_state,
)
from .technical_review import record_sb_email_delivery


UNRESOLVED_DELIVERY_STATES = {"sending", "delivery_in_doubt"}


@dataclass(frozen=True)
class MondayFileChange:
    """Non-secret identifiers and workflow facts for one Monday file change."""

    item_id: str
    case_id: str
    column_id: str
    asset_id: str | None = None
    grid_operator_practices_accepted: bool = False
    installation_completed: bool = False


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
            }
            save_automation_state(state_path, updated)

            try:
                confirmation = self._email_sender(copy.deepcopy(request))
                if not isinstance(confirmation, dict):
                    raise WorkflowError("email_sender must return a confirmation object")
                handoffs = _require_mapping(updated.get("handoffs"), "state.handoffs")
                handoff = _require_mapping(
                    handoffs.get(handoff_key), f"state.handoffs.{handoff_key}"
                )
                handoffs[handoff_key] = record_sb_email_delivery(
                    handoff, confirmation
                )
            except Exception as exc:
                outbox[idempotency_key]["status"] = "delivery_in_doubt"
                save_automation_state(state_path, updated)
                raise WorkflowError(
                    "Email delivery outcome is uncertain; manual reconciliation required"
                ) from exc

            outbox[idempotency_key] = {
                "status": "sent",
                "handoff": handoff_key,
                "request_sha256": _canonical_sha256(request),
                "message_ref_sha256": confirmation["message_ref_sha256"],
            }
            completed = {
                "status": "completed",
                "handoff": handoff_key,
                "email_sent": True,
                "idempotency_key": idempotency_key,
            }
            updated["last_result"] = completed
            save_automation_state(state_path, updated)
            return {"state": updated, "result": completed}

    def _validate_runtime_mode(self) -> None:
        mode = _require_string(self._config.get("mode"), "config.mode")
        if mode not in {"test", "live"}:
            raise WorkflowError("config.mode must be test or live")
        if not isinstance(self._config.get("allow_external_email", False), bool):
            raise WorkflowError("config.allow_external_email must be a boolean")

    def _validate_binding(self) -> None:
        binding = self._collector.tenant_binding()
        expected_board = _require_string(
            self._config.get("expected_board_id"), "config.expected_board_id"
        )
        raw_columns = _require_mapping(
            self._config.get("document_columns"), "config.document_columns"
        )
        if binding["expected_board_id"] != expected_board:
            raise WorkflowError("Monday collector and automation board do not match")
        if binding["document_columns"] != dict(raw_columns):
            raise WorkflowError("Monday collector and automation columns do not match")


def _find_unresolved_delivery(state: dict[str, Any]) -> str | None:
    outbox = state.get("delivery_outbox", {})
    if not isinstance(outbox, dict):
        raise WorkflowError("state.delivery_outbox must be an object")
    unresolved = []
    for key, value in outbox.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise WorkflowError("state.delivery_outbox contains an invalid entry")
        status = value.get("status")
        if status not in {"sending", "delivery_in_doubt", "sent"}:
            raise WorkflowError("state.delivery_outbox contains an invalid status")
        if status in UNRESOLVED_DELIVERY_STATES:
            unresolved.append(key)
    if len(unresolved) > 1:
        raise WorkflowError("Multiple unresolved email deliveries require reconciliation")
    return unresolved[0] if unresolved else None


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
