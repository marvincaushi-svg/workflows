"""Declarative control plane for MERAVIQA automations.

The control plane keeps orchestration rules separate from provider adapters. It
validates the automation catalog, builds deterministic execution plans, applies
idempotency, resolves communication policy, and records retry/dead-letter state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


AUTOMATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_TRIGGER_TYPES = {"event", "schedule"}
ALLOWED_ACTION_MODES = {"observe", "internal_write", "draft", "guarded_send"}
ALLOWED_FAILURE_POLICIES = {"retry_then_dead_letter", "manual_review"}
REQUIRED_SEND_GUARDS = {
    "verified_recipient",
    "idempotency_key",
    "provider_live_switch",
}


class AutomationCatalogError(ValueError):
    """Raised when automation configuration violates the control-plane contract."""


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AutomationCatalogError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AutomationCatalogError(f"{field} must be a list")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutomationCatalogError(f"{field} must be a non-empty string")
    return value.strip()


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise AutomationCatalogError(f"{field} must be a boolean")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AutomationCatalogError(f"{field} must be a positive integer")
    return value


def _require_timestamp(value: Any, field: str) -> datetime:
    timestamp = _require_string(value, field)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutomationCatalogError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AutomationCatalogError(f"{field} must include a timezone")
    return parsed


def _validate_timezone(value: Any, field: str) -> str:
    timezone = _require_string(value, field)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise AutomationCatalogError(f"{field} is not a known IANA timezone") from exc
    return timezone


def _validate_layer(layer: Any, field: str) -> dict[str, str]:
    value = _require_mapping(layer, field)
    return {
        "capability": _require_string(value.get("capability"), f"{field}.capability"),
        "provider": _require_string(value.get("provider"), f"{field}.provider"),
        "adapter": _require_string(value.get("adapter"), f"{field}.adapter"),
    }


def _validate_retry_policy(value: Any, field: str) -> dict[str, int]:
    policy = _require_mapping(value, field)
    max_attempts = _require_positive_int(policy.get("max_attempts"), f"{field}.max_attempts")
    base_delay_seconds = _require_positive_int(
        policy.get("base_delay_seconds"), f"{field}.base_delay_seconds"
    )
    max_delay_seconds = _require_positive_int(
        policy.get("max_delay_seconds"), f"{field}.max_delay_seconds"
    )
    if max_delay_seconds < base_delay_seconds:
        raise AutomationCatalogError(
            f"{field}.max_delay_seconds must be >= base_delay_seconds"
        )
    return {
        "max_attempts": max_attempts,
        "base_delay_seconds": base_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
    }


def _validate_tenant(value: Any, field: str) -> dict[str, Any]:
    tenant = _require_mapping(value, field)
    tenant_id = _require_string(tenant.get("id"), f"{field}.id")
    if not AUTOMATION_ID_RE.fullmatch(tenant_id):
        raise AutomationCatalogError(f"Invalid tenant id: {tenant_id}")

    raw_roles = _require_mapping(tenant.get("roles"), f"{field}.roles")
    if not raw_roles:
        raise AutomationCatalogError(f"{field}.roles must not be empty")
    roles: dict[str, str] = {}
    for raw_role_name, raw_role_value in raw_roles.items():
        role_name = _require_string(raw_role_name, "tenant role name")
        if not AUTOMATION_ID_RE.fullmatch(role_name):
            raise AutomationCatalogError(f"Invalid tenant role name: {role_name}")
        roles[role_name] = _require_string(
            raw_role_value, f"{field}.roles.{role_name}"
        )

    return {
        "id": tenant_id,
        "organization_name": _require_string(
            tenant.get("organization_name"), f"{field}.organization_name"
        ),
        "roles": roles,
    }


def validate_automation_catalog(document: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a complete automation catalog.

    External systems may only appear through declared collectors. Every
    automation has exactly one textual objective and one textual output, plus a
    capability/provider/adapter layer that can be replaced independently.
    """

    catalog = copy.deepcopy(_require_mapping(document, "catalog"))
    _require_string(catalog.get("schema_version"), "schema_version")
    tenant = _validate_tenant(catalog.get("tenant"), "tenant")
    timezone = _validate_timezone(catalog.get("timezone"), "timezone")

    policies = _require_mapping(catalog.get("policies"), "policies")
    retry_policy = _validate_retry_policy(policies.get("retry"), "policies.retry")
    email_policy = _require_mapping(
        policies.get("external_email"), "policies.external_email"
    )
    default_send_mode = _require_string(
        email_policy.get("default_mode"), "policies.external_email.default_mode"
    )
    if default_send_mode not in {"draft", "guarded_send"}:
        raise AutomationCatalogError(
            "policies.external_email.default_mode must be draft or guarded_send"
        )
    mandatory_send_guards = {
        _require_string(item, f"policies.external_email.mandatory_send_guards[{index}]")
        for index, item in enumerate(
            _require_list(
                email_policy.get("mandatory_send_guards"),
                "policies.external_email.mandatory_send_guards",
            )
        )
    }
    missing_guards = REQUIRED_SEND_GUARDS - mandatory_send_guards
    if missing_guards:
        raise AutomationCatalogError(
            "policies.external_email.mandatory_send_guards is missing: "
            + ", ".join(sorted(missing_guards))
        )

    communication = _require_mapping(
        policies.get("communication"), "policies.communication"
    )
    default_language = _require_string(
        communication.get("default_language"),
        "policies.communication.default_language",
    )
    recipient_rules = _require_mapping(
        communication.get("recipient_rules", {}),
        "policies.communication.recipient_rules",
    )
    normalized_recipient_rules: dict[str, dict[str, Any]] = {}
    for raw_recipient, raw_rule in recipient_rules.items():
        recipient = _normalize_email_address(raw_recipient, "recipient rule")
        rule = _require_mapping(
            raw_rule, f"policies.communication.recipient_rules.{recipient}"
        )
        normalized_recipient_rules[recipient] = {
            "language": _require_string(
                rule.get("language"),
                f"policies.communication.recipient_rules.{recipient}.language",
            ),
            "swiss_spelling": _require_bool(
                rule.get("swiss_spelling", False),
                f"policies.communication.recipient_rules.{recipient}.swiss_spelling",
            ),
        }

    collectors_value = _require_mapping(catalog.get("collectors"), "collectors")
    collectors: dict[str, dict[str, str]] = {}
    for collector_id, raw_collector in collectors_value.items():
        normalized_id = _require_string(collector_id, "collector id")
        if not AUTOMATION_ID_RE.fullmatch(normalized_id):
            raise AutomationCatalogError(f"Invalid collector id: {normalized_id}")
        collector = _require_mapping(raw_collector, f"collectors.{normalized_id}")
        collectors[normalized_id] = {
            "source": _require_string(
                collector.get("source"), f"collectors.{normalized_id}.source"
            ),
            **_validate_layer(collector.get("layer"), f"collectors.{normalized_id}.layer"),
        }

    raw_automations = _require_list(catalog.get("automations"), "automations")
    automations: list[dict[str, Any]] = []
    automation_ids: set[str] = set()
    for index, raw_automation in enumerate(raw_automations):
        field = f"automations[{index}]"
        automation = _require_mapping(raw_automation, field)
        automation_id = _require_string(automation.get("id"), f"{field}.id")
        if not AUTOMATION_ID_RE.fullmatch(automation_id):
            raise AutomationCatalogError(f"Invalid automation id: {automation_id}")
        if automation_id in automation_ids:
            raise AutomationCatalogError(f"Duplicate automation id: {automation_id}")
        automation_ids.add(automation_id)

        objective = _require_string(automation.get("objective"), f"{field}.objective")
        output = _require_string(automation.get("output"), f"{field}.output")
        if isinstance(automation.get("objective"), list) or isinstance(
            automation.get("output"), list
        ):
            raise AutomationCatalogError(
                f"{field} must define one objective and one output"
            )

        trigger = _require_mapping(automation.get("trigger"), f"{field}.trigger")
        trigger_type = _require_string(trigger.get("type"), f"{field}.trigger.type")
        if trigger_type not in ALLOWED_TRIGGER_TYPES:
            raise AutomationCatalogError(
                f"{field}.trigger.type must be event or schedule"
            )
        normalized_trigger: dict[str, Any] = {"type": trigger_type}
        if trigger_type == "event":
            collector_id = _require_string(
                trigger.get("collector_id"), f"{field}.trigger.collector_id"
            )
            if collector_id not in collectors:
                raise AutomationCatalogError(
                    f"{field}.trigger.collector_id is not a declared collector"
                )
            normalized_trigger.update(
                {
                    "collector_id": collector_id,
                    "event_type": _require_string(
                        trigger.get("event_type"), f"{field}.trigger.event_type"
                    ),
                }
            )
        else:
            local_time = _require_string(
                trigger.get("local_time"), f"{field}.trigger.local_time"
            )
            _parse_local_time(local_time, f"{field}.trigger.local_time")
            raw_weekdays = _require_list(
                trigger.get("weekdays"), f"{field}.trigger.weekdays"
            )
            weekdays: list[int] = []
            for weekday_index, value in enumerate(raw_weekdays):
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    or value > 6
                ):
                    raise AutomationCatalogError(
                        f"{field}.trigger.weekdays[{weekday_index}] must be an integer from 0 to 6"
                    )
                weekdays.append(value)
            if not weekdays:
                raise AutomationCatalogError(
                    f"{field}.trigger.weekdays must not be empty"
                )
            normalized_trigger.update(
                {"local_time": local_time, "weekdays": weekdays}
            )

        action = _require_mapping(automation.get("action"), f"{field}.action")
        mode = _require_string(action.get("mode"), f"{field}.action.mode")
        if mode not in ALLOWED_ACTION_MODES:
            raise AutomationCatalogError(
                f"{field}.action.mode is not supported: {mode}"
            )
        guards = [
            _require_string(item, f"{field}.action.guards[{guard_index}]")
            for guard_index, item in enumerate(
                _require_list(action.get("guards", []), f"{field}.action.guards")
            )
        ]
        if mode == "guarded_send":
            missing = mandatory_send_guards - set(guards)
            if missing:
                raise AutomationCatalogError(
                    f"{field}.action.guards is missing: {', '.join(sorted(missing))}"
                )

        dependencies = [
            _require_string(item, f"{field}.dependencies[{dependency_index}]")
            for dependency_index, item in enumerate(
                _require_list(automation.get("dependencies", []), f"{field}.dependencies")
            )
        ]
        if len(dependencies) != len(set(dependencies)):
            raise AutomationCatalogError(f"{field}.dependencies contains duplicates")
        if automation_id in dependencies:
            raise AutomationCatalogError(f"{field} cannot depend on itself")

        failure_policy = _require_string(
            automation.get("failure_policy", "retry_then_dead_letter"),
            f"{field}.failure_policy",
        )
        if failure_policy not in ALLOWED_FAILURE_POLICIES:
            raise AutomationCatalogError(
                f"{field}.failure_policy is not supported: {failure_policy}"
            )
        automation_retry = (
            _validate_retry_policy(automation["retry"], f"{field}.retry")
            if "retry" in automation
            else retry_policy
        )
        priority = automation.get("priority", 50)
        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
            or not 0 <= priority <= 100
        ):
            raise AutomationCatalogError(
                f"{field}.priority must be an integer from 0 to 100"
            )

        owner_role = _require_string(
            automation.get("owner_role"), f"{field}.owner_role"
        )
        if owner_role not in tenant["roles"]:
            raise AutomationCatalogError(
                f"{field}.owner_role is not declared in tenant.roles: {owner_role}"
            )

        automations.append(
            {
                "id": automation_id,
                "enabled": _require_bool(
                    automation.get("enabled", True), f"{field}.enabled"
                ),
                "owner_role": owner_role,
                "owner": tenant["roles"][owner_role],
                "objective": objective,
                "output": output,
                "layer": _validate_layer(
                    automation.get("layer"), f"{field}.layer"
                ),
                "trigger": normalized_trigger,
                "dependencies": dependencies,
                "priority": priority,
                "failure_policy": failure_policy,
                "retry": automation_retry,
                "action": {"mode": mode, "guards": guards},
            }
        )

    for automation in automations:
        unknown = set(automation["dependencies"]) - automation_ids
        if unknown:
            raise AutomationCatalogError(
                f"Automation {automation['id']} has unknown dependencies: "
                + ", ".join(sorted(unknown))
            )
    _assert_acyclic(automations)

    return {
        "schema_version": catalog["schema_version"],
        "tenant": tenant,
        "timezone": timezone,
        "policies": {
            "retry": retry_policy,
            "external_email": {
                "default_mode": default_send_mode,
                "mandatory_send_guards": sorted(mandatory_send_guards),
            },
            "communication": {
                "default_language": default_language,
                "recipient_rules": normalized_recipient_rules,
            },
        },
        "collectors": collectors,
        "automations": automations,
    }


