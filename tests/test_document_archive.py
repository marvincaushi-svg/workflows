from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from workflowos.core import WorkflowError
from workflowos.document_archive import MeraviqaDocumentArchive, SbPdfDocument


PDF = b"%PDF-1.7\nSB document"


class MeraviqaDocumentArchiveTests(unittest.TestCase):
    def document(self, **changes):
        values = {
            "tenant_id": "af-elektro", "case_id": "case-001",
            "case_name": "Wuersten Juerg", "monday_item_id": "2001",
            "monday_column_id": "file_tag",
            "document_type": "tag_grid_connection_application",
            "filename": "TAG.pdf", "content": PDF,
            "content_sha256": hashlib.sha256(PDF).hexdigest(),
        }
        values.update(changes)
        return SbPdfDocument(**values)

    def test_archives_in_case_named_folder_then_uploads_to_monday(self):
        calls = []

        def publish(item_id, column_id, filename, content):
            calls.append((item_id, column_id, filename, content))
            return {"item_id": item_id, "column_id": column_id, "content_sha256": hashlib.sha256(content).hexdigest()}

        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(directory, monday_publisher=publish).archive(self.document())
            case = Path(directory) / "af-elektro" / "Wuersten Juerg"
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
            self.assertEqual(result["monday_status"], "pending")
            self.assertTrue((Path(directory) / "af-elektro" / "Wuersten Juerg" / "TAG.pdf").is_file())

    def test_path_traversal_is_sanitized_inside_case_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(directory).archive(self.document(case_name="../Wuersten/Juerg", filename="../../TAG.pdf"))
            self.assertEqual(result["case_folder"], "_Wuersten_Juerg")
            self.assertEqual(result["filename"], "_.._TAG.pdf")
            archived = Path(directory) / "af-elektro" / result["case_folder"] / result["filename"]
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
