from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = (
    ROOT
    / "examples"
    / "tenants"
    / "electrical-contractor"
    / "automation.catalog.sanitized.json"
)
TENANT_ID = "example-electrical-contractor"


class OperationalCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "workflowos.cli", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def work_item(self, automation_id, key, case_id="case-100"):
        path = self.directory / f"{automation_id}-{key}.json"
        path.write_text(
            json.dumps(
                {
                    "tenant_id": TENANT_ID,
                    "automation_id": automation_id,
                    "case_id": case_id,
                    "idempotency_key": key,
                }
            ),
            encoding="utf-8",
        )
        return path

    def record(self, work_item, outcome, at, *extra):
        return self.run_cli(
            "record-automation-outcome",
            "--catalog", str(CATALOG),
            "--state", str(self.directory / "state.json"),
            "--work-item", str(work_item),
            "--outcome", outcome,
            "--at", at,
            *extra,
        )

    def test_archiving_a_pdf_does_not_touch_monday(self):
        pdf = self.directory / "TAG.pdf"
        pdf.write_bytes(b"%PDF-1.7\nTAG")

        run = self.run_cli(
            "archive-document",
            "--archive-root", str(self.directory / "archive"),
            "--tenant-id", TENANT_ID,
            "--case-id", "case-100",
            "--case-name", "Commessa Prova",
            "--monday-item-id", "2001",
            "--monday-column-id", "file_tag",
            "--document-type", "tag_grid_connection_application",
            "--pdf", str(pdf),
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["monday_status"], "pending")
        self.assertFalse(result["monday_uploaded"])
        self.assertNotIn("Commessa Prova", run.stdout)

    def test_publishing_requires_the_upload_switch(self):
        pdf = self.directory / "TAG.pdf"
        pdf.write_bytes(b"%PDF-1.7\nTAG")

        run = self.run_cli(
            "archive-document",
            "--archive-root", str(self.directory / "archive"),
            "--tenant-id", TENANT_ID,
            "--case-id", "case-100",
            "--case-name", "Commessa Prova",
            "--monday-item-id", "2001",
            "--monday-column-id", "file_tag",
            "--document-type", "tag_grid_connection_application",
            "--pdf", str(pdf),
            "--publish-to-monday",
        )

        self.assertEqual(run.returncode, 2)
        self.assertIn("WORKFLOWOS_MONDAY_UPLOAD_ENABLED", run.stderr)

    def test_recorded_success_unblocks_the_dependent_automation(self):
        events = self.directory / "events.json"
        events.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "collector_id": "workflowos.event",
                            "event_type": "technical_review_changes_required",
                            "case_id": "case-100",
                            "event_ref_sha256": "2" * 64,
                            "occurred_at": "2026-08-04T05:10:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        state = self.directory / "state.json"

        recorded = self.record(
            self.work_item("assignment-intake", "key-intake"),
            "succeeded",
            "2026-08-04T07:20:00+02:00",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)["status"], "completed")
        self.assertTrue(state.is_file())

        plan = self.run_cli(
            "plan-automation-events",
            "--catalog", str(CATALOG),
            "--events", str(events),
            "--state", str(state),
            "--at", "2026-08-04T07:25:00+02:00",
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        planned = json.loads(plan.stdout)
        self.assertEqual(
            [item["automation_id"] for item in planned["ready"]],
            ["technical-work-plan"],
        )
        self.assertEqual(planned["blocked"], [])

    def test_repeated_failures_escalate_to_a_dead_letter(self):
        item = self.work_item("technical-work-plan", "key-twp")
        statuses = []
        for minute, at in enumerate(
            (
                "2026-08-04T07:31:00+02:00",
                "2026-08-04T07:32:00+02:00",
                "2026-08-04T07:40:00+02:00",
            )
        ):
            del minute
            run = self.record(
                item, "failed", at, "--error-code", "provider_error"
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            statuses.append(json.loads(run.stdout)["status"])

        self.assertEqual(
            statuses, ["retry_scheduled", "retry_scheduled", "dead_letter"]
        )

        brief = self.run_cli(
            "daily-operations-brief",
            "--catalog", str(CATALOG),
            "--state", str(self.directory / "state.json"),
            "--at", "2026-08-04T05:00:00Z",
        )
        self.assertEqual(brief.returncode, 0, brief.stderr)
        report = json.loads(brief.stdout)
        self.assertEqual(report["status"], "attention_required")
        self.assertEqual(report["totals"]["failed"], 1)
        self.assertEqual(
            report["failed"][0]["automation_id"], "technical-work-plan"
        )

    def test_failed_outcome_requires_an_error_code(self):
        run = self.record(
            self.work_item("assignment-intake", "key-intake"),
            "failed",
            "2026-08-04T07:20:00+02:00",
        )

        self.assertEqual(run.returncode, 2)
        self.assertIn("error_code", run.stderr)
        self.assertFalse((self.directory / "state.json").is_file())


if __name__ == "__main__":
    unittest.main()