def _assert_acyclic(automations: list[dict[str, Any]]) -> None:
    graph = {item["id"]: set(item["dependencies"]) for item in automations}
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise AutomationCatalogError("Automation dependencies contain a cycle")
        temporary.add(node)
        for dependency in graph[node]:
            visit(dependency)
        temporary.remove(node)
        permanent.add(node)

    for automation_id in graph:
        visit(automation_id)


def _parse_local_time(value: str, field: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise AutomationCatalogError(f"{field} must use HH:MM")
    hour, minute = (int(part) for part in parts)
    if hour > 23 or minute > 59:
        raise AutomationCatalogError(f"{field} must use a valid 24-hour time")
    return hour, minute


def _normalize_email_address(value: Any, field: str) -> str:
    raw = _require_string(value, field)
    _, address = parseaddr(raw)
    normalized = address.strip().lower()
    if not normalized or "@" not in normalized or normalized.startswith("@"):
        raise AutomationCatalogError(f"{field} must be a valid email address")
    return normalized


def resolve_communication_policy(
    catalog_document: dict[str, Any], recipient_email: str
) -> dict[str, Any]:
    """Resolve the exact-recipient language policy for an outbound message."""

    catalog = validate_automation_catalog(catalog_document)
    recipient = _normalize_email_address(recipient_email, "recipient_email")
    rule = catalog["policies"]["communication"]["recipient_rules"].get(recipient)
    if rule is None:
        return {
            "tenant_id": catalog["tenant"]["id"],
            "organization_name": catalog["tenant"]["organization_name"],
            "recipient_email": recipient,
            "language": catalog["policies"]["communication"]["default_language"],
            "swiss_spelling": False,
            "matched_rule": False,
        }
    return {
        "tenant_id": catalog["tenant"]["id"],
        "organization_name": catalog["tenant"]["organization_name"],
        "recipient_email": recipient,
        **copy.deepcopy(rule),
        "matched_rule": True,
    }


def _automation_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in catalog["automations"]}


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "completed_idempotency_keys": [],
        "completed_automations": [],
        "attempts": {},
        "retries": {},
        "dead_letters": {},
        "completed_schedule_slots": [],
    }


