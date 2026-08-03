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

    def verification(self, **overrides):
        value = {
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
            outcome = handle_monday_file_event(
                current,
                self.event(
                    column_id,
                    index,
                    grid_operator_practices_accepted=index == 3,
                ),
                self.verification(),
                config or self.config,
                email_sender=sender,
            )
            current = outcome["state"]
        return outcome

    def test_state_binds_both_handoffs_to_one_monday_item(self):
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
        repeated = handle_monday_file_event(
            outcome["state"],
            self.event(
                "schema-column",
                3,
                grid_operator_practices_accepted=True,
            ),
            self.verification(),
            config,
            email_sender=sender,
        )

        self.assertEqual(outcome["result"]["status"], "completed")
        self.assertEqual(repeated["result"]["status"], "already_completed")
        self.assertEqual(len(calls), 1)

    def test_mismatched_document_blocks_email_adapter(self):
        calls = []
        current = self.state
        for index, column_id in enumerate(
            ("tag-column", "ia-column", "schema-column"), start=1
        ):
            identity = copy.deepcopy(self.identity)
            if column_id == "schema-column":
                identity["installation_address"]["house_number"] = "11"
            outcome = handle_monday_file_event(
                current,
                self.event(
                    column_id,
                    index,
                    grid_operator_practices_accepted=index == 3,
                ),
                self.verification(document_identity=identity),
                self.config,
                email_sender=lambda request: calls.append(request),
            )
            current = outcome["state"]

        self.assertEqual(outcome["result"]["status"], "blocked_document_identity")
        self.assertFalse(outcome["result"]["email_sent"])
        self.assertEqual(calls, [])

    def test_signed_safety_report_is_ready_only_after_installation(self):
        outcome = handle_monday_file_event(
            self.state,
            self.event("sina-column", 4, installation_completed=True),
            self.verification(),
            self.config,
        )

        self.assertEqual(outcome["result"]["status"], "ready_test_no_email_sent")
        self.assertEqual(
            outcome["result"]["email_request"]["template"],
            "signed_safety_report",
        )

    def test_unsigned_safety_report_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "professional signoff"):
            handle_monday_file_event(
                self.state,
                self.event("sina-column", 4, installation_completed=True),
                self.verification(professional_signoff_verified=False),
                self.config,
            )

    def test_wrong_board_event_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "board_id does not match"):
            handle_monday_file_event(
                self.state,
                self.event("tag-column", 1, board_id="wrong-board"),
                self.verification(),
                self.config,
            )

    def test_unmapped_column_is_ignored_without_email(self):
        outcome = handle_monday_file_event(
            self.state,
            self.event("unrelated-column", 1),
            self.verification(),
            self.config,
        )

        self.assertEqual(outcome["result"]["status"], "ignored_unmapped_column")
        self.assertFalse(outcome["result"]["email_sent"])


if __name__ == "__main__":
    unittest.main()
