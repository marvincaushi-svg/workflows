from __future__ import annotations

import copy
import unittest
from pathlib import Path

from workflowos.automation import (
    handle_monday_file_event,
    initialize_automation_state,
)
from workflowos.core import WorkflowError, load_document
from workflowos.technical_review import (
    create_remediation_plan,
    evaluate_technical_review,
)


ROOT = Path(__file__).resolve().parents[1]
TECHNICAL_REVIEW_PATH = ROOT / "examples" / "pilot" / "technical-review.sanitized.json"


class WorkflowOSAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        review = load_document(TECHNICAL_REVIEW_PATH)
        self.plan = create_remediation_plan(
            evaluate_technical_review(review),
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )
        self.identity = {
            "customer_name": "Cliente Esempio SA",
            "installation_address": {
                "street": "Via Esempio",
                "house_number": "10",
                "postal_code": "2540",
                "city": "Città Esempio",
            },
            "project_reference": "SB-2026-001",
            "owner_name": "Proprietario Esempio",
            "parcel_number": "1001",
            "pv_power_kwp": "18.80",
            "grid_operator": "Gestore Esempio",
        }
        self.monday_case = {
            "source": "monday",
            "tenant_id": "tenant-test-001",
            "board_id": "board-test-001",
            "item_id": "item-test-001",
            "case_id": "pilot-pv-001",
            "identity": self.identity,
            "sb_email_recipient": {
                "name": "Referente SB",
                "email": "referente@example.invalid",
                "organization_role": "sb_energetica",
                "resolution_status": "verified",
            },
        }
        self.config = {
            "expected_tenant_id": "tenant-test-001",
            "expected_board_id": "board-test-001",
            "mode": "test",
            "allow_external_email": False,
            "document_columns": {
                "tag-column": "tag_grid_connection_application",
                "ia-column": "installation_notice_ia",
                "schema-column": "single_line_diagram",
                "sina-column": "safety_report_rasi_sina",
            },
        }
        self.state = initialize_automation_state(self.plan, self.monday_case)

    def event(self, column_id, index, **overrides):
        value = {
            "source": "monday",
            "event_type": "file_column_changed",
            "tenant_id": "tenant-test-001",
            "board_id": "board-test-001",
            "item_id": "item-test-001",
            "case_id": "pilot-pv-001",
            "column_id": column_id,
            "event_ref_sha256": f"{index:x}" * 64,
            "attachment_ref_sha256": f"{index + 4:x}" * 64,
            "asset_locator": f"monday-asset-{index}",
            "grid_operator_practices_accepted": False,
            "installation_completed": False,
        }
        value.update(overrides)
        return value

    def verification(self, event, **overrides):
        document_type = self.config["document_columns"].get(event["column_id"])
        value = {
            "case_id": event["case_id"],
            "document_type": document_type,
            "attachment_ref_sha256": event["attachment_ref_sha256"],
            "content_verified": True,
            "latest_version": True,
            "identity_extraction_verified": True,
            "professional_signoff_verified": True,
            "document_identity": copy.deepcopy(self.identity),
        }
        value.update(overrides)
        return value

    def process_practice_documents(self, state=None, *, config=None, sender=None):
        current = state or self.state
        for index, column_id in enumerate(
            ("tag-column", "ia-column", "schema-column"), start=1
        ):
            event = self.event(
                column_id,
                index,
                grid_operator_practices_accepted=index == 3,
            )
            outcome = handle_monday_file_event(
                current,
                event,
                self.verification(event),
                config or self.config,
                email_sender=sender,
            )
            current = outcome["state"]
        return outcome

    def test_state_binds_both_handoffs_to_one_monday_item(self):
        self.assertEqual(self.state["tenant_id"], "tenant-test-001")
        self.assertEqual(self.state["board_id"], "board-test-001")
        self.assertEqual(self.state["item_id"], "item-test-001")
        self.assertEqual(
            set(self.state["handoffs"]),
            {"accepted_practices", "installation_completion"},
        )

    def test_test_mode_reaches_email_ready_without_calling_sender(self):
        calls = []

        def sender(request):
            calls.append(request)
            raise AssertionError("test mode must never call the email adapter")

        outcome = self.process_practice_documents(sender=sender)

        self.assertEqual(outcome["result"]["status"], "ready_test_no_email_sent")
        self.assertFalse(outcome["result"]["email_sent"])
        self.assertEqual(calls, [])
        self.assertEqual(
            outcome["result"]["email_request"]["attachment_document_types"],
            [
                "tag_grid_connection_application",
                "installation_notice_ia",
                "single_line_diagram",
            ],
        )
        self.assertEqual(
            outcome["result"]["email_request"]["tenant_id"],
            "tenant-test-001",
        )

    def test_cross_tenant_event_is_rejected_without_state_mutation(self):
        event = self.event("tag-column", 1, tenant_id="tenant-other-001")
        before = copy.deepcopy(self.state)

        with self.assertRaisesRegex(WorkflowError, "tenant_id does not match"):
            handle_monday_file_event(
                self.state,
                event,
                self.verification(event),
                self.config,
            )

        self.assertEqual(self.state, before)

    def test_live_mode_requires_explicit_external_email_switch(self):
        config = copy.deepcopy(self.config)
        config["mode"] = "live"

        with self.assertRaisesRegex(WorkflowError, "allow_external_email=true"):
            self.process_practice_documents(config=config)

    def test_live_mode_records_delivery_and_does_not_send_duplicate(self):
        calls = []
        config = copy.deepcopy(self.config)
        config.update({"mode": "live", "allow_external_email": True})

        def sender(request):
            calls.append(request)
            return {
                "source": "email_adapter",
                "delivery_status": "sent",
                "recipient_email": request["recipient_email"],
                "message_ref_sha256": "f" * 64,
            }

        outcome = self.process_practice_documents(config=config, sender=sender)
        repeated_event = self.event(
            "schema-column",
            3,
            grid_operator_practices_accepted=True,
        )
        repeated = handle_monday_file_event(
            outcome["state"],
            repeated_event,
            self.verification(repeated_event),
            config,
            email_sender=sender,
        )

        self.assertEqual(outcome["result"]["status"], "completed")
        self.assertEqual(repeated["result"]["status"], "ignored_duplicate_event")
        self.assertEqual(len(calls), 1)

    def test_duplicate_event_with_changed_attachment_is_rejected_without_mutation(self):
        first_event = self.event("tag-column", 1)
        first = handle_monday_file_event(
            self.state,
            first_event,
            self.verification(first_event),
            self.config,
        )
        conflicting = self.event("tag-column", 1)
        conflicting["attachment_ref_sha256"] = "e" * 64

        with self.assertRaisesRegex(WorkflowError, "different attachment"):
            handle_monday_file_event(
                first["state"],
                conflicting,
                self.verification(conflicting),
                self.config,
            )

        self.assertEqual(
            first["state"]["asset_locators"]["tag_grid_connection_application"],
            "monday-asset-1",
        )

    def test_verification_is_bound_to_case_type_and_attachment(self):
        event = self.event("tag-column", 1)
        valid = self.verification(event)
        invalid_values = (
            ("case_id", "other-case", "different case"),
            (
                "document_type",
                "installation_notice_ia",
                "different document type",
            ),
            ("attachment_ref_sha256", "f" * 64, "different attachment"),
        )

        for field, value, expected_error in invalid_values:
            with self.subTest(field=field):
                verification = copy.deepcopy(valid)
                verification[field] = value
                with self.assertRaisesRegex(WorkflowError, expected_error):
                    handle_monday_file_event(
                        self.state,
                        event,
                        verification,
                        self.config,
                    )
                self.assertEqual(self.state["asset_locators"], {})

    def test_mismatched_document_blocks_email_adapter(self):
        calls = []
        current = self.state
        for index, column_id in enumerate(
            ("tag-column", "ia-column", "schema-column"), start=1
        ):
            identity = copy.deepcopy(self.identity)
            if column_id == "schema-column":
                identity["installation_address"]["house_number"] = "11"
            event = self.event(
                column_id,
                index,
                grid_operator_practices_accepted=index == 3,
            )
            outcome = handle_monday_file_event(
                current,
                event,
                self.verification(event, document_identity=identity),
                self.config,
                email_sender=lambda request: calls.append(request),
            )
            current = outcome["state"]

        self.assertEqual(outcome["result"]["status"], "blocked_document_identity")
        self.assertFalse(outcome["result"]["email_sent"])
        self.assertEqual(calls, [])

    def test_signed_safety_report_is_ready_only_after_installation(self):
        event = self.event("sina-column", 4, installation_completed=True)
        outcome = handle_monday_file_event(
            self.state,
            event,
            self.verification(event),
            self.config,
        )

        self.assertEqual(outcome["result"]["status"], "ready_test_no_email_sent")
        self.assertEqual(
            outcome["result"]["email_request"]["template"],
            "signed_safety_report",
        )

    def test_unsigned_safety_report_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "professional signoff"):
            event = self.event("sina-column", 4, installation_completed=True)
            handle_monday_file_event(
                self.state,
                event,
                self.verification(event, professional_signoff_verified=False),
                self.config,
            )

    def test_wrong_board_event_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "board_id does not match"):
            event = self.event("tag-column", 1, board_id="wrong-board")
            handle_monday_file_event(
                self.state,
                event,
                self.verification(event),
                self.config,
            )

    def test_unmapped_column_is_ignored_without_email(self):
        event = self.event("unrelated-column", 1)
        outcome = handle_monday_file_event(
            self.state,
            event,
            self.verification(event),
            self.config,
        )

        self.assertEqual(outcome["result"]["status"], "ignored_unmapped_column")
        self.assertFalse(outcome["result"]["email_sent"])


if __name__ == "__main__":
    unittest.main()
