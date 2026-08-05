from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from workflowos.core import PublicationRefused, WorkflowError
from workflowos.document_archive import MeraviqaDocumentArchive, SbPdfDocument
from workflowos.monday_uploads import (
    MONDAY_FILE_API_URL,
    MondayGuardedUploader,
    MondayUploadConfig,
    _add_file_mutation,
    _multipart_upload_body,
)


PDF = b"%PDF-1.7\nTAG document"
COLUMNS = {
    "file_tag": "tag_grid_connection_application",
    "file_ia": "installation_notice_ia",
    "file_schema": "single_line_diagram",
    "file_sina": "safety_report_rasi_sina",
}


class MondayUploadConfigTests(unittest.TestCase):
    def environment(self, **changes):
        values = {
            "MONDAY_API_TOKEN": "token-value",
            "WORKFLOWOS_TENANT_ID": "tenant-test-001",
            "WORKFLOWOS_MONDAY_BOARD_ID": "1001",
            "WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS": json.dumps(COLUMNS),
            "WORKFLOWOS_MONDAY_UPLOAD_ENABLED": "true",
        }
        values.update(changes)
        return values

    def test_upload_requires_an_explicit_enable_switch(self):
        with self.assertRaisesRegex(WorkflowError, "WORKFLOWOS_MONDAY_UPLOAD_ENABLED"):
            MondayUploadConfig.from_environment(
                self.environment(WORKFLOWOS_MONDAY_UPLOAD_ENABLED="false")
            )

    def test_binding_check_does_not_need_the_enable_switch(self):
        config = MondayUploadConfig.from_environment(
            self.environment(WORKFLOWOS_MONDAY_UPLOAD_ENABLED=""),
            require_live_enabled=False,
        )
        self.assertEqual(config.expected_board_id, "1001")

    def test_endpoints_are_fixed(self):
        with self.assertRaisesRegex(WorkflowError, "api.monday.com/v2/file"):
            MondayUploadConfig(
                api_token="token-value",
                tenant_id="tenant-test-001",
                expected_board_id="1001",
                document_columns=COLUMNS,
                file_api_url="https://attacker.example/v2/file",
            )

    def test_token_is_not_repeated_in_the_representation(self):
        config = MondayUploadConfig.from_environment(self.environment())
        self.assertNotIn("token-value", repr(config))


class UploaderFixtures:
    """Shared uploader configuration and injected transports."""

    def config(self, **changes):
        values = {
            "api_token": "token-value",
            "tenant_id": "tenant-test-001",
            "expected_board_id": "1001",
            "document_columns": COLUMNS,
        }
        values.update(changes)
        return MondayUploadConfig(**values)

    def uploader(self, *, board_id="1001", uploads=None, asset=None):
        calls = {"reads": [], "uploads": uploads if uploads is not None else []}

        def reader(query, variables, token):
            calls["reads"].append((query, variables, token))
            return {"items": [{"id": variables["itemIds"][0], "board": {"id": board_id}}]}

        def uploader_transport(mutation, item_id, column_id, content, filename):
            calls["uploads"].append((item_id, column_id, filename, content))
            return {
                "add_file_to_column": asset
                if asset is not None
                else {"id": "77", "name": filename, "file_size": len(content)}
            }

        return (
            MondayGuardedUploader(
                self.config(),
                graphql_reader=reader,
                file_uploader=uploader_transport,
            ),
            calls,
        )

