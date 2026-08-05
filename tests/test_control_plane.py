from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from workflowos.control_plane import (
    AutomationCatalogError,
    build_daily_operations_brief,
    build_event_execution_plan,
    build_health_report,
    build_schedule_execution_plan,
    record_execution_outcome,
    resolve_communication_policy,
    validate_automation_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = (
    ROOT
    / "examples"
    / "tenants"
    / "electrical-contractor"
    / "automation.catalog.sanitized.json"
)


class AutomationControlPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    def event(self, event_type, collector_id="workflowos.event", ref="a" * 64):
        return {
            "collector_id": collector_id,
            "event_type": event_type,
            "case_id": "case-001",
            "event_ref_sha256": ref,
            "occurred_at": "2026-08-04T06:00:00Z",
        }

    def completed(self, *automation_ids):
        return {
            "completed_idempotency_keys": [],
            "completed_automations": [
                {
                    "tenant_id": self.catalog["tenant"]["id"],
                    "automation_id": automation_id,
                    "case_id": "case-001",
                }
                for automation_id in automation_ids
            ],
            "attempts": {},
            "retries": {},
            "dead_letters": {},
            "completed_schedule_slots": [],
        }

    def test_catalog_preserves_one_objective_and_one_output(self):
        normalized = validate_automation_catalog(self.catalog)
        self.assertEqual(
            normalized["tenant"]["id"], "example-electrical-contractor"
        )
        self.assertEqual(normalized["timezone"], "Europe/Zurich")
        self.assertEqual(len(normalized["automations"]), 8)
        self.assertTrue(
            all(isinstance(item["objective"], str) for item in normalized["automations"])
        )
        self.assertTrue(
            all(isinstance(item["output"], str) for item in normalized["automations"])
        )

    def test_external_sources_require_declared_collectors(self):
        invalid = copy.deepcopy(self.catalog)
        invalid["automations"][0]["trigger"]["collector_id"] = "gmail.direct"
        with self.assertRaisesRegex(AutomationCatalogError, "declared collector"):
            validate_automation_catalog(invalid)

    def test_guarded_send_requires_global_safety_guards(self):
        invalid = copy.deepcopy(self.catalog)
        delivery = next(
            item
            for item in invalid["automations"]
            if item["id"] == "accepted-practices-delivery"
        )
        delivery["action"]["guards"].remove("idempotency_key")
        with self.assertRaisesRegex(AutomationCatalogError, "idempotency_key"):
            validate_automation_catalog(invalid)

    def test_swiss_german_rule_is_exact_for_internal_recipient(self):
        internal = resolve_communication_policy(
            self.catalog, "Operations <operations@example.com>"
        )
        other = resolve_communication_policy(self.catalog, "giada@example.com")
        self.assertEqual(internal["language"], "de-CH")
        self.assertTrue(internal["swiss_spelling"])
        self.assertTrue(internal["matched_rule"])
        self.assertEqual(other["language"], "it-CH")
        self.assertFalse(other["matched_rule"])

    def test_automation_owner_resolves_through_tenant_roles(self):
        normalized = validate_automation_catalog(self.catalog)
        work_plan = next(
            item
            for item in normalized["automations"]
            if item["id"] == "technical-work-plan"
        )
        self.assertEqual(work_plan["owner_role"], "technical_document_owner")
        self.assertEqual(work_plan["owner"], "technical_team")

        invalid = copy.deepcopy(self.catalog)
        invalid["automations"][0]["owner_role"] = "undeclared_role"
        with self.assertRaisesRegex(AutomationCatalogError, "tenant.roles"):
            validate_automation_catalog(invalid)

    def test_second_tenant_is_isolated_without_product_code_changes(self):
        second = copy.deepcopy(self.catalog)
        second["tenant"] = {
            "id": "second-electrical-company",
            "organization_name": "Second Electrical Company AG",
            "roles": {
                "assignment_owner": "customer_success",
                "available_project_data_provider": "project_intake",
                "technical_document_owner": "engineering",
                "grid_operator_manager": "permits",
            },
        }
        second["policies"]["communication"]["recipient_rules"] = {
            "operations@example.com": {
                "language": "de-CH",
                "swiss_spelling": True,
            }
        }

        event = self.event("technical_review_changes_required")
        pilot = build_event_execution_plan(
            self.catalog,
            [event],
            state=self.completed("assignment-intake"),
            at="2026-08-04T06:01:00Z",
        )
        second_state = self.completed("assignment-intake")
        second_state["completed_automations"][0]["tenant_id"] = (
            "second-electrical-company"
        )
        other = build_event_execution_plan(
            second,
            [event],
            state=second_state,
            at="2026-08-04T06:01:00Z",
        )

        self.assertEqual(other["tenant_id"], "second-electrical-company")
        self.assertEqual(other["ready"][0]["owner"], "engineering")
        self.assertNotEqual(
            pilot["ready"][0]["idempotency_key"],
            other["ready"][0]["idempotency_key"],
        )

        with self.assertRaisesRegex(AutomationCatalogError, "tenant_id"):
            record_execution_outcome(
                second,
                pilot["ready"][0],
                state=second_state,
                succeeded=True,
                at="2026-08-04T06:02:00Z",
            )

    def test_health_report_excludes_another_tenants_failures(self):
        second = copy.deepcopy(self.catalog)
        second["tenant"]["id"] = "second-electrical-company"
        state = self.completed()
        state["dead_letters"] = {
            "pilot-key": {
                "tenant_id": "example-electrical-contractor",
                "automation_id": "technical-work-plan",
                "case_id": "case-001",
                "attempts": 3,
                "error_code": "pilot_error",
                "failed_at": "2026-08-04T06:00:00Z",
            },
            "other-key": {
                "tenant_id": "second-electrical-company",
                "automation_id": "technical-work-plan",
                "case_id": "case-002",
                "attempts": 3,
                "error_code": "other_error",
                "failed_at": "2026-08-04T06:00:00Z",
            },
        }

        pilot_health = build_health_report(
            self.catalog, state=state, at="2026-08-04T06:01:00Z"
        )
        other_health = build_health_report(
            second, state=state, at="2026-08-04T06:01:00Z"
        )

        self.assertEqual(
            [item["error_code"] for item in pilot_health["dead_letters"]],
            ["pilot_error"],
        )
        self.assertEqual(
            [item["error_code"] for item in other_health["dead_letters"]],
            ["other_error"],
        )

    def test_event_plan_blocks_until_case_dependency_is_completed(self):
        event = self.event("technical_review_changes_required")
        blocked = build_event_execution_plan(
            self.catalog,
            [event],
            state=self.completed(),
            at="2026-08-04T06:01:00Z",
        )
        self.assertEqual(
            blocked["blocked"][0]["missing_dependencies"],
            ["assignment-intake"],
        )

        ready = build_event_execution_plan(
            self.catalog,
            [event],
            state=self.completed("assignment-intake"),
            at="2026-08-04T06:01:00Z",
        )
        self.assertEqual(
            ready["ready"][0]["automation_id"], "technical-work-plan"
        )

    def test_duplicate_events_create_only_one_ready_work_item(self):
        event = self.event("technical_review_changes_required")
        plan = build_event_execution_plan(
            self.catalog,
            [event, copy.deepcopy(event)],
            state=self.completed("assignment-intake"),
            at="2026-08-04T06:01:00Z",
        )
        self.assertEqual(len(plan["ready"]), 1)
        self.assertEqual(plan["ignored"][0]["status"], "duplicate")

    def test_retry_is_bounded_then_moves_to_dead_letter(self):
        event = self.event("technical_review_changes_required")
        state = self.completed("assignment-intake")
        plan = build_event_execution_plan(
            self.catalog,
            [event],
            state=state,
            at="2026-08-04T06:01:00Z",
        )
        item = plan["ready"][0]

        first = record_execution_outcome(
            self.catalog,
            item,
            state=state,
            succeeded=False,
            error_code="temporary_provider_error",
            at="2026-08-04T06:02:00Z",
        )
        self.assertEqual(first["result"]["status"], "retry_scheduled")
        self.assertEqual(
            first["result"]["retry_at"], "2026-08-04T06:07:00+00:00"
        )

        second = record_execution_outcome(
            self.catalog,
            item,
            state=first["state"],
            succeeded=False,
            error_code="temporary_provider_error",
            at="2026-08-04T06:07:00Z",
        )
        third = record_execution_outcome(
            self.catalog,
            item,
            state=second["state"],
            succeeded=False,
            error_code="temporary_provider_error",
            at="2026-08-04T06:17:00Z",
        )
        self.assertEqual(third["result"]["status"], "dead_letter")
        health = build_health_report(
            self.catalog,
            state=third["state"],
            at="2026-08-04T06:18:00Z",
        )
        self.assertEqual(health["status"], "attention_required")
        self.assertEqual(len(health["dead_letters"]), 1)

    def test_schedule_uses_europe_zurich_and_is_idempotent(self):
        plan = build_schedule_execution_plan(
            self.catalog,
            state=None,
            at="2026-08-04T05:00:00Z",
        )
        daily = next(
            item
            for item in plan["ready"]
            if item["automation_id"] == "daily-operations-brief"
        )
        completed = record_execution_outcome(
            self.catalog,
            daily,
            state=None,
            succeeded=True,
            at="2026-08-04T05:00:10Z",
        )
        repeated = build_schedule_execution_plan(
            self.catalog,
            state=completed["state"],
            at="2026-08-04T05:00:30Z",
        )
        daily_again = next(
            item
            for item in repeated["ignored"]
            if item["automation_id"] == "daily-operations-brief"
        )
        self.assertEqual(daily_again["status"], "duplicate")


if __name__ == "__main__":
    unittest.main()


class DocumentArchiveHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.tenant_id = cls.catalog["tenant"]["id"]

    def entry(self, **changes):
        values = {
            "tenant_id": self.tenant_id,
            "case_id": "case-042",
            "content_sha256": "a" * 64,
            "document_type": "tag_grid_connection_application",
            "monday_status": "upload_in_doubt",
            "upload_attempts": 1,
            "refused_attempts": 0,
            "action_required": "reconcile_monday_upload",
        }
        values.update(changes)
        return values

    def report(self, *entries):
        return {
            "status": "ok",
            "entry_count": len(entries),
            "unresolved_count": len(entries),
            "entries": list(entries),
        }

    def health(self, archive_report):
        return build_health_report(
            self.catalog,
            state=None,
            at="2026-08-04T06:01:00Z",
            archive_report=archive_report,
        )

    def test_archive_is_absent_until_a_root_is_inspected(self):
        health = self.health(None)

        self.assertFalse(health["document_archive"]["configured"])
        self.assertEqual(health["document_archive"]["unresolved_count"], 0)
        self.assertEqual(health["status"], "healthy")

    def test_document_awaiting_reconciliation_requires_attention(self):
        health = self.health(self.report(self.entry()))

        archive = health["document_archive"]
        self.assertEqual(health["status"], "attention_required")
        self.assertEqual(len(archive["awaiting_reconciliation"]), 1)
        self.assertEqual(
            archive["awaiting_reconciliation"][0]["case_id"], "case-042"
        )
        self.assertEqual(archive["awaiting_retry"], [])

    def test_document_awaiting_authorized_retry_requires_attention(self):
        health = self.health(
            self.report(
                self.entry(
                    monday_status="retry_authorized",
                    action_required="retry_monday_upload",
                )
            )
        )

        self.assertEqual(health["status"], "attention_required")
        self.assertEqual(len(health["document_archive"]["awaiting_retry"]), 1)

    def test_plain_publication_backlog_is_not_an_alert(self):
        health = self.health(
            self.report(
                self.entry(
                    monday_status="pending",
                    upload_attempts=0,
                    action_required="publish_monday_upload",
                )
            )
        )

        archive = health["document_archive"]
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(len(archive["awaiting_publication"]), 1)
        self.assertEqual(archive["refused_documents"], 0)

    def test_repeated_refusal_is_a_configuration_fault(self):
        health = self.health(
            self.report(
                self.entry(
                    monday_status="pending",
                    upload_attempts=0,
                    refused_attempts=3,
                    action_required="publish_monday_upload",
                )
            )
        )

        self.assertEqual(health["status"], "attention_required")
        self.assertEqual(health["document_archive"]["refused_documents"], 1)

    def test_resolved_document_is_counted_but_needs_no_action(self):
        health = self.health(
            self.report(
                self.entry(monday_status="uploaded", action_required="none")
            )
        )

        archive = health["document_archive"]
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(archive["entry_count"], 1)
        self.assertEqual(archive["unresolved_count"], 0)

    def test_report_of_another_tenant_is_rejected(self):
        with self.assertRaisesRegex(AutomationCatalogError, "another tenant"):
            self.health(self.report(self.entry(tenant_id="tenant-other-001")))

    def test_malformed_archive_report_is_rejected(self):
        with self.assertRaises(AutomationCatalogError):
            self.health({"entries": "not-a-list"})
        with self.assertRaises(AutomationCatalogError):
            self.health(self.report(self.entry(refused_attempts=-1)))


class DailyOperationsBriefTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.tenant_id = cls.catalog["tenant"]["id"]

    # 07:00 local on a Tuesday: the brief's own schedule slot is due.
    BRIEF_TIME = "2026-08-04T05:00:00Z"

    def event(self, event_type, ref="a" * 64, collector_id="workflowos.event"):
        return {
            "collector_id": collector_id,
            "event_type": event_type,
            "case_id": "case-001",
            "event_ref_sha256": ref,
            "occurred_at": "2026-08-04T04:10:00Z",
        }

    def brief(self, **changes):
        arguments = {"state": None, "at": self.BRIEF_TIME}
        arguments.update(changes)
        return build_daily_operations_brief(self.catalog, **arguments)

    def archive_report(self, action_required, refused_attempts=0):
        return {
            "status": "ok",
            "entry_count": 1,
            "unresolved_count": 1,
            "entries": [
                {
                    "tenant_id": self.tenant_id,
                    "case_id": "case-042",
                    "content_sha256": "a" * 64,
                    "document_type": "tag_grid_connection_application",
                    "monday_status": "upload_in_doubt",
                    "upload_attempts": 1,
                    "refused_attempts": refused_attempts,
                    "action_required": action_required,
                }
            ],
        }

    def test_scheduled_work_due_now_is_ready(self):
        brief = self.brief()

        scheduled = [item for item in brief["ready"] if item["source"] == "schedule"]
        self.assertIn(
            "daily-operations-brief",
            [item["automation_id"] for item in scheduled],
        )
        self.assertEqual(brief["status"], "healthy")
        self.assertEqual(brief["totals"]["blocked"], 0)

    def test_event_work_appears_with_its_case(self):
        brief = self.brief(
            events=[self.event("assignment_received", collector_id="email.assignment")]
        )

        event_work = [item for item in brief["ready"] if item["source"] == "event"]
        self.assertEqual(event_work[0]["automation_id"], "assignment-intake")
        self.assertEqual(event_work[0]["case_id"], "case-001")
        self.assertEqual(event_work[0]["action_mode"], "internal_write")

    def test_unmet_dependency_is_reported_as_blocked_not_ready(self):
        brief = self.brief(
            events=[self.event("technical_review_changes_required")]
        )

        self.assertEqual(brief["totals"]["blocked"], 1)
        blocked = brief["blocked"][0]
        self.assertEqual(blocked["automation_id"], "technical-work-plan")
        self.assertEqual(blocked["missing_dependencies"], ["assignment-intake"])
        self.assertNotIn(
            "technical-work-plan",
            [item["automation_id"] for item in brief["ready"]],
        )

    def test_retries_and_dead_letters_are_carried_over(self):
        state = {
            "retries": {
                "retry-key": {
                    "tenant_id": self.tenant_id,
                    "automation_id": "assignment-intake",
                    "case_id": "case-001",
                    "attempts": 2,
                    "retry_at": "2026-08-04T04:00:00Z",
                }
            },
            "dead_letters": {
                "dead-key": {
                    "tenant_id": self.tenant_id,
                    "automation_id": "technical-work-plan",
                    "case_id": "case-002",
                    "attempts": 3,
                    "error_code": "provider_error",
                    "failed_at": "2026-08-04T03:00:00Z",
                }
            },
        }

        brief = self.brief(state=state)

        self.assertEqual(brief["totals"]["retrying"], 1)
        self.assertEqual(brief["totals"]["failed"], 1)
        self.assertEqual(brief["retrying"][0]["automation_id"], "assignment-intake")
        self.assertEqual(brief["failed"][0]["error_code"], "provider_error")
        self.assertEqual(brief["status"], "attention_required")

    def test_documents_needing_a_person_are_counted(self):
        brief = self.brief(
            archive_report=self.archive_report("reconcile_monday_upload")
        )

        self.assertEqual(brief["totals"]["documents_awaiting_action"], 1)
        self.assertEqual(brief["status"], "attention_required")
        self.assertEqual(
            brief["documents"]["awaiting_reconciliation"][0]["case_id"], "case-042"
        )

    def test_publication_backlog_alone_does_not_raise_the_brief(self):
        brief = self.brief(
            archive_report=self.archive_report("publish_monday_upload")
        )

        self.assertEqual(brief["totals"]["documents_awaiting_action"], 0)
        self.assertEqual(brief["status"], "healthy")

    def test_brief_never_mutates_the_runtime_state(self):
        state = {
            "completed_automations": [],
            "retries": {},
            "dead_letters": {},
        }
        snapshot = copy.deepcopy(state)

        self.brief(
            state=state,
            events=[self.event("assignment_received", collector_id="email.assignment")],
        )

        self.assertEqual(state, snapshot)

    def test_archive_report_of_another_tenant_is_rejected(self):
        report = self.archive_report("reconcile_monday_upload")
        report["entries"][0]["tenant_id"] = "tenant-other-001"

        with self.assertRaisesRegex(AutomationCatalogError, "another tenant"):
            self.brief(archive_report=report)
