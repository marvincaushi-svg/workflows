from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflowos.audit import build_audit_log, verify_audit_log
from workflowos.core import (
    WorkflowError,
    build_case_from_email,
    evaluate_case,
    load_document,
)
from workflowos.technical_review import (
    create_remediation_plan,
    create_remediation_work,
    evaluate_technical_review,
    record_monday_document_notification,
)


ROOT = Path(__file__).resolve().parents[1]
PROCESS_PATH = ROOT / "process.schema.yaml"
EMAIL_PATH = ROOT / "examples" / "pilot" / "assignment.email.sanitized.json"
TECHNICAL_REVIEW_PATH = ROOT / "examples" / "pilot" / "technical-review.sanitized.json"


class WorkflowOSMVPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process = load_document(PROCESS_PATH)
        self.email = load_document(EMAIL_PATH)

    def build_pilot_case(self):
        return build_case_from_email(self.process, self.email, "pilot-pv-001")

    def test_real_sanitized_email_reaches_ready_after_content_verification(self):
        case = self.build_pilot_case()
        evaluation = evaluate_case(self.process, case)

        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(evaluation["missing_artifacts"], [])
        self.assertEqual(evaluation["missing_facts"], [])
        self.assertEqual(evaluation["readiness_scope"], "assignment_intake_only")
        self.assertEqual(case["assignment_owner_role"], "sb_energetica")
        self.assertEqual(case["technical_document_owner_role"], "af_elektro")
        self.assertEqual(case["grid_operator_manager_role"], "af_elektro")

    def test_no_unobserved_customer_fields_are_invented(self):
        case = self.build_pilot_case()
        self.assertNotIn("customer", case)
        self.assertNotIn("address", case)
        self.assertEqual(
            {fact["path"] for fact in case["facts"]}, {"requested_start_date"}
        )
        serialized = json.dumps(case).lower()
        self.assertNotIn("glaeser", serialized)
        self.assertNotIn("gläser", serialized)

    def test_missing_project_data_does_not_block_assignment(self):
        email = copy.deepcopy(self.email)
        email["observations"] = []
        email["attachments"] = []

        case = build_case_from_email(self.process, email, "pilot-pv-no-project-data")
        evaluation = evaluate_case(self.process, case)
        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(evaluation["missing_facts"], [])

    def test_sb_project_data_is_optional_input(self):
        email = copy.deepcopy(self.email)
        email["attachments"] = []

        case = build_case_from_email(self.process, email, "pilot-pv-optional-data")
        evaluation = evaluate_case(self.process, case)
        self.assertEqual(evaluation["missing_artifacts"], [])
        self.assertEqual(evaluation["status"], "ready")

    def test_non_email_intake_is_rejected(self):
        not_email = copy.deepcopy(self.email)
        not_email["source_type"] = "form"
        with self.assertRaisesRegex(WorkflowError, "source_type must be email"):
            build_case_from_email(self.process, not_email, "pilot-pv-001")

    def test_sb_cannot_be_configured_as_technical_owner(self):
        process = copy.deepcopy(self.process)
        process["process"]["responsibilities"]["technical_document_owner"] = (
            "sb_energetica"
        )

        with self.assertRaisesRegex(
            WorkflowError,
            "technical_document_owner must be af_elektro",
        ):
            build_case_from_email(process, self.email, "pilot-pv-invalid-owner")

    def test_sb_project_data_cannot_be_made_mandatory(self):
        process = copy.deepcopy(self.process)
        process["process"]["checklist"]["required_artifacts"].append(
            {
                "id": "system-sizing",
                "artifact_type": "system_sizing",
                "description": "System sizing document",
            }
        )

        with self.assertRaisesRegex(
            WorkflowError,
            "Only the SB assignment email may be mandatory",
        ):
            build_case_from_email(process, self.email, "pilot-pv-invalid-input")

    def test_audit_log_detects_tampering(self):
        case = self.build_pilot_case()
        evaluation = evaluate_case(self.process, case)
        events = build_audit_log(case, evaluation, "2026-08-03T10:00:00Z")

        verification = verify_audit_log(events)
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["event_count"], 4)

        tampered = copy.deepcopy(events)
        tampered[2]["data"]["status"] = "blocked"
        with self.assertRaisesRegex(WorkflowError, "Audit hash mismatch"):
            verify_audit_log(tampered)

    def test_cli_runs_end_to_end_and_verifies_written_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "workflowos.cli",
                    "run",
                    "--process",
                    str(PROCESS_PATH),
                    "--email",
                    str(EMAIL_PATH),
                    "--case-id",
                    "pilot-pv-001",
                    "--audit",
                    str(audit_path),
                    "--at",
                    "2026-08-03T10:00:00Z",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual(payload["evaluation"]["status"], "ready")
            self.assertTrue(audit_path.exists())

            verify = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "workflowos.cli",
                    "verify-audit",
                    "--audit",
                    str(audit_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertTrue(json.loads(verify.stdout)["valid"])

    def test_pilot_technical_review_requires_changes(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        decision = evaluate_technical_review(review)

        self.assertEqual(decision["decision"], "changes_required")
        self.assertEqual(decision["verified_controls"], [])
        self.assertEqual(len(decision["missing_controls"]), 12)
        self.assertTrue(decision["professional_signoff_required"])

    def test_missing_control_cannot_be_silently_omitted(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        review["controls"].pop()

        with self.assertRaisesRegex(WorkflowError, "omits required controls"):
            evaluate_technical_review(review)

    def test_non_compliance_is_rejected_not_treated_as_missing(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        single_line_diagram = next(
            control
            for control in review["controls"]
            if control["id"] == "single_line_diagram"
        )
        single_line_diagram["result"] = "non_compliant"
        decision = evaluate_technical_review(review)

        self.assertEqual(decision["decision"], "rejected")
        self.assertEqual(decision["non_compliant_controls"], ["single_line_diagram"])

    def test_changes_required_creates_ordered_af_workstreams_and_sb_handoff(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        decision = evaluate_technical_review(review)
        plan = create_remediation_plan(
            decision,
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )

        self.assertEqual(plan["status"], "open")
        self.assertEqual(plan["type"], "af_technical_remediation_plan")
        self.assertEqual(plan["source_decision"], "changes_required")
        self.assertEqual(plan["assigned_to_role"], "af_elektro")
        self.assertEqual(plan["assignment_source_role"], "sb_energetica")
        self.assertEqual(
            plan["available_project_data_provider_role"], "sb_energetica"
        )
        self.assertEqual(plan["grid_operator_manager_role"], "af_elektro")
        self.assertTrue(plan["technical_controls_do_not_create_sb_requests"])

        work_items = plan["work_items"]
        self.assertEqual(
            [item["type"] for item in work_items],
            [
                "produce_af_technical_design",
                "manage_grid_operator_coordination",
                "perform_commissioning_verification",
                "publish_accepted_documentation_to_sb_monday",
            ],
        )
        self.assertEqual(
            {
                deliverable["control_id"]
                for item in work_items
                for deliverable in item["deliverables"]
                if "control_id" in deliverable
            },
            set(decision["missing_controls"]),
        )
        self.assertTrue(
            all(
                item["assigned_to_role"] == "af_elektro" for item in work_items
            )
        )

    def test_grid_operator_is_contacted_directly_by_af(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        plan = create_remediation_plan(
            evaluate_technical_review(review),
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )

        grid_work = next(
            item
            for item in plan["work_items"]
            if item["type"] == "manage_grid_operator_coordination"
        )
        self.assertEqual(grid_work["external_counterparty_role"], "grid_operator")
        self.assertEqual(
            grid_work["external_counterparty_manager_role"], "af_elektro"
        )
        self.assertNotIn("sb_energetica", json.dumps(grid_work))

    def test_accepted_practice_documents_are_delivered_to_sb_on_monday(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        plan = create_remediation_plan(
            evaluate_technical_review(review),
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )

        handoff = next(
            item
            for item in plan["work_items"]
            if item["type"] == "publish_accepted_documentation_to_sb_monday"
        )
        self.assertEqual(handoff["status"], "blocked")
        self.assertEqual(handoff["assigned_to_role"], "af_elektro")
        self.assertEqual(handoff["delivery_recipient_role"], "sb_energetica")
        self.assertEqual(handoff["document_repository"], "monday")
        self.assertEqual(handoff["trigger"], "grid_operator_practices_accepted")
        self.assertEqual(
            handoff["depends_on"],
            ["pilot-pv-001.grid-operator-coordination.1"],
        )

    def test_monday_notifications_prepare_complete_email_to_sb(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        plan = create_remediation_plan(
            evaluate_technical_review(review),
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )
        handoff = next(
            item
            for item in plan["work_items"]
            if item["type"] == "publish_accepted_documentation_to_sb_monday"
        )

        document_types = [
            "tag_grid_connection_application",
            "installation_notice_ia",
            "single_line_diagram",
        ]
        for index, document_type in enumerate(document_types, start=1):
            handoff = record_monday_document_notification(
                handoff,
                {
                    "sanitized": True,
                    "source": "monday",
                    "event_type": "document_uploaded",
                    "document_type": document_type,
                    "content_verified": True,
                    "grid_operator_practices_accepted": index == 3,
                    "event_ref_sha256": f"{index:064x}",
                },
            )

        self.assertEqual(handoff["status"], "ready_for_email_approval")
        self.assertEqual(handoff["email_delivery"]["status"], "draft_ready")
        self.assertEqual(handoff["email_delivery"]["recipient_role"], "sb_energetica")
        self.assertEqual(handoff["email_delivery"]["channel"], "email")
        self.assertEqual(
            handoff["email_delivery"]["send_mode"], "human_approval_required"
        )
        self.assertEqual(
            handoff["email_delivery"]["attachment_document_types"], document_types
        )

    def test_monday_notification_is_idempotent_and_acceptance_gated(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        plan = create_remediation_plan(
            evaluate_technical_review(review),
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )
        handoff = next(
            item
            for item in plan["work_items"]
            if item["type"] == "publish_accepted_documentation_to_sb_monday"
        )
        notification = {
            "sanitized": True,
            "source": "monday",
            "event_type": "document_uploaded",
            "document_type": "tag_grid_connection_application",
            "content_verified": True,
            "grid_operator_practices_accepted": False,
            "event_ref_sha256": "1" * 64,
        }

        once = record_monday_document_notification(handoff, notification)
        twice = record_monday_document_notification(once, notification)

        self.assertEqual(once, twice)
        self.assertEqual(len(twice["notification_refs"]), 1)
        self.assertEqual(twice["status"], "blocked")
        self.assertEqual(
            twice["email_delivery"]["status"], "waiting_for_documents"
        )

    def test_only_missing_workstreams_are_created(self):
        decision = {
            "decision": "changes_required",
            "missing_controls": ["grid_operator_requirements"],
        }
        plan = create_remediation_plan(
            decision,
            "pilot-pv-subset",
            "2026-08-03T13:00:00Z",
        )

        self.assertEqual(len(plan["work_items"]), 2)
        self.assertEqual(
            plan["work_items"][0]["type"], "manage_grid_operator_coordination"
        )
        self.assertEqual(plan["work_items"][0]["depends_on"], [])
        self.assertEqual(
            plan["work_items"][1]["type"],
            "publish_accepted_documentation_to_sb_monday",
        )

    def test_approved_review_does_not_create_remediation_work(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        for control in review["controls"]:
            control["result"] = "verified"
        decision = evaluate_technical_review(review)

        with self.assertRaisesRegex(WorkflowError, "decision=changes_required"):
            create_remediation_plan(
                decision,
                "pilot-pv-001",
                "2026-08-03T13:00:00Z",
            )

    def test_legacy_function_uses_the_same_work_plan(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        decision = evaluate_technical_review(review)

        self.assertEqual(
            create_remediation_work(
                decision, "pilot-pv-001", "2026-08-03T13:00:00Z"
            ),
            create_remediation_plan(
                decision, "pilot-pv-001", "2026-08-03T13:00:00Z"
            ),
        )

    def test_cli_creates_af_owned_work_plan(self):
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "workflowos.cli",
                "create-af-work-plan",
                "--review",
                str(TECHNICAL_REVIEW_PATH),
                "--case-id",
                "pilot-pv-001",
                "--at",
                "2026-08-03T13:00:00Z",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        plan = json.loads(run.stdout)
        self.assertEqual(plan["plan_id"], "pilot-pv-001.technical-remediation.1")
        self.assertEqual(len(plan["work_items"]), 4)
        self.assertNotIn("customer", json.dumps(plan).lower())


if __name__ == "__main__":
    unittest.main()
