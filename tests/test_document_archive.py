from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from workflowos.core import WorkflowError
from workflowos.document_archive import (
    MeraviqaDocumentArchive,
    MondayArchiveReconciliation,
    SbPdfDocument,
)


PDF = b"%PDF-1.7\nSB document"


class MeraviqaDocumentArchiveTests(unittest.TestCase):
    def document(self, **changes):
        values = {
            "tenant_id": "tenant-test-001", "case_id": "case-001",
            "case_name": "Example Project", "monday_item_id": "2001",
            "monday_column_id": "file_tag",
            "document_type": "tag_grid_connection_application",
            "filename": "TAG.pdf", "content": PDF,
            "content_sha256": hashlib.sha256(PDF).hexdigest(),
        }
        values.update(changes)
        return SbPdfDocument(**values)

    def evidence(self, document, outcome):
        return MondayArchiveReconciliation(
            tenant_id=document.tenant_id,
            case_id=document.case_id,
            content_sha256=document.content_sha256,
            outcome=outcome,
            checked_at="2026-08-05T09:00:00+02:00",
            checked_by_ref="operator-ref-001",
            evidence_ref_sha256="e" * 64,
        )

    def test_archives_in_case_named_folder_then_uploads_to_monday(self):
        calls = []

        def publish(item_id, column_id, filename, content):
            calls.append((item_id, column_id, filename, content))
            return {"item_id": item_id, "column_id": column_id, "content_sha256": hashlib.sha256(content).hexdigest()}

        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(directory, monday_publisher=publish).archive(self.document())
            case = Path(directory) / "tenant-test-001" / "Example Project"
            self.assertEqual((case / "TAG.pdf").read_bytes(), PDF)
            self.assertEqual(result["monday_status"], "uploaded")
            self.assertEqual(calls, [("2001", "file_tag", "TAG.pdf", PDF)])
            manifest = json.loads((case / ".meraviqa-documents.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["documents"][result["content_sha256"]]["monday_status"], "uploaded")

    def test_duplicate_pdf_is_not_written_or_uploaded_twice(self):
        calls = []

        def publish(item_id, column_id, filename, content):
            calls.append(filename)
            return {"item_id": item_id, "column_id": column_id, "content_sha256": hashlib.sha256(content).hexdigest()}

        with tempfile.TemporaryDirectory() as directory:
            archive = MeraviqaDocumentArchive(directory, monday_publisher=publish)
            first = archive.archive(self.document())
            second = archive.archive(self.document(filename="copy.pdf"))
            self.assertEqual(first["status"], "archived")
            self.assertEqual(second["status"], "already_archived")
            self.assertEqual(calls, ["TAG.pdf"])

    def test_monday_failure_keeps_pdf_pending_in_meraviqa(self):
        def unavailable(*args):
            raise OSError("provider unavailable")

        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(directory, monday_publisher=unavailable).archive(self.document())
            self.assertEqual(result["monday_status"], "upload_in_doubt")
            self.assertTrue((Path(directory) / "tenant-test-001" / "Example Project" / "TAG.pdf").is_file())

    def test_known_pending_pdf_uploads_when_publisher_becomes_available(self):
        calls = []

        def publish(item_id, column_id, filename, content):
            calls.append((item_id, column_id, filename))
            return {
                "item_id": item_id,
                "column_id": column_id,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            first = MeraviqaDocumentArchive(directory).archive(document)
            second = MeraviqaDocumentArchive(
                directory, monday_publisher=publish
            ).archive(document)

            self.assertEqual(first["monday_status"], "pending")
            self.assertEqual(second["monday_status"], "uploaded")
            self.assertEqual(second["upload_attempts"], 1)
            self.assertEqual(calls, [("2001", "file_tag", "TAG.pdf")])

    def test_uncertain_upload_is_never_retried_automatically(self):
        calls = []

        def uncertain(*args):
            calls.append(args)
            raise OSError("connection lost after upload")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(
                directory, monday_publisher=uncertain
            )
            first = archive.archive(document)
            second = archive.archive(document)

            self.assertEqual(first["monday_status"], "upload_in_doubt")
            self.assertEqual(second["monday_status"], "upload_in_doubt")
            self.assertEqual(len(calls), 1)

    def test_confirmed_non_upload_allows_one_explicit_retry(self):
        attempts = []

        def publisher(item_id, column_id, filename, content):
            attempts.append(filename)
            if len(attempts) == 1:
                raise OSError("connection lost")
            return {
                "item_id": item_id,
                "column_id": column_id,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(
                directory, monday_publisher=publisher
            )
            archive.archive(document)
            reconciled = archive.reconcile_monday(
                document, self.evidence(document, "confirmed_not_uploaded")
            )
            retried = archive.retry_authorized_monday(document)

            self.assertEqual(
                reconciled["reconciliation_status"], "retry_authorized"
            )
            self.assertEqual(retried["monday_status"], "uploaded")
            self.assertEqual(retried["upload_attempts"], 2)
            self.assertEqual(attempts, ["TAG.pdf", "TAG.pdf"])
            with self.assertRaisesRegex(WorkflowError, "not been authorized"):
                archive.retry_authorized_monday(document)

    def test_confirmed_upload_reconciles_without_publishing_again(self):
        calls = []

        def uncertain(*args):
            calls.append(args)
            raise OSError("confirmation lost")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(
                directory, monday_publisher=uncertain
            )
            archive.archive(document)
            reconciled = archive.reconcile_monday(
                document, self.evidence(document, "confirmed_uploaded")
            )
            repeated = archive.archive(document)

            self.assertEqual(
                reconciled["reconciliation_status"], "uploaded_reconciled"
            )
            self.assertEqual(repeated["monday_status"], "uploaded")
            self.assertEqual(len(calls), 1)

    def test_manifest_tampering_is_rejected_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            MeraviqaDocumentArchive(directory).archive(document)
            manifest_path = (
                Path(directory)
                / "tenant-test-001"
                / "Example Project"
                / ".meraviqa-documents.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["documents"][document.content_sha256][
                "monday_item_id"
            ] = "9999"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(WorkflowError, "checksum does not match"):
                MeraviqaDocumentArchive(
                    directory,
                    monday_publisher=lambda *args: self.fail("must not publish"),
                ).archive(document)

    def test_legacy_pending_manifest_requires_reconciliation(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(directory)
            archive.archive(document)
            manifest_path = (
                Path(directory)
                / "tenant-test-001"
                / "Example Project"
                / ".meraviqa-documents.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "1.0"
            manifest.pop("payload_sha256")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = MeraviqaDocumentArchive(
                directory, monday_publisher=lambda *args: calls.append(args)
            ).archive(document)

            self.assertEqual(result["monday_status"], "upload_in_doubt")
            self.assertEqual(calls, [])

    def test_path_traversal_is_sanitized_inside_case_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(directory).archive(self.document(case_name="../Example/Project", filename="../../TAG.pdf"))
            self.assertEqual(result["case_folder"], "_Example_Project")
            self.assertEqual(result["filename"], "_.._TAG.pdf")
            archived = Path(directory) / "tenant-test-001" / result["case_folder"] / result["filename"]
            self.assertTrue(archived.is_file())
            self.assertEqual(archived.resolve().parents[2], Path(directory).resolve())

    def test_non_pdf_and_checksum_mismatch_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = MeraviqaDocumentArchive(directory)
            with self.assertRaisesRegex(WorkflowError, "must be a PDF"):
                archive.archive(self.document(content=b"not-pdf"))
            with self.assertRaisesRegex(WorkflowError, "checksum does not match"):
                archive.archive(self.document(content_sha256="a" * 64))

    def test_same_filename_with_different_content_gets_hash_suffix(self):
        second_pdf = b"%PDF-1.7\nSecond document"
        with tempfile.TemporaryDirectory() as directory:
            archive = MeraviqaDocumentArchive(directory)
            archive.archive(self.document())
            second = archive.archive(self.document(content=second_pdf, content_sha256=hashlib.sha256(second_pdf).hexdigest()))
            self.assertRegex(second["filename"], r"^TAG-[0-9a-f]{12}\.pdf$")


if __name__ == "__main__":
    unittest.main()