class MondayGuardedUploaderTests(UploaderFixtures, unittest.TestCase):
    def test_upload_confirms_item_column_and_transmitted_checksum(self):
        uploader, calls = self.uploader()
        confirmation = uploader.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)

        self.assertEqual(confirmation["item_id"], "2001")
        self.assertEqual(confirmation["column_id"], "file_tag")
        self.assertEqual(confirmation["board_id"], "1001")
        self.assertEqual(confirmation["asset_id"], "77")
        self.assertEqual(
            confirmation["content_sha256"], hashlib.sha256(PDF).hexdigest()
        )
        self.assertEqual(len(calls["uploads"]), 1)

    def test_item_on_another_board_is_never_uploaded(self):
        uploader, calls = self.uploader(board_id="9999")

        with self.assertRaisesRegex(WorkflowError, "another board"):
            uploader.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)
        self.assertEqual(calls["uploads"], [])

    def test_unconfigured_column_is_rejected_before_any_request(self):
        uploader, calls = self.uploader()

        with self.assertRaisesRegex(WorkflowError, "not configured for documents"):
            uploader.publish_pdf("2001", "file_other", "TAG.pdf", PDF)
        self.assertEqual(calls["reads"], [])
        self.assertEqual(calls["uploads"], [])

    def test_non_pdf_and_oversized_content_are_rejected(self):
        uploader, calls = self.uploader()

        with self.assertRaisesRegex(WorkflowError, "must be a PDF"):
            uploader.publish_pdf("2001", "file_tag", "TAG.pdf", b"not-a-pdf")
        oversized = MondayGuardedUploader(
            self.config(max_file_bytes=16),
            graphql_reader=lambda *args: self.fail("must not read"),
            file_uploader=lambda *args: self.fail("must not upload"),
        )
        with self.assertRaisesRegex(WorkflowError, "size limit"):
            oversized.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)
        self.assertEqual(calls["uploads"], [])

    def test_unsafe_filename_is_rejected(self):
        uploader, calls = self.uploader()

        for filename in ('TAG";x.pdf', "../TAG.pdf", "TAG.pdf\r\nX: y", "TAG.exe"):
            with self.assertRaisesRegex(WorkflowError, "simple PDF name"):
                uploader.publish_pdf("2001", "file_tag", filename, PDF)
        self.assertEqual(calls["uploads"], [])

    def test_size_mismatch_reported_by_monday_is_an_error(self):
        uploader, _ = self.uploader(
            asset={"id": "77", "name": "TAG.pdf", "file_size": len(PDF) - 1}
        )

        with self.assertRaisesRegex(WorkflowError, "different number of bytes"):
            uploader.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)

    def test_missing_asset_id_is_not_treated_as_a_successful_upload(self):
        uploader, _ = self.uploader(asset={"name": "TAG.pdf"})

        with self.assertRaises(WorkflowError):
            uploader.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)

    def test_verify_binding_never_uploads(self):
        uploader, calls = self.uploader()
        result = uploader.verify_binding("2001")

        self.assertEqual(result["board_id"], "1001")
        self.assertFalse(result["uploaded"])
        self.assertEqual(calls["uploads"], [])

    def test_publisher_plugs_into_the_document_archive(self):
        uploader, calls = self.uploader()
        document = SbPdfDocument(
            tenant_id="tenant-test-001",
            case_id="case-001",
            case_name="Example Project",
            monday_item_id="2001",
            monday_column_id="file_tag",
            document_type="tag_grid_connection_application",
            filename="TAG.pdf",
            content=PDF,
            content_sha256=hashlib.sha256(PDF).hexdigest(),
        )

        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(
                directory, monday_publisher=uploader.publisher()
            ).archive(document)

        self.assertEqual(result["monday_status"], "uploaded")
        self.assertEqual(calls["uploads"], [("2001", "file_tag", "TAG.pdf", PDF)])


