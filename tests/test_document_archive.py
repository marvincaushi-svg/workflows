from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflowos.core import PublicationRefused, WorkflowError
from workflowos.document_archive import (
    MeraviqaDocumentArchive,
    MondayArchiveReconciliation,
    SbPdfDocument,
    inspect_document_archive,
    reconcile_archived_monday_upload,
    retry_archived_monday_upload,
)


PDF = b"%PDF-1.7\nSB document"


class ArchiveFixtures:
    """Shared sanitized document and evidence builders."""

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


class MeraviqaDocumentArchiveTests(ArchiveFixtures, unittest.TestCase):
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

    def test_reconciliation_cannot_be_bound_to_another_monday_item(self):
        def uncertain(*args):
            raise OSError("connection lost")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(directory, monday_publisher=uncertain)
            archive.archive(document)
            redirected = self.document(monday_item_id="9999")

            with self.assertRaisesRegex(WorkflowError, "another item or column"):
                archive.reconcile_monday(
                    redirected, self.evidence(redirected, "confirmed_uploaded")
                )

    def test_authorized_retry_cannot_publish_to_another_monday_item(self):
        attempts = []

        def publisher(item_id, column_id, filename, content):
            attempts.append((item_id, column_id))
            raise OSError("connection lost")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(directory, monday_publisher=publisher)
            archive.archive(document)
            archive.reconcile_monday(
                document, self.evidence(document, "confirmed_not_uploaded")
            )

            with self.assertRaisesRegex(WorkflowError, "another item or column"):
                archive.retry_authorized_monday(self.document(monday_item_id="9999"))
            self.assertEqual(attempts, [("2001", "file_tag")])


