from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from workflowos.automation import initialize_automation_state
from workflowos.core import WorkflowError, load_document
from workflowos.gmail_email import GmailSmtpConfig, GmailSmtpSender
from workflowos.monday_assets import (
    DownloadedAsset,
    MondayReadOnlyCollector,
    MondayReadOnlyConfig,
)
from workflowos.monday_pipeline import DurableMondayEmailPipeline, MondayFileChange
from workflowos.state_store import load_automation_state, save_automation_state
from workflowos.technical_review import create_remediation_plan, evaluate_technical_review


ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = ROOT / "examples" / "pilot" / "technical-review.sanitized.json"
BOARD_ID = "5090983792"
ITEM_ID = "3050000000"
COLUMNS = {
    "file_tag": "tag_grid_connection_application",
    "file_ia": "installation_notice_ia",
    "file_schema": "single_line_diagram",
    "file_sina": "safety_report_rasi_sina",
}


class FakeSmtp:
    sent_messages = []

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def ehlo(self):
        return None

    def starttls(self, *, context):
        return None

    def login(self, username, password):
        return None

    def send_message(self, message, *, from_addr, to_addrs):
        self.sent_messages.append((message, from_addr, to_addrs))
        return {}


class DurableMondayEmailPipelineTests(unittest.TestCase):
    def setUp(self):
        FakeSmtp.sent_messages = []
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
        review = load_document(REVIEW_PATH)
        plan = create_remediation_plan(
            evaluate_technical_review(review), "pilot-pv-001", "2026-08-03T13:00:00Z"
        )
        monday_case = {
            "source": "monday",
            "board_id": BOARD_ID,
            "item_id": ITEM_ID,
            "case_id": "pilot-pv-001",
            "identity": self.identity,
            "sb_email_recipient": {
                "name": "Referente SB",
                "email": "verified-sb@example.invalid",
                "organization_role": "sb_energetica",
                "resolution_status": "verified",
            },
        }
        self.initial_state = initialize_automation_state(plan, monday_case)
        self.contents = {
            "file_tag": b"%PDF-1.7\nTAG",
            "file_ia": b"%PDF-1.7\nIA",
            "file_schema": b"%PDF-1.7\nSCHEMA",
            "file_sina": b"%PDF-1.7\nSINA",
        }
        self.graphql_calls = []
        self.download_calls = []

        def graphql(query, variables, token):
            self.graphql_calls.append((query, copy.deepcopy(variables), token))
            column = variables["columnIds"][0]
            asset_id = str(700000000 + list(COLUMNS).index(column))
            content = self.contents[column]
            url = (
                "https://prod-euc1-files-monday-com.s3.eu-central-1.amazonaws.com/"
                f"sanitized/{asset_id}/{column}.pdf?signature=redacted"
            )
            return {
                "items": [{
                    "id": ITEM_ID,
                    "board": {"id": BOARD_ID},
                    "column_values": [{
                        "id": column,
                        "type": "file",
                        "files": [{
                            "__typename": "FileAssetValue",
                            "asset_id": asset_id,
                            "asset": {
                                "id": asset_id,
                                "name": f"{column}.pdf",
                                "file_extension": ".pdf",
                                "file_size": len(content),
                                "public_url": url,
                            },
                        }],
                    }],
                }]
            }

        def download(url, max_bytes):
            self.download_calls.append((url, max_bytes))
            column = next(column for column in COLUMNS if f"/{column}.pdf" in url)
            return DownloadedAsset(self.contents[column], url, "application/pdf")

        self.collector = MondayReadOnlyCollector(
            MondayReadOnlyConfig(
                api_token="test-token",
                expected_board_id=BOARD_ID,
                document_columns=COLUMNS,
            ),
            graphql_transport=graphql,
            download_transport=download,
        )
        self.automation_config = {
            "expected_board_id": BOARD_ID,
            "mode": "live",
            "allow_external_email": True,
            "document_columns": COLUMNS,
        }
        gmail = GmailSmtpSender(
            GmailSmtpConfig(password="test-password"),
            self.collector.load_attachment,
            smtp_factory=FakeSmtp,
        )
        self.pipeline = DurableMondayEmailPipeline(
            self.collector, self.automation_config, email_sender=gmail
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "state.json"
        save_automation_state(self.state_path, self.initial_state)

    def tearDown(self):
        self.tempdir.cleanup()

    def verification(self, column):
        return {
            "case_id": "pilot-pv-001",
            "document_type": COLUMNS[column],
            "attachment_ref_sha256": hashlib.sha256(
                self.contents[column]
            ).hexdigest(),
            "content_verified": True,
            "latest_version": True,
            "identity_extraction_verified": True,
            "professional_signoff_verified": True,
            "document_identity": copy.deepcopy(self.identity),
        }

    def test_full_monday_to_verified_gmail_path_sends_one_three_pdf_message(self):
        outcome = None
        for index, column in enumerate(("file_tag", "file_ia", "file_schema")):
            outcome = self.pipeline.process(
                self.state_path,
                MondayFileChange(
                    item_id=ITEM_ID,
                    case_id="pilot-pv-001",
                    column_id=column,
                    grid_operator_practices_accepted=index == 2,
                ),
                self.verification(column),
            )

        self.assertEqual(outcome["result"]["status"], "completed")
        self.assertEqual(len(FakeSmtp.sent_messages), 1)
        message, sender, recipients = FakeSmtp.sent_messages[0]
        self.assertEqual(sender, "marvin.caushi@gmail.com")
        self.assertEqual(recipients, ["verified-sb@example.invalid"])
        self.assertEqual(len(list(message.iter_attachments())), 3)
        persisted = load_automation_state(self.state_path)
        self.assertEqual(
            persisted["handoffs"]["accepted_practices"]["email_delivery"]["status"],
            "sent",
        )
        self.assertEqual(
            list(persisted["delivery_outbox"].values())[0]["status"], "sent"
        )

    def test_collector_and_runtime_binding_must_match(self):
        config = copy.deepcopy(self.automation_config)
        config["document_columns"] = dict(COLUMNS)
        config["document_columns"]["file_tag"] = "installation_notice_ia"
        with self.assertRaisesRegex(WorkflowError, "columns do not match"):
            DurableMondayEmailPipeline(self.collector, config)

    def test_invalid_runtime_mode_is_rejected_before_monday_access(self):
        config = copy.deepcopy(self.automation_config)
        config["mode"] = "unsafe"
        with self.assertRaisesRegex(WorkflowError, "test or live"):
            DurableMondayEmailPipeline(self.collector, config)
        self.assertEqual(self.graphql_calls, [])

    def test_uncertain_delivery_is_persisted_and_never_retried_automatically(self):
        calls = []

        def uncertain_sender(request):
            calls.append(request)
            raise OSError("connection dropped after DATA")

        pipeline = DurableMondayEmailPipeline(
            self.collector,
            self.automation_config,
            email_sender=uncertain_sender,
        )
        for index, column in enumerate(("file_tag", "file_ia")):
            pipeline.process(
                self.state_path,
                MondayFileChange(
                    item_id=ITEM_ID,
                    case_id="pilot-pv-001",
                    column_id=column,
                    grid_operator_practices_accepted=False,
                ),
                self.verification(column),
            )

        with self.assertRaisesRegex(WorkflowError, "manual reconciliation"):
            pipeline.process(
                self.state_path,
                MondayFileChange(
                    item_id=ITEM_ID,
                    case_id="pilot-pv-001",
                    column_id="file_schema",
                    grid_operator_practices_accepted=True,
                ),
                self.verification("file_schema"),
            )

        before_retry_calls = len(self.graphql_calls)
        blocked = pipeline.process(
            self.state_path,
            MondayFileChange(
                item_id=ITEM_ID,
                case_id="pilot-pv-001",
                column_id="file_schema",
                grid_operator_practices_accepted=True,
            ),
            self.verification("file_schema"),
        )
        self.assertEqual(
            blocked["result"]["status"], "blocked_delivery_reconciliation"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.graphql_calls), before_retry_calls)
        persisted = load_automation_state(self.state_path)
        self.assertEqual(
            list(persisted["delivery_outbox"].values())[0]["status"],
            "delivery_in_doubt",
        )


if __name__ == "__main__":
    unittest.main()