class MondayUploadRefusalTests(UploaderFixtures, unittest.TestCase):
    def assert_refused(self, uploader, *arguments):
        with self.assertRaises(PublicationRefused):
            uploader.publish_pdf(*arguments)

    def test_pre_transmission_failures_are_certain_non_transmissions(self):
        uploader, calls = self.uploader()

        self.assert_refused(uploader, "2001", "file_other", "TAG.pdf", PDF)
        self.assert_refused(uploader, "2001", "file_tag", "TAG.exe", PDF)
        self.assert_refused(uploader, "2001", "file_tag", "TAG.pdf", b"not-a-pdf")
        self.assert_refused(uploader, "not-numeric", "file_tag", "TAG.pdf", PDF)
        self.assertEqual(calls["uploads"], [])

    def test_cross_board_item_is_a_certain_non_transmission(self):
        uploader, calls = self.uploader(board_id="9999")

        self.assert_refused(uploader, "2001", "file_tag", "TAG.pdf", PDF)
        self.assertEqual(calls["uploads"], [])

    def test_binding_query_failure_is_a_certain_non_transmission(self):
        def failing_reader(query, variables, token):
            raise OSError("binding query failed")

        uploader = MondayGuardedUploader(
            self.config(),
            graphql_reader=failing_reader,
            file_uploader=lambda *args: self.fail("must not upload"),
        )

        self.assert_refused(uploader, "2001", "file_tag", "TAG.pdf", PDF)

    def test_transport_failure_is_never_reported_as_a_refusal(self):
        def failing_upload(*args):
            raise OSError("connection lost after the request started")

        uploader = MondayGuardedUploader(
            self.config(),
            graphql_reader=lambda query, variables, token: {
                "items": [{"id": "2001", "board": {"id": "1001"}}]
            },
            file_uploader=failing_upload,
        )

        with self.assertRaises(OSError):
            uploader.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)

    def test_unusable_response_after_upload_is_never_a_refusal(self):
        uploader, _ = self.uploader(asset={"name": "TAG.pdf"})

        with self.assertRaises(WorkflowError) as caught:
            uploader.publish_pdf("2001", "file_tag", "TAG.pdf", PDF)
        self.assertNotIsInstance(caught.exception, PublicationRefused)

    def test_refusal_leaves_the_archive_entry_publishable(self):
        uploader, calls = self.uploader(board_id="9999")
        document = SbPdfDocument(
            tenant_id="tenant-test-001",
            case_id="case-001",
            case_name="Example Project",
            monday_item_id="2001",
            monday_column_id="file_tag",
            document_type="tag_grid_connection_application",
            filename="TAG.pdf",
            content=PDF,
            content_sha256=hashlib.sha256(PDF).hexdigest(),
        )

        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(
                directory, monday_publisher=uploader.publisher()
            ).archive(document)

        self.assertEqual(result["monday_status"], "pending")
        self.assertEqual(result["upload_attempts"], 0)
        self.assertEqual(result["refused_attempts"], 1)
        self.assertEqual(calls["uploads"], [])


class MondayMultipartBodyTests(unittest.TestCase):
    def build(self, content=PDF, filename="TAG.pdf"):
        return _multipart_upload_body(
            "mutation X { add_file_to_column { id } }", content, filename
        )

    def test_body_carries_the_map_and_the_untouched_pdf(self):
        body, content_type = self.build()
        boundary = content_type.split("boundary=")[1]

        self.assertIn(b'{"image":"variables.file"}', body)
        self.assertIn(b'name="image"; filename="TAG.pdf"', body)
        self.assertIn(PDF, body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode("utf-8")))

    def test_variables_declare_the_file_placeholder(self):
        body, _ = self.build()
        self.assertIn(json.dumps({"file": None}).encode("utf-8"), body)

    def test_boundary_never_collides_with_the_pdf_content(self):
        body, content_type = self.build(content=b"%PDF-1.7\nMERAVIQA")
        boundary = content_type.split("boundary=")[1]
        self.assertEqual(body.count(f"--{boundary}".encode("utf-8")), 5)


class MondayMutationTests(unittest.TestCase):
    def test_mutation_targets_the_exact_item_and_column(self):
        mutation = _add_file_mutation("2001", "file_tag")

        self.assertTrue(mutation.startswith("mutation "))
        self.assertIn("item_id: 2001", mutation)
        self.assertIn('column_id: "file_tag"', mutation)
        self.assertIn("$file: File!", mutation)

    def test_unsafe_target_is_never_inlined(self):
        for item_id, column_id in (
            ('2001) { id } malicious(x: "', "file_tag"),
            ("2001", 'file_tag") { id } malicious(y: "'),
            ("not-numeric", "file_tag"),
        ):
            with self.assertRaisesRegex(WorkflowError, "not safe to inline"):
                _add_file_mutation(item_id, column_id)


class MondayUploadEndpointTests(unittest.TestCase):
    def test_file_endpoint_is_the_documented_monday_url(self):
        self.assertEqual(MONDAY_FILE_API_URL, "https://api.monday.com/v2/file")


if __name__ == "__main__":
    unittest.main()
