from __future__ import annotations

import copy
import hashlib
import json
import unittest

from workflowos.core import WorkflowError
from workflowos.monday_assets import (
    DEFAULT_ASSET_HOSTS,
    DownloadedAsset,
    MondayReadOnlyCollector,
    MondayReadOnlyConfig,
)


BOARD_ID = "5090000000"
ITEM_ID = "3050000000"
COLUMN_ID = "file_tag"
ASSET_ID = "250000001"
PDF = b"%PDF-1.7\nverified-test-pdf"
PUBLIC_URL = (
    "https://prod-euc1-files-monday-com.s3.eu-central-1.amazonaws.com/"
    "sanitized/resources/250000001/TAG.pdf?temporary-signature=redacted"
)


class MondayReadOnlyCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MondayReadOnlyConfig(
            api_token="test-token",
            expected_board_id=BOARD_ID,
            document_columns={
                COLUMN_ID: "tag_grid_connection_application",
                "file_ia": "installation_notice_ia",
                "file_schema": "single_line_diagram",
                "file_sina": "safety_report_rasi_sina",
            },
        )
        self.graphql_calls = []
        self.download_calls = []
        self.files = [self.asset_file()]
        self.board_id = BOARD_ID
        self.content = PDF

    def asset_file(self, asset_id=ASSET_ID, filename="TAG.pdf"):
        return {
            "__typename": "FileAssetValue",
            "asset_id": asset_id,
            "asset_name": filename,
            "asset": {
                "id": asset_id,
                "name": filename,
                "file_extension": ".pdf",
                "file_size": len(PDF),
                "public_url": PUBLIC_URL.replace(ASSET_ID, asset_id),
            },
        }

    def graphql(self, query, variables, token):
        self.graphql_calls.append((query, copy.deepcopy(variables), token))
        return {
            "items": [
                {
                    "id": ITEM_ID,
                    "board": {"id": self.board_id},
                    "column_values": [
                        {"id": COLUMN_ID, "type": "file", "files": self.files}
                    ],
                }
            ]
        }

    def download(self, url, max_bytes):
        self.download_calls.append((url, max_bytes))
        return DownloadedAsset(self.content, url, "application/pdf")

    def collector(self):
        return MondayReadOnlyCollector(
            self.config,
            graphql_transport=self.graphql,
            download_transport=self.download,
        )

    def test_collects_one_exact_pdf_into_a_sanitized_event(self):
        event = self.collector().collect_file_event(
            item_id=ITEM_ID,
            case_id="pilot-case-001",
            column_id=COLUMN_ID,
        )

        self.assertEqual(event["source"], "monday")
        self.assertEqual(event["board_id"], BOARD_ID)
        self.assertEqual(event["item_id"], ITEM_ID)
        self.assertEqual(event["column_id"], COLUMN_ID)
        self.assertEqual(
            event["attachment_ref_sha256"], hashlib.sha256(PDF).hexdigest()
        )
        self.assertRegex(event["event_ref_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            event["asset_locator"],
            f"monday-asset://{BOARD_ID}/{ITEM_ID}/{COLUMN_ID}/{ASSET_ID}",
        )
        self.assertNotIn(PUBLIC_URL, json.dumps(event))
        query, variables, token = self.graphql_calls[0]
        self.assertTrue(query.startswith("query MeraviqaReadOnlyItemFiles"))
        self.assertNotIn("mutation", query.casefold())
        self.assertEqual(variables, {"itemIds": [ITEM_ID], "columnIds": [COLUMN_ID]})
        self.assertEqual(token, "test-token")

    def test_loader_reresolves_locator_instead_of_persisting_temporary_url(self):
        collector = self.collector()
        event = collector.collect_file_event(
            item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
        )
        attachment = collector.load_attachment(event["asset_locator"])

        self.assertEqual(attachment.filename, "TAG.pdf")
        self.assertEqual(attachment.content, PDF)
        self.assertEqual(len(self.graphql_calls), 2)
        self.assertEqual(len(self.download_calls), 2)

    def test_multiple_files_require_the_exact_asset_id(self):
        second_id = "250000002"
        self.files.append(self.asset_file(second_id, "TAG-approved.pdf"))
        with self.assertRaisesRegex(WorkflowError, "exactly one"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
            )

        event = self.collector().collect_file_event(
            item_id=ITEM_ID,
            case_id="case-1",
            column_id=COLUMN_ID,
            asset_id=second_id,
        )
        self.assertTrue(event["asset_locator"].endswith(f"/{second_id}"))

    def test_item_from_another_board_is_rejected_before_download(self):
        self.board_id = "5099999999"
        with self.assertRaisesRegex(WorkflowError, "different board"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
            )
        self.assertEqual(self.download_calls, [])

    def test_unknown_column_is_rejected_before_api_request(self):
        with self.assertRaisesRegex(WorkflowError, "not configured"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id="file_other"
            )
        self.assertEqual(self.graphql_calls, [])

    def test_unapproved_asset_host_is_rejected_before_download(self):
        self.files[0]["asset"]["public_url"] = "https://example.com/TAG.pdf"
        with self.assertRaisesRegex(WorkflowError, "host is not approved"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
            )
        self.assertEqual(self.download_calls, [])

    def test_non_pdf_content_is_rejected(self):
        self.content = b"not-a-pdf-but-same-size-xxxxx"[: len(PDF)]
        self.assertEqual(len(self.content), len(PDF))
        with self.assertRaisesRegex(WorkflowError, "content is not a PDF"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
            )

    def test_changed_download_size_is_rejected(self):
        self.content = PDF + b"changed"
        with self.assertRaisesRegex(WorkflowError, "size changed"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
            )

    def test_linked_file_is_not_accepted_as_an_uploaded_asset(self):
        self.files = [
            {
                "__typename": "FileLinkValue",
                "linked_file_id": "link-1",
                "linked_name": "TAG.pdf",
                "linked_url": "https://example.com/TAG.pdf",
            }
        ]
        with self.assertRaisesRegex(WorkflowError, "exactly one uploaded asset"):
            self.collector().collect_file_event(
                item_id=ITEM_ID, case_id="case-1", column_id=COLUMN_ID
            )

    def test_environment_configuration_contains_no_tenant_defaults(self):
        columns = dict(self.config.document_columns)
        config = MondayReadOnlyConfig.from_environment(
            {
                "MONDAY_API_TOKEN": "secret",
                "WORKFLOWOS_MONDAY_BOARD_ID": BOARD_ID,
                "WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS": json.dumps(columns),
            }
        )
        self.assertEqual(config.document_columns, columns)
        self.assertEqual(config.allowed_asset_hosts, DEFAULT_ASSET_HOSTS)


if __name__ == "__main__":
    unittest.main()
