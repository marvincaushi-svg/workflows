from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflowos.state_store import load_automation_state, save_automation_state


ROOT = Path(__file__).resolve().parents[1]


class EmailOutboxCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_path = Path(self.temporary.name) / "automation-state.json"
        self.idempotency_key = "a" * 64
        request = {
            "idempotency_key": self.idempotency_key,
            "recipient_email": "recipient@example.invalid",
            "attachments": [
                {
                    "source_locator": "monday-asset://board/item/column/asset",
                    "sha256": "b" * 64,
                }
            ],
        }
        request_sha256 = hashlib.sha256(
            json.dumps(
                request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        save_automation_state(
            self.state_path,
            {
                "delivery_outbox": {
                    self.idempotency_key: {
                        "status": "delivery_in_doubt",
                        "handoff": "accepted_practices",
                        "request_sha256": request_sha256,
                        "email_request": request,
                    }
                }
            },
        )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "workflowos.cli", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_inspection_is_read_only_and_hides_email_contents(self):
        before = self.state_path.read_bytes()
        run = self.run_cli(
            "inspect-email-outbox",
            "--state",
            str(self.state_path),
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertEqual(
            report["entries"][0]["action_required"], "reconcile_delivery"
        )
        self.assertNotIn("recipient@example.invalid", run.stdout)
        self.assertNotIn("email_request", run.stdout)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_confirmed_non_delivery_authorizes_without_sending(self):
        run = self.run_cli(
            "reconcile-email-delivery",
            "--state",
            str(self.state_path),
            "--idempotency-key",
            self.idempotency_key,
            "--outcome",
            "confirmed-not-sent",
            "--checked-at",
            "2026-08-05T01:30:00+02:00",
            "--checked-by-ref",
            "operator-ref-001",
            "--evidence-ref-sha256",
            "c" * 64,
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "retry_authorized")
        state = load_automation_state(self.state_path)
        entry = state["delivery_outbox"][self.idempotency_key]
        self.assertEqual(entry["status"], "retry_authorized")
        self.assertEqual(entry["reconciliation"]["outcome"], "confirmed_not_sent")

    def test_confirmed_delivery_requires_message_evidence(self):
        before = self.state_path.read_bytes()
        run = self.run_cli(
            "reconcile-email-delivery",
            "--state",
            str(self.state_path),
            "--idempotency-key",
            self.idempotency_key,
            "--outcome",
            "confirmed-sent",
            "--checked-at",
            "2026-08-05T01:30:00+02:00",
            "--checked-by-ref",
            "operator-ref-001",
            "--evidence-ref-sha256",
            "c" * 64,
        )

        self.assertEqual(run.returncode, 2)
        self.assertIn("message_ref_sha256", run.stderr)
        self.assertEqual(self.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
