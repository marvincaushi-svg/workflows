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
from workflowos.technical_review import create_remediation_work, evaluate_technical_review


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

    def test_changes_required_creates_one_assignable_work_item(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        decision = evaluate_technical_review(review)
        work = create_remediation_work(
            decision,
            "pilot-pv-001",
            "2026-08-03T13:00:00Z",
        )

        self.assertEqual(work["status"], "open")
        self.assertEqual(work["type"], "produce_af_technical_package")
        self.assertEqual(work["source_decision"], "changes_required")
        self.assertEqual(work["assigned_to_role"], "af_elektro")
        self.assertEqual(work["assignment_source_role"], "sb_energetica")
        self.assertEqual(
            work["available_project_data_provider_role"], "sb_energetica"
        )
        self.assertEqual(work["grid_operator_manager_role"], "af_elektro")
        self.assertEqual(len(work["deliverables"]), 12)
        self.assertEqual(
            {item["control_id"] for item in work["deliverables"]},
            set(decision["missing_controls"]),
        )
        self.assertTrue(
            all(
                item["status"] == "requested" and item["owner_role"] == "af_elektro"
                for item in work["deliverables"]
            )
        )

    def test_approved_review_does_not_create_remediation_work(self):
        review = load_document(TECHNICAL_REVIEW_PATH)
        for control in review["controls"]:
            control["result"] = "verified"
        decision = evaluate_technical_review(review)

        with self.assertRaisesRegex(WorkflowError, "decision=changes_required"):
            create_remediation_work(
                decision,
                "pilot-pv-001",
                "2026-08-03T13:00:00Z",
            )

    def test_cli_creates_af_owned_technical_work(self):
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "workflowos.cli",
                "create-af-technical-work",
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
        work = json.loads(run.stdout)
        self.assertEqual(work["work_id"], "pilot-pv-001.technical-remediation.1")
        self.assertNotIn("customer", json.dumps(work).lower())


if __name__ == "__main__":
    unittest.main()
