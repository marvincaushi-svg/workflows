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


ROOT = Path(__file__).resolve().parents[1]
PROCESS_PATH = ROOT / "process.schema.yaml"
EMAIL_PATH = ROOT / "examples" / "pilot" / "assignment.email.sanitized.json"


class WorkflowOSMVPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process = load_document(PROCESS_PATH)
        self.email = load_document(EMAIL_PATH)

    def build_pilot_case(self):
        return build_case_from_email(self.process, self.email, "pilot-pv-001")

    def test_real_sanitized_email_reaches_a_verifiable_blocked_state(self):
        case = self.build_pilot_case()
        evaluation = evaluate_case(self.process, case)

        self.assertEqual(evaluation["status"], "blocked")
        self.assertEqual(
            evaluation["missing_artifacts"],
            [
                "signed_architectural_layout",
                "roofing_plan",
                "bill_of_materials",
            ],
        )
        self.assertEqual(evaluation["missing_facts"], [])
        self.assertEqual(evaluation["readiness_scope"], "document_intake_only")

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

    def test_complete_evidence_package_reaches_ready(self):
        email = copy.deepcopy(self.email)
        email["attachments"][1]["artifact_type"] = "signed_architectural_layout"
        email["attachments"][1]["classification"] = "content_verified"
        email["attachments"][5]["artifact_type"] = "bill_of_materials"
        email["attachments"][5]["classification"] = "content_verified"
        email["attachments"].append(
            {
                "source_index": 8,
                "mime_type": "application/pdf",
                "artifact_type": "roofing_plan",
                "classification": "content_verified",
            }
        )

        case = build_case_from_email(self.process, email, "pilot-pv-ready")
        evaluation = evaluate_case(self.process, case)
        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(evaluation["missing_artifacts"], [])
        self.assertTrue(
            all(decision["outcome"] == "pass" for decision in evaluation["decisions"])
        )

    def test_unverified_classification_never_satisfies_a_required_artifact(self):
        email = copy.deepcopy(self.email)
        email["attachments"][1]["artifact_type"] = "signed_architectural_layout"
        email["attachments"][1]["classification"] = "signature_not_verified"

        case = build_case_from_email(self.process, email, "pilot-pv-unverified")
        evaluation = evaluate_case(self.process, case)
        self.assertIn("signed_architectural_layout", evaluation["missing_artifacts"])
        self.assertEqual(evaluation["status"], "blocked")

    def test_non_email_intake_is_rejected(self):
        not_email = copy.deepcopy(self.email)
        not_email["source_type"] = "form"
        with self.assertRaisesRegex(WorkflowError, "source_type must be email"):
            build_case_from_email(self.process, not_email, "pilot-pv-001")

    def test_audit_log_detects_tampering(self):
        case = self.build_pilot_case()
        evaluation = evaluate_case(self.process, case)
        events = build_audit_log(case, evaluation, "2026-08-03T10:00:00Z")

        verification = verify_audit_log(events)
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["event_count"], 4)

        tampered = copy.deepcopy(events)
        tampered[2]["data"]["missing_artifacts"] = []
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
            self.assertEqual(payload["evaluation"]["status"], "blocked")
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


if __name__ == "__main__":
    unittest.main()