def normalize_runtime_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return a defensive, validated copy of runtime state."""

    value = (
        _empty_state()
        if state is None
        else copy.deepcopy(_require_mapping(state, "state"))
    )
    for field in (
        "completed_idempotency_keys",
        "completed_automations",
        "completed_schedule_slots",
    ):
        _require_list(value.get(field, []), f"state.{field}")
        value.setdefault(field, [])
    for field in ("attempts", "retries", "dead_letters"):
        _require_mapping(value.get(field, {}), f"state.{field}")
        value.setdefault(field, {})
    value.setdefault("schema_version", "1.0")
    return value


def _idempotency_key(
    tenant_id: str, automation_id: str, case_id: str, event_ref: str
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "automation_id": automation_id,
        "case_id": case_id,
        "event_ref_sha256": event_ref,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_event_execution_plan(
    catalog_document: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
    at: str,
) -> dict[str, Any]:
    """Build a deterministic, dependency-aware plan for collector events."""

    catalog = validate_automation_catalog(catalog_document)
    tenant = catalog["tenant"]
    runtime = normalize_runtime_state(state)
    generated_at = _require_timestamp(at, "at").isoformat()
    automations = [
        item
        for item in catalog["automations"]
        if item["trigger"]["type"] == "event"
    ]
    completed_keys = set(runtime["completed_idempotency_keys"])
    completed_pairs = {
        (item.get("tenant_id"), item.get("automation_id"), item.get("case_id"))
        for item in runtime["completed_automations"]
        if isinstance(item, dict)
    }
    dead_letters = set(runtime["dead_letters"])
    batch_keys: set[str] = set()
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for event_index, raw_event in enumerate(_require_list(events, "events")):
        event = _require_mapping(raw_event, f"events[{event_index}]")
        collector_id = _require_string(
            event.get("collector_id"), f"events[{event_index}].collector_id"
        )
        if collector_id not in catalog["collectors"]:
            raise AutomationCatalogError(
                f"events[{event_index}].collector_id is not declared"
            )
        event_type = _require_string(
            event.get("event_type"), f"events[{event_index}].event_type"
        )
        case_id = _require_string(
            event.get("case_id"), f"events[{event_index}].case_id"
        )
        event_ref = _require_string(
            event.get("event_ref_sha256"),
            f"events[{event_index}].event_ref_sha256",
        )
        if not SHA256_RE.fullmatch(event_ref):
            raise AutomationCatalogError(
                f"events[{event_index}].event_ref_sha256 must be a lowercase SHA-256 digest"
            )
        _require_timestamp(
            event.get("occurred_at"), f"events[{event_index}].occurred_at"
        )

        matches = [
            automation
            for automation in automations
            if automation["trigger"]["collector_id"] == collector_id
            and automation["trigger"]["event_type"] == event_type
        ]
        if not matches:
            ignored.append(
                {
                    "event_index": event_index,
                    "case_id": case_id,
                    "status": "no_matching_automation",
                }
            )
            continue

        for automation in matches:
            key = _idempotency_key(
                tenant["id"], automation["id"], case_id, event_ref
            )
            item = {
                "tenant_id": tenant["id"],
                "organization_name": tenant["organization_name"],
                "automation_id": automation["id"],
                "owner_role": automation["owner_role"],
                "owner": automation["owner"],
                "case_id": case_id,
                "event_ref_sha256": event_ref,
                "idempotency_key": key,
                "priority": automation["priority"],
                "objective": automation["objective"],
                "output": automation["output"],
                "layer": copy.deepcopy(automation["layer"]),
                "action": copy.deepcopy(automation["action"]),
            }
            if not automation["enabled"]:
                ignored.append({**item, "status": "disabled"})
                continue
            if key in completed_keys or key in batch_keys:
                ignored.append({**item, "status": "duplicate"})
                continue
            if key in dead_letters:
                ignored.append({**item, "status": "dead_letter"})
                continue
            missing_dependencies = [
                dependency
                for dependency in automation["dependencies"]
                if (tenant["id"], dependency, case_id) not in completed_pairs
            ]
            if missing_dependencies:
                blocked.append(
                    {
                        **item,
                        "status": "blocked_dependencies",
                        "missing_dependencies": missing_dependencies,
                    }
                )
                batch_keys.add(key)
                continue
            ready.append({**item, "status": "ready"})
            batch_keys.add(key)

    ready.sort(
        key=lambda item: (
            -item["priority"],
            item["automation_id"],
            item["case_id"],
        )
    )
    blocked.sort(
        key=lambda item: (
            -item["priority"],
            item["automation_id"],
            item["case_id"],
        )
    )
    return {
        "tenant_id": tenant["id"],
        "generated_at": generated_at,
        "timezone": catalog["timezone"],
        "ready": ready,
        "blocked": blocked,
        "ignored": ignored,
    }


def build_schedule_execution_plan(
    catalog_document: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    at: str,
) -> dict[str, Any]:
    """Return schedule automations due in the catalog timezone for the given minute."""

    catalog = validate_automation_catalog(catalog_document)
    tenant = catalog["tenant"]
    runtime = normalize_runtime_state(state)
    instant = _require_timestamp(at, "at")
    local = instant.astimezone(ZoneInfo(catalog["timezone"]))
    completed_slots = set(runtime["completed_schedule_slots"])
    due: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for automation in catalog["automations"]:
        trigger = automation["trigger"]
        if trigger["type"] != "schedule":
            continue
        hour, minute = _parse_local_time(trigger["local_time"], "local_time")
        slot = (
            f"{tenant['id']}:{automation['id']}:"
            f"{local.date().isoformat()}:{trigger['local_time']}"
        )
        base = {
            "tenant_id": tenant["id"],
            "organization_name": tenant["organization_name"],
            "automation_id": automation["id"],
            "owner_role": automation["owner_role"],
            "owner": automation["owner"],
            "schedule_slot": slot,
            "priority": automation["priority"],
            "objective": automation["objective"],
            "output": automation["output"],
            "layer": copy.deepcopy(automation["layer"]),
            "action": copy.deepcopy(automation["action"]),
        }
        if not automation["enabled"]:
            ignored.append({**base, "status": "disabled"})
        elif local.weekday() not in trigger["weekdays"] or (
            local.hour,
            local.minute,
        ) != (hour, minute):
            ignored.append({**base, "status": "not_due"})
        elif slot in completed_slots:
            ignored.append({**base, "status": "duplicate"})
        else:
            due.append({**base, "status": "ready"})

    due.sort(key=lambda item: (-item["priority"], item["automation_id"]))
    return {
        "tenant_id": tenant["id"],
        "generated_at": instant.isoformat(),
        "local_time": local.isoformat(),
        "timezone": catalog["timezone"],
        "ready": due,
        "ignored": ignored,
    }


def record_execution_outcome(
    catalog_document: dict[str, Any],
    work_item: dict[str, Any],
    *,
    state: dict[str, Any] | None,
    succeeded: bool,
    at: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Record success or schedule bounded exponential retry/dead-letter handling."""

    catalog = validate_automation_catalog(catalog_document)
    runtime = normalize_runtime_state(state)
    automation_id = _require_string(
        work_item.get("automation_id"), "work_item.automation_id"
    )
    automation = _automation_index(catalog).get(automation_id)
    if automation is None:
        raise AutomationCatalogError(
            "work_item.automation_id is not in the catalog"
        )
    tenant_id = _require_string(work_item.get("tenant_id"), "work_item.tenant_id")
    if tenant_id != catalog["tenant"]["id"]:
        raise AutomationCatalogError(
            "work_item.tenant_id does not match the automation catalog"
        )
    instant = _require_timestamp(at, "at")
    key = work_item.get("idempotency_key") or work_item.get("schedule_slot")
    key = _require_string(key, "work_item idempotency key or schedule slot")

    if succeeded:
        if (
            "idempotency_key" in work_item
            and key not in runtime["completed_idempotency_keys"]
        ):
            runtime["completed_idempotency_keys"].append(key)
            pair = {
                "tenant_id": tenant_id,
                "automation_id": automation_id,
                "case_id": work_item.get("case_id"),
            }
            if pair not in runtime["completed_automations"]:
                runtime["completed_automations"].append(pair)
        if (
            "schedule_slot" in work_item
            and key not in runtime["completed_schedule_slots"]
        ):
            runtime["completed_schedule_slots"].append(key)
        runtime["attempts"].pop(key, None)
        runtime["retries"].pop(key, None)
        runtime["dead_letters"].pop(key, None)
        return {
            "state": runtime,
            "result": {"status": "completed", "key": key},
        }

    code = _require_string(error_code, "error_code")
    attempts = runtime["attempts"].get(key, 0) + 1
    runtime["attempts"][key] = attempts
    retry = automation["retry"]
    if (
        automation["failure_policy"] == "manual_review"
        or attempts >= retry["max_attempts"]
    ):
        runtime["retries"].pop(key, None)
        runtime["dead_letters"][key] = {
            "tenant_id": tenant_id,
            "automation_id": automation_id,
            "case_id": work_item.get("case_id"),
            "attempts": attempts,
            "error_code": code,
            "failed_at": instant.isoformat(),
        }
        return {
            "state": runtime,
            "result": {
                "status": "dead_letter",
                "key": key,
                "attempts": attempts,
                "error_code": code,
            },
        }

    delay_seconds = min(
        retry["base_delay_seconds"] * (2 ** (attempts - 1)),
        retry["max_delay_seconds"],
    )
    retry_at = instant + timedelta(seconds=delay_seconds)
    runtime["retries"][key] = {
        "tenant_id": tenant_id,
        "automation_id": automation_id,
        "case_id": work_item.get("case_id"),
        "attempts": attempts,
        "error_code": code,
        "retry_at": retry_at.isoformat(),
    }
    return {
        "state": runtime,
        "result": {
            "status": "retry_scheduled",
            "key": key,
            "attempts": attempts,
            "retry_at": retry_at.isoformat(),
            "error_code": code,
        },
    }