class RefusedPublicationTests(ArchiveFixtures, unittest.TestCase):
    def refusing_publisher(self, calls):
        def publisher(item_id, column_id, filename, content):
            calls.append(item_id)
            raise PublicationRefused("column is not configured")

        return publisher

    def test_certain_non_transmission_keeps_the_entry_actionable(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            result = MeraviqaDocumentArchive(
                directory, monday_publisher=self.refusing_publisher(calls)
            ).archive(document)

            self.assertEqual(result["monday_status"], "pending")
            self.assertEqual(result["upload_attempts"], 0)
            self.assertEqual(result["refused_attempts"], 1)
            self.assertEqual(len(calls), 1)

    def test_refusal_does_not_demand_reconciliation_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            MeraviqaDocumentArchive(
                directory, monday_publisher=self.refusing_publisher([])
            ).archive(document)

            with self.assertRaisesRegex(WorkflowError, "not awaiting reconciliation"):
                reconcile_archived_monday_upload(
                    directory, self.evidence(document, "confirmed_not_uploaded")
                )

    def test_refusal_never_consumes_an_authorized_retry(self):
        def publisher(item_id, column_id, filename, content):
            raise PublicationRefused("binding rejected")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(
                directory, monday_publisher=lambda *args: (_ for _ in ()).throw(
                    OSError("connection lost")
                )
            )
            archive.archive(document)
            archive.reconcile_monday(
                document, self.evidence(document, "confirmed_not_uploaded")
            )

            refusing = MeraviqaDocumentArchive(directory, monday_publisher=publisher)
            refused = refusing.retry_authorized_monday(document)

            self.assertEqual(refused["monday_status"], "retry_authorized")
            self.assertEqual(refused["upload_attempts"], 1)
            self.assertEqual(refused["refused_attempts"], 1)

    def test_uncertain_outcome_still_requires_reconciliation(self):
        def uncertain(*args):
            raise OSError("connection lost after upload")

        with tempfile.TemporaryDirectory() as directory:
            result = MeraviqaDocumentArchive(
                directory, monday_publisher=uncertain
            ).archive(self.document())

            self.assertEqual(result["monday_status"], "upload_in_doubt")
            self.assertEqual(result["upload_attempts"], 1)
            self.assertEqual(result["refused_attempts"], 0)

    def test_inspection_reports_refusals_and_success_clears_them(self):
        attempts = []

        def publisher(item_id, column_id, filename, content):
            attempts.append(filename)
            if len(attempts) == 1:
                raise PublicationRefused("column is not configured")
            return {
                "item_id": item_id,
                "column_id": column_id,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            archive = MeraviqaDocumentArchive(directory, monday_publisher=publisher)
            archive.archive(document)

            report = inspect_document_archive(directory)
            entry = report["entries"][0]
            self.assertEqual(entry["action_required"], "publish_monday_upload")
            self.assertEqual(entry["refused_attempts"], 1)
            self.assertEqual(entry["last_refusal_code"], "PublicationRefused")

            archive.archive(document)
            resolved = inspect_document_archive(directory)["entries"][0]
            self.assertEqual(resolved["monday_status"], "uploaded")
            self.assertNotIn("last_refusal_code", resolved)
            self.assertEqual(resolved["upload_attempts"], 1)


class DocumentArchiveInspectionTests(ArchiveFixtures, unittest.TestCase):
    def uncertain_archive(self, directory):
        """Archive one PDF whose Monday outcome stays unknown."""

        def uncertain(*args):
            raise OSError("connection lost after upload")

        document = self.document()
        MeraviqaDocumentArchive(directory, monday_publisher=uncertain).archive(document)
        return document

    def test_inspection_reports_action_without_exposing_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.uncertain_archive(directory)
            report = inspect_document_archive(directory)

            self.assertEqual(report["entry_count"], 1)
            self.assertEqual(report["unresolved_count"], 1)
            entry = report["entries"][0]
            self.assertEqual(entry["monday_status"], "upload_in_doubt")
            self.assertEqual(entry["action_required"], "reconcile_monday_upload")
            self.assertEqual(entry["content_sha256"], document.content_sha256)
            self.assertEqual(entry["case_id"], "case-001")
            serialized = json.dumps(report)
            self.assertNotIn("Example Project", serialized)
            self.assertNotIn("TAG.pdf", serialized)
            self.assertNotIn("2001", serialized)

    def test_inspection_never_writes_to_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            self.uncertain_archive(directory)
            case = Path(directory) / "tenant-test-001" / "Example Project"
            before = sorted(
                (path.relative_to(directory).as_posix(), path.read_bytes())
                for path in Path(directory).rglob("*")
                if path.is_file()
            )

            inspect_document_archive(directory)

            after = sorted(
                (path.relative_to(directory).as_posix(), path.read_bytes())
                for path in Path(directory).rglob("*")
                if path.is_file()
            )
            self.assertEqual(after, before)
            self.assertTrue((case / "TAG.pdf").is_file())

    def test_inspection_is_scoped_to_the_requested_tenant(self):
        with tempfile.TemporaryDirectory() as directory:
            self.uncertain_archive(directory)
            other = self.document(tenant_id="tenant-test-002", case_id="case-002")
            MeraviqaDocumentArchive(directory).archive(other)

            everything = inspect_document_archive(directory)
            scoped = inspect_document_archive(directory, tenant_id="tenant-test-002")

            self.assertEqual(everything["entry_count"], 2)
            self.assertEqual(scoped["entry_count"], 1)
            self.assertEqual(scoped["entries"][0]["tenant_id"], "tenant-test-002")
            self.assertEqual(
                scoped["entries"][0]["action_required"], "publish_monday_upload"
            )

    def test_confirmed_non_upload_authorizes_retry_without_a_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.uncertain_archive(directory)
            reconciled = reconcile_archived_monday_upload(
                directory, self.evidence(document, "confirmed_not_uploaded")
            )

            self.assertEqual(reconciled["result"]["status"], "retry_authorized")
            self.assertFalse(reconciled["result"]["monday_uploaded"])
            report = inspect_document_archive(directory)
            self.assertEqual(
                report["entries"][0]["action_required"], "retry_monday_upload"
            )

    def test_confirmed_upload_closes_the_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.uncertain_archive(directory)
            reconciled = reconcile_archived_monday_upload(
                directory, self.evidence(document, "confirmed_uploaded")
            )

            self.assertEqual(reconciled["result"]["status"], "uploaded_reconciled")
            self.assertTrue(reconciled["result"]["monday_uploaded"])
            report = inspect_document_archive(directory)
            self.assertEqual(report["unresolved_count"], 0)
            self.assertEqual(report["entries"][0]["action_required"], "none")

    def test_unknown_case_or_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.uncertain_archive(directory)
            unknown_case = self.document(case_id="case-999")
            with self.assertRaisesRegex(WorkflowError, "no folder for this tenant"):
                reconcile_archived_monday_upload(
                    directory, self.evidence(unknown_case, "confirmed_uploaded")
                )

            unknown_pdf = self.document(content_sha256="f" * 64)
            with self.assertRaisesRegex(WorkflowError, "not registered"):
                reconcile_archived_monday_upload(
                    directory, self.evidence(unknown_pdf, "confirmed_uploaded")
                )
            self.assertEqual(
                inspect_document_archive(directory)["entries"][0]["monday_status"],
                "upload_in_doubt",
            )

    def test_resolved_entry_cannot_be_reconciled_again(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.uncertain_archive(directory)
            reconcile_archived_monday_upload(
                directory, self.evidence(document, "confirmed_uploaded")
            )

            with self.assertRaisesRegex(WorkflowError, "not awaiting reconciliation"):
                reconcile_archived_monday_upload(
                    directory, self.evidence(document, "confirmed_not_uploaded")
                )


class AuthorizedRetryTests(ArchiveFixtures, unittest.TestCase):
    def authorized_archive(self, directory, document):
        """Bring one archived PDF to the retry_authorized state."""

        def uncertain(*args):
            raise OSError("connection lost after upload")

        archive = MeraviqaDocumentArchive(directory, monday_publisher=uncertain)
        archive.archive(document)
        archive.reconcile_monday(
            document, self.evidence(document, "confirmed_not_uploaded")
        )

    def accepting_publisher(self, calls):
        def publisher(item_id, column_id, filename, content):
            calls.append((item_id, column_id, filename))
            return {
                "item_id": item_id,
                "column_id": column_id,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }

        return publisher

    def test_authorized_retry_uploads_the_archived_pdf(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            self.authorized_archive(directory, document)

            retried = retry_archived_monday_upload(
                directory,
                document.tenant_id,
                document.case_id,
                document.content_sha256,
                publisher=self.accepting_publisher(calls),
                publisher_tenant_id=document.tenant_id,
            )

            self.assertEqual(retried["result"]["status"], "uploaded")
            self.assertTrue(retried["result"]["monday_uploaded"])
            self.assertEqual(calls, [("2001", "file_tag", "TAG.pdf")])
            self.assertEqual(
                inspect_document_archive(directory)["unresolved_count"], 0
            )

    def test_retry_is_available_only_once(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            self.authorized_archive(directory, document)
            retry = lambda: retry_archived_monday_upload(
                directory,
                document.tenant_id,
                document.case_id,
                document.content_sha256,
                publisher=self.accepting_publisher(calls),
                publisher_tenant_id=document.tenant_id,
            )
            retry()

            with self.assertRaisesRegex(WorkflowError, "not been authorized"):
                retry()
            self.assertEqual(len(calls), 1)

    def test_retry_without_authorization_is_refused(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            MeraviqaDocumentArchive(directory).archive(document)

            with self.assertRaisesRegex(WorkflowError, "not been authorized"):
                retry_archived_monday_upload(
                    directory,
                    document.tenant_id,
                    document.case_id,
                    document.content_sha256,
                    publisher=self.accepting_publisher(calls),
                    publisher_tenant_id=document.tenant_id,
                )
            self.assertEqual(calls, [])

    def test_publisher_of_another_tenant_is_never_used(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            self.authorized_archive(directory, document)

            with self.assertRaisesRegex(WorkflowError, "Publisher tenant"):
                retry_archived_monday_upload(
                    directory,
                    document.tenant_id,
                    document.case_id,
                    document.content_sha256,
                    publisher=self.accepting_publisher(calls),
                    publisher_tenant_id="tenant-test-999",
                )
            self.assertEqual(calls, [])

    def test_refused_retry_keeps_the_authorization(self):
        def refusing(*args):
            raise PublicationRefused("column is not configured")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            self.authorized_archive(directory, document)

            refused = retry_archived_monday_upload(
                directory,
                document.tenant_id,
                document.case_id,
                document.content_sha256,
                publisher=refusing,
                publisher_tenant_id=document.tenant_id,
            )

            self.assertEqual(refused["result"]["status"], "retry_refused")
            self.assertFalse(refused["result"]["monday_uploaded"])
            self.assertEqual(
                inspect_document_archive(directory)["entries"][0]["action_required"],
                "retry_monday_upload",
            )

    def test_uncertain_retry_requires_reconciliation_again(self):
        def uncertain(*args):
            raise OSError("connection lost again")

        with tempfile.TemporaryDirectory() as directory:
            document = self.document()
            self.authorized_archive(directory, document)

            retried = retry_archived_monday_upload(
                directory,
                document.tenant_id,
                document.case_id,
                document.content_sha256,
                publisher=uncertain,
                publisher_tenant_id=document.tenant_id,
            )

            self.assertEqual(retried["result"]["status"], "upload_in_doubt")
            self.assertEqual(
                inspect_document_archive(directory)["entries"][0]["action_required"],
                "reconcile_monday_upload",
            )


class DocumentArchiveCliTests(ArchiveFixtures, unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "workflowos.cli", *arguments],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )

    def archive_uncertain_pdf(self, directory):
        def uncertain(*args):
            raise OSError("connection lost after upload")

        document = self.document()
        MeraviqaDocumentArchive(directory, monday_publisher=uncertain).archive(document)
        return document

    def test_inspect_command_hides_case_folder_and_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            self.archive_uncertain_pdf(directory)
            run = self.run_cli(
                "inspect-document-archive", "--archive-root", directory
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(report["unresolved_count"], 1)
            self.assertEqual(
                report["entries"][0]["action_required"], "reconcile_monday_upload"
            )
            self.assertNotIn("Example Project", run.stdout)
            self.assertNotIn("TAG.pdf", run.stdout)

    def test_reconcile_command_authorizes_retry_without_touching_monday(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.archive_uncertain_pdf(directory)
            run = self.run_cli(
                "reconcile-monday-upload",
                "--archive-root", directory,
                "--tenant-id", document.tenant_id,
                "--case-id", document.case_id,
                "--content-sha256", document.content_sha256,
                "--outcome", "confirmed-not-uploaded",
                "--checked-at", "2026-08-05T09:00:00+02:00",
                "--checked-by-ref", "operator-ref-001",
                "--evidence-ref-sha256", "e" * 64,
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["status"], "retry_authorized")
            self.assertFalse(result["monday_uploaded"])
            manifest = json.loads(
                (
                    Path(directory)
                    / "tenant-test-001"
                    / "Example Project"
                    / ".meraviqa-documents.json"
                ).read_text(encoding="utf-8")
            )
            entry = manifest["documents"][document.content_sha256]
            self.assertEqual(entry["monday_status"], "retry_authorized")
            self.assertEqual(
                entry["reconciliation"]["outcome"], "confirmed_not_uploaded"
            )

    def test_reconcile_command_rejects_unverifiable_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self.archive_uncertain_pdf(directory)
            run = self.run_cli(
                "reconcile-monday-upload",
                "--archive-root", directory,
                "--tenant-id", document.tenant_id,
                "--case-id", document.case_id,
                "--content-sha256", document.content_sha256,
                "--outcome", "confirmed-uploaded",
                "--checked-at", "2026-08-05T09:00:00+02:00",
                "--checked-by-ref", "operator-ref-001",
                "--evidence-ref-sha256", "not-a-sha256",
            )

            self.assertEqual(run.returncode, 2)
            self.assertIn("evidence must be SHA-256", run.stderr)
            self.assertEqual(
                inspect_document_archive(directory)["entries"][0]["monday_status"],
                "upload_in_doubt",
            )


if __name__ == "__main__":
    unittest.main()