def build_health_report(
    catalog_document: dict[str, Any],
    *,
    state: dict[str, Any] | None,
    at: str,
    archive_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize enabled automations, due retries, dead letters and documents.

    The archive backlog is supplied already computed, so the control plane keeps
    orchestration rules separate from storage and provider code.  Every entry
    must belong to the catalog tenant, otherwise the report is rejected.
    """

    catalog = validate_automation_catalog(catalog_document)
    runtime = normalize_runtime_state(state)
    instant = _require_timestamp(at, "at")
    tenant_id = catalog["tenant"]["id"]
    due_retries = [
        {"key": key, **copy.deepcopy(value)}
        for key, value in runtime["retries"].items()
        if value.get("tenant_id") == tenant_id
        if _require_timestamp(
            value.get("retry_at"), f"state.retries.{key}.retry_at"
        )
        <= instant
    ]
    dead_letters = [
        {"key": key, **copy.deepcopy(value)}
        for key, value in runtime["dead_letters"].items()
        if value.get("tenant_id") == tenant_id
    ]
    enabled = sum(1 for item in catalog["automations"] if item["enabled"])
    documents = _summarize_document_archive(archive_report, tenant_id)
    needs_attention = bool(dead_letters) or documents["attention_required"]
    return {
        "tenant_id": tenant_id,
        "organization_name": catalog["tenant"]["organization_name"],
        "generated_at": instant.isoformat(),
        "timezone": catalog["timezone"],
        "status": "attention_required" if needs_attention else "healthy",
        "enabled_automations": enabled,
        "disabled_automations": len(catalog["automations"]) - enabled,
        "due_retries": sorted(due_retries, key=lambda item: item["retry_at"]),
        "dead_letters": sorted(
            dead_letters, key=lambda item: item["failed_at"]
        ),
        "document_archive": documents,
    }


ARCHIVE_ATTENTION_ACTIONS = {"reconcile_monday_upload", "retry_monday_upload"}


def _summarize_document_archive(
    archive_report: dict[str, Any] | None, tenant_id: str
) -> dict[str, Any]:
    """Group archived PDFs by the operator action each one still needs.

    A document waiting for reconciliation or for its authorized retry needs a
    person, so it raises the report to attention_required.  A document merely
    waiting to be published does not, unless a publication was already refused:
    a refusal that repeats is a configuration fault, not a queue.
    """

    summary: dict[str, Any] = {
        "configured": archive_report is not None,
        "entry_count": 0,
        "unresolved_count": 0,
        "awaiting_reconciliation": [],
        "awaiting_retry": [],
        "awaiting_publication": [],
        "refused_documents": 0,
        "attention_required": False,
    }
    if archive_report is None:
        return summary

    report = _require_mapping(archive_report, "archive_report")
    entries = _require_list(report.get("entries", []), "archive_report.entries")
    grouped = {
        "reconcile_monday_upload": summary["awaiting_reconciliation"],
        "retry_monday_upload": summary["awaiting_retry"],
        "publish_monday_upload": summary["awaiting_publication"],
    }
    for position, raw_entry in enumerate(entries):
        entry = _require_mapping(raw_entry, f"archive_report.entries.{position}")
        if _require_string(
            entry.get("tenant_id"), f"archive_report.entries.{position}.tenant_id"
        ) != tenant_id:
            raise AutomationCatalogError(
                "Document archive report belongs to another tenant"
            )
        action = _require_string(
            entry.get("action_required"),
            f"archive_report.entries.{position}.action_required",
        )
        refused = entry.get("refused_attempts", 0)
        if not isinstance(refused, int) or isinstance(refused, bool) or refused < 0:
            raise AutomationCatalogError("Document archive refusals are invalid")
        if refused:
            summary["refused_documents"] += 1
        bucket = grouped.get(action)
        if bucket is not None:
            bucket.append(
                {
                    "case_id": _require_string(
                        entry.get("case_id"),
                        f"archive_report.entries.{position}.case_id",
                    ),
                    "content_sha256": _require_string(
                        entry.get("content_sha256"),
                        f"archive_report.entries.{position}.content_sha256",
                    ),
                    "document_type": _require_string(
                        entry.get("document_type"),
                        f"archive_report.entries.{position}.document_type",
                    ),
                    "action_required": action,
                }
            )
        if action in ARCHIVE_ATTENTION_ACTIONS:
            summary["attention_required"] = True

    summary["entry_count"] = len(entries)
    summary["unresolved_count"] = (
        len(summary["awaiting_reconciliation"])
        + len(summary["awaiting_retry"])
        + len(summary["awaiting_publication"])
    )
    if summary["refused_documents"]:
        summary["attention_required"] = True
    for bucket in grouped.values():
        bucket.sort(key=lambda item: (item["case_id"], item["content_sha256"]))
    return summary
