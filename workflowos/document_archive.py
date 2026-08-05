"""Tenant-isolated MERAVIQA archive with guarded Monday publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import (
    SHA256_RE,
    PublicationRefused,
    WorkflowError,
    _require_string,
    _require_timestamp,
)
from .state_store import lock_automation_state


MAX_ARCHIVE_PDF_BYTES = 10 * 1024 * 1024
MANIFEST_SCHEMA_VERSION = "1.1"
MANIFEST_FILENAME = ".meraviqa-documents.json"
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
MONDAY_STATUSES = {
    "pending",
    "uploading",
    "upload_in_doubt",
    "retry_authorized",
    "uploaded",
}
UNRESOLVED_MONDAY_STATUSES = {
    "pending",
    "uploading",
    "upload_in_doubt",
    "retry_authorized",
}
MONDAY_ARCHIVE_ACTIONS = {
    "pending": "publish_monday_upload",
    "uploading": "reconcile_monday_upload",
    "upload_in_doubt": "reconcile_monday_upload",
    "retry_authorized": "retry_monday_upload",
    "uploaded": "none",
}
RECONCILIATION_OUTCOMES = {"confirmed_uploaded", "confirmed_not_uploaded"}
MondayPdfPublisher = Callable[[str, str, str, bytes], dict[str, Any]]


@dataclass(frozen=True)
class SbPdfDocument:
    """One verified SB PDF bound to an exact tenant and Monday item."""

    tenant_id: str
    case_id: str
    case_name: str
    monday_item_id: str
    monday_column_id: str
    document_type: str
    filename: str
    content: bytes
    content_sha256: str


@dataclass(frozen=True)
class MondayArchiveReconciliation:
    """Auditable evidence resolving an uncertain Monday upload outcome."""

    tenant_id: str
    case_id: str
    content_sha256: str
    outcome: str
    checked_at: str
    checked_by_ref: str
    evidence_ref_sha256: str


class MeraviqaDocumentArchive:
    """Store PDFs by project name and optionally publish the same bytes to Monday."""

    def __init__(self, root: str | Path, *, monday_publisher: MondayPdfPublisher | None = None, max_pdf_bytes: int = MAX_ARCHIVE_PDF_BYTES) -> None:
        self._root = Path(root)
        self._monday_publisher = monday_publisher
        if max_pdf_bytes <= 0 or max_pdf_bytes > MAX_ARCHIVE_PDF_BYTES:
            raise WorkflowError("Archive PDF limit must be between 1 byte and 10 MB")
        self._max_pdf_bytes = max_pdf_bytes

    def archive(self, document: SbPdfDocument) -> dict[str, Any]:
        """Archive once, then publish to the bound Monday item when configured."""

        validated = _validate_document(document, self._max_pdf_bytes)
        case_directory = self._case_directory(validated)
        case_directory.mkdir(parents=True, exist_ok=True)
        manifest_path = case_directory / MANIFEST_FILENAME

        with lock_automation_state(manifest_path):
            manifest = _load_manifest(
                manifest_path, validated.tenant_id, validated.case_id
            )
            existing = manifest["documents"].get(validated.content_sha256)
            if existing is not None:
                existing_path = _validated_archived_path(
                    case_directory, existing, validated.content_sha256
                )
                if (
                    existing.get("monday_status") == "pending"
                    and self._monday_publisher is not None
                ):
                    return self._publish_entry(
                        manifest_path,
                        manifest,
                        existing,
                        existing_path,
                        validated,
                        archive_status="already_archived",
                    )
                return _archive_result(
                    validated,
                    case_directory,
                    existing_path,
                    existing,
                    status="already_archived",
                )

            destination = case_directory / _safe_pdf_filename(validated.filename)
            if (
                destination.exists()
                and _file_sha256(destination) != validated.content_sha256
            ):
                destination = _deduplicated_destination(
                    destination, validated.content_sha256
                )
            _atomic_write_bytes(destination, validated.content)

            entry = {
                "filename": destination.name,
                "document_type": validated.document_type,
                "monday_item_id": validated.monday_item_id,
                "monday_column_id": validated.monday_column_id,
                "monday_status": "pending",
                "monday_ref_sha256": None,
                "upload_attempts": 0,
            }
            manifest["documents"][validated.content_sha256] = entry
            _atomic_write_json(manifest_path, manifest)
            if self._monday_publisher is None:
                return _archive_result(
                    validated,
                    case_directory,
                    destination,
                    entry,
                    status="archived",
                )
            return self._publish_entry(
                manifest_path,
                manifest,
                entry,
                destination,
                validated,
                archive_status="archived",
            )

    def reconcile_monday(
        self,
        document: SbPdfDocument,
        evidence: MondayArchiveReconciliation,
    ) -> dict[str, Any]:
        """Resolve an uncertain upload without writing to Monday."""

        validated = _validate_document(document, self._max_pdf_bytes)
        reconciliation = _validate_reconciliation(evidence, validated)
        case_directory = self._case_directory(validated)
        manifest_path = case_directory / MANIFEST_FILENAME
        with lock_automation_state(manifest_path):
            manifest = _load_manifest(
                manifest_path, validated.tenant_id, validated.case_id
            )
            entry = _require_archive_entry(manifest, validated.content_sha256)
            _validate_entry_binding(entry, validated)
            archived_path = _validated_archived_path(
                case_directory, entry, validated.content_sha256
            )
            if entry.get("monday_status") not in {"uploading", "upload_in_doubt"}:
                raise WorkflowError("Monday archive upload is not awaiting reconciliation")
            if evidence.outcome == "confirmed_uploaded":
                entry["monday_status"] = "uploaded"
                entry["monday_ref_sha256"] = validated.content_sha256
                result_status = "uploaded_reconciled"
            else:
                entry["monday_status"] = "retry_authorized"
                entry["monday_ref_sha256"] = None
                result_status = "retry_authorized"
            entry["reconciliation"] = reconciliation
            entry.pop("last_error_code", None)
            _atomic_write_json(manifest_path, manifest)
            result = _archive_result(
                validated,
                case_directory,
                archived_path,
                entry,
                status="already_archived",
            )
            result["reconciliation_status"] = result_status
            return result

    def retry_authorized_monday(self, document: SbPdfDocument) -> dict[str, Any]:
        """Perform one retry after evidence confirms the PDF was not uploaded."""

        if self._monday_publisher is None:
            raise WorkflowError("Monday retry requires a publisher adapter")
        validated = _validate_document(document, self._max_pdf_bytes)
        case_directory = self._case_directory(validated)
        manifest_path = case_directory / MANIFEST_FILENAME
        with lock_automation_state(manifest_path):
            manifest = _load_manifest(
                manifest_path, validated.tenant_id, validated.case_id
            )
            entry = _require_archive_entry(manifest, validated.content_sha256)
            _validate_entry_binding(entry, validated)
            archived_path = _validated_archived_path(
                case_directory, entry, validated.content_sha256
            )
            if entry.get("monday_status") != "retry_authorized":
                raise WorkflowError("Monday archive retry has not been authorized")
            return self._publish_entry(
                manifest_path,
                manifest,
                entry,
                archived_path,
                validated,
                archive_status="already_archived",
            )

    def _case_directory(self, document: SbPdfDocument) -> Path:
        return (
            self._root
            / document.tenant_id
            / _safe_name(document.case_name, "case_name")
        )

    def _publish_entry(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        entry: dict[str, Any],
        archived_path: Path,
        document: SbPdfDocument,
        *,
        archive_status: str,
    ) -> dict[str, Any]:
        previous_status = entry.get("monday_status")
        if previous_status not in {"pending", "retry_authorized"}:
            raise WorkflowError("Monday archive upload is not safe to start")
        previous_attempts = _require_upload_attempts(entry)
        entry["monday_status"] = "uploading"
        entry["upload_attempts"] = previous_attempts + 1
        _atomic_write_json(manifest_path, manifest)
        try:
            confirmation = self._monday_publisher(
                document.monday_item_id,
                document.monday_column_id,
                archived_path.name,
                document.content,
            )
            monday_ref = _validate_monday_confirmation(confirmation, document)
        except PublicationRefused as refusal:
            # The adapter guarantees the PDF never left this process, so the
            # entry keeps its previous state instead of demanding evidence.
            entry["monday_status"] = previous_status
            entry["monday_ref_sha256"] = None
            entry["upload_attempts"] = previous_attempts
            entry["refused_attempts"] = _require_refused_attempts(entry) + 1
            entry["last_refusal_code"] = type(refusal).__name__
            _atomic_write_json(manifest_path, manifest)
            return _archive_result(
                document,
                archived_path.parent,
                archived_path,
                entry,
                status=archive_status,
            )
        except Exception as exc:
            entry["monday_status"] = "upload_in_doubt"
            entry["monday_ref_sha256"] = None
            entry["last_error_code"] = type(exc).__name__
            _atomic_write_json(manifest_path, manifest)
            return _archive_result(
                document,
                archived_path.parent,
                archived_path,
                entry,
                status=archive_status,
            )

        entry["monday_status"] = "uploaded"
        entry["monday_ref_sha256"] = monday_ref
        entry.pop("last_error_code", None)
        entry.pop("last_refusal_code", None)
        _atomic_write_json(manifest_path, manifest)
        return _archive_result(
            document,
            archived_path.parent,
            archived_path,
            entry,
            status=archive_status,
        )


def inspect_document_archive(
    root: str | Path, *, tenant_id: str | None = None
) -> dict[str, Any]:
    """Report archived PDFs awaiting a Monday outcome without exposing identities.

    The scan only reads manifests: it never opens a PDF, never contacts Monday
    and never reports folder names, file names or Monday identifiers.
    """

    root_path = Path(root)
    entries: list[dict[str, Any]] = []
    for tenant_directory in _archive_tenant_directories(root_path, tenant_id):
        for case_directory in _archive_case_directories(tenant_directory):
            manifest_path = case_directory / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            manifest = _parse_manifest(manifest_path)
            if manifest["tenant_id"] != tenant_directory.name:
                raise WorkflowError("MERAVIQA archive manifest tenant does not match its folder")
            entries.extend(_inspected_entries(manifest))
    entries.sort(key=lambda entry: (entry["case_id"], entry["content_sha256"]))
    return {
        "status": "ok",
        "entry_count": len(entries),
        "unresolved_count": sum(
            entry["monday_status"] in UNRESOLVED_MONDAY_STATUSES for entry in entries
        ),
        "entries": entries,
    }


def reconcile_archived_monday_upload(
    root: str | Path,
    evidence: MondayArchiveReconciliation,
    *,
    max_pdf_bytes: int = MAX_ARCHIVE_PDF_BYTES,
) -> dict[str, Any]:
    """Resolve an uncertain archive upload from persisted evidence only.

    The document binding is rebuilt from the manifest and the archived PDF, so
    the caller cannot redirect the outcome to another Monday item or column.
    The archive is constructed without a publisher: no Monday call is possible.
    """

    root_path = Path(root)
    tenant_id = _require_string(evidence.tenant_id, "reconciliation.tenant_id")
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise WorkflowError("Archive tenant id is invalid")
    case_id = _require_string(evidence.case_id, "reconciliation.case_id")
    if not SHA256_RE.fullmatch(evidence.content_sha256):
        raise WorkflowError("Monday reconciliation checksum must be SHA-256")

    case_directory = _find_case_directory(root_path, tenant_id, case_id)
    manifest = _parse_manifest(case_directory / MANIFEST_FILENAME)
    document = _rebuild_archived_document(
        case_directory, manifest, evidence.content_sha256
    )
    archive = MeraviqaDocumentArchive(root_path, max_pdf_bytes=max_pdf_bytes)
    archived = archive.reconcile_monday(document, evidence)
    return {
        "archive": archived,
        "result": {
            "status": archived["reconciliation_status"],
            "tenant_id": archived["tenant_id"],
            "case_id": archived["case_id"],
            "content_sha256": archived["content_sha256"],
            "document_type": document.document_type,
            "monday_status": archived["monday_status"],
            "upload_attempts": archived["upload_attempts"],
            "monday_uploaded": archived["monday_status"] == "uploaded",
        },
    }


def _archive_tenant_directories(
    root: Path, tenant_id: str | None
) -> list[Path]:
    if not _is_real_directory(root):
        raise WorkflowError("MERAVIQA archive root is not a directory")
    if tenant_id is not None:
        if not TENANT_ID_RE.fullmatch(tenant_id):
            raise WorkflowError("Archive tenant id is invalid")
        candidate = root / tenant_id
        return [candidate] if _is_real_directory(candidate) else []
    return sorted(
        child
        for child in root.iterdir()
        if _is_real_directory(child) and TENANT_ID_RE.fullmatch(child.name)
    )


def _archive_case_directories(tenant_directory: Path) -> list[Path]:
    return sorted(
        child for child in tenant_directory.iterdir() if _is_real_directory(child)
    )


def _is_real_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _inspected_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    inspected = []
    for content_sha256, entry in sorted(manifest["documents"].items()):
        status = entry["monday_status"]
        snapshot = {
            "tenant_id": manifest["tenant_id"],
            "case_id": manifest["case_id"],
            "content_sha256": content_sha256,
            "document_type": _require_string(
                entry.get("document_type"), "manifest.document.document_type"
            ),
            "monday_status": status,
            "upload_attempts": _require_upload_attempts(entry),
            "refused_attempts": _require_refused_attempts(entry),
            "action_required": MONDAY_ARCHIVE_ACTIONS[status],
        }
        if entry.get("last_refusal_code") is not None:
            snapshot["last_refusal_code"] = _require_string(
                entry.get("last_refusal_code"), "manifest.document.last_refusal_code"
            )
        if status == "uploaded":
            snapshot["monday_ref_sha256"] = _require_string(
                entry.get("monday_ref_sha256"), "manifest.document.monday_ref_sha256"
            )
        inspected.append(snapshot)
    return inspected


def _require_upload_attempts(entry: dict[str, Any]) -> int:
    return _require_attempt_count(entry, "upload_attempts")


def _require_refused_attempts(entry: dict[str, Any]) -> int:
    return _require_attempt_count(entry, "refused_attempts")


def _require_attempt_count(entry: dict[str, Any], field: str) -> int:
    attempts = entry.get(field, 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise WorkflowError(f"MERAVIQA archive {field} are invalid")
    return attempts


def _find_case_directory(root: Path, tenant_id: str, case_id: str) -> Path:
    tenant_directory = root / tenant_id
    if not _is_real_directory(tenant_directory):
        raise WorkflowError("MERAVIQA archive has no folder for this tenant")
    matches = []
    for case_directory in _archive_case_directories(tenant_directory):
        manifest_path = case_directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = _parse_manifest(manifest_path)
        if manifest["tenant_id"] == tenant_id and manifest["case_id"] == case_id:
            matches.append(case_directory)
    if not matches:
        raise WorkflowError("MERAVIQA archive has no folder for this tenant and case")
    if len(matches) > 1:
        raise WorkflowError("MERAVIQA archive has more than one folder for this case")
    return matches[0]


def _rebuild_archived_document(
    case_directory: Path, manifest: dict[str, Any], content_sha256: str
) -> SbPdfDocument:
    entry = _require_archive_entry(manifest, content_sha256)
    archived_path = _validated_archived_path(case_directory, entry, content_sha256)
    return SbPdfDocument(
        tenant_id=manifest["tenant_id"],
        case_id=manifest["case_id"],
        case_name=case_directory.name,
        monday_item_id=_require_string(
            entry.get("monday_item_id"), "manifest.document.monday_item_id"
        ),
        monday_column_id=_require_string(
            entry.get("monday_column_id"), "manifest.document.monday_column_id"
        ),
        document_type=_require_string(
            entry.get("document_type"), "manifest.document.document_type"
        ),
        filename=archived_path.name,
        content=archived_path.read_bytes(),
        content_sha256=content_sha256,
    )


def _validate_document(document: SbPdfDocument, max_bytes: int) -> SbPdfDocument:
    if not TENANT_ID_RE.fullmatch(document.tenant_id):
        raise WorkflowError("Document tenant id is invalid")
    _require_string(document.case_id, "document.case_id")
    _safe_name(document.case_name, "document.case_name")
    if not document.monday_item_id.isdigit():
        raise WorkflowError("Document Monday item id must be numeric")
    if not re.fullmatch(r"[A-Za-z0-9_]+", document.monday_column_id):
        raise WorkflowError("Document Monday column id is invalid")
    _require_string(document.document_type, "document.document_type")
    _safe_pdf_filename(document.filename)
    if not isinstance(document.content, bytes) or not document.content.startswith(b"%PDF-"):
        raise WorkflowError("Document content must be a PDF")
    if len(document.content) > max_bytes:
        raise WorkflowError("Document PDF exceeds the 10 MB archive limit")
    if not SHA256_RE.fullmatch(document.content_sha256):
        raise WorkflowError("Document content_sha256 must be SHA-256")
    if hashlib.sha256(document.content).hexdigest() != document.content_sha256:
        raise WorkflowError("Document checksum does not match PDF content")
    return document


def _safe_name(value: str, field: str) -> str:
    normalized = unicodedata.normalize("NFC", _require_string(value, field))
    cleaned = "".join("_" if char in "/\\" or ord(char) < 32 else char for char in normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise WorkflowError(f"{field} cannot be used as a folder name")
    return cleaned[:120].rstrip(" .")


def _safe_pdf_filename(value: str) -> str:
    cleaned = _safe_name(value, "document.filename")
    if not cleaned.casefold().endswith(".pdf"):
        raise WorkflowError("Document filename must end with .pdf")
    return cleaned


def _load_manifest(path: Path, tenant_id: str, case_id: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tenant_id": tenant_id,
            "case_id": case_id,
            "documents": {},
        }
    manifest = _parse_manifest(path)
    if manifest.get("tenant_id") != tenant_id or manifest.get("case_id") != case_id:
        raise WorkflowError("MERAVIQA archive folder belongs to another case")
    return manifest


def _parse_manifest(path: Path) -> dict[str, Any]:
    """Read and validate a manifest without binding it to an expected case."""

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("MERAVIQA archive manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise WorkflowError("MERAVIQA archive manifest must be an object")
    schema_version = manifest.get("schema_version")
    if schema_version not in {"1.0", MANIFEST_SCHEMA_VERSION}:
        raise WorkflowError("Unsupported MERAVIQA archive manifest schema")
    if schema_version == MANIFEST_SCHEMA_VERSION:
        expected = _require_string(
            manifest.get("payload_sha256"), "manifest.payload_sha256"
        )
        if expected != _manifest_payload_sha256(manifest):
            raise WorkflowError("MERAVIQA archive manifest checksum does not match")
    _require_string(manifest.get("tenant_id"), "manifest.tenant_id")
    _require_string(manifest.get("case_id"), "manifest.case_id")
    if not isinstance(manifest.get("documents"), dict):
        raise WorkflowError("MERAVIQA archive manifest documents are invalid")
    if schema_version == "1.0":
        manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
        for raw_entry in manifest["documents"].values():
            if isinstance(raw_entry, dict) and raw_entry.get("monday_status") == "pending":
                raw_entry["monday_status"] = "upload_in_doubt"
                raw_entry["migration_reason"] = "legacy_pending_outcome_unknown"
                raw_entry.setdefault("upload_attempts", 1)
    for checksum, raw_entry in manifest["documents"].items():
        if not SHA256_RE.fullmatch(checksum) or not isinstance(raw_entry, dict):
            raise WorkflowError("MERAVIQA archive manifest entry is invalid")
        status = raw_entry.get("monday_status")
        if status not in MONDAY_STATUSES:
            raise WorkflowError("MERAVIQA archive Monday status is invalid")
    return manifest


def _validate_entry_binding(
    entry: dict[str, Any], document: SbPdfDocument
) -> None:
    """Reject evidence or a retry aimed at a different Monday target."""

    item_id = _require_string(
        entry.get("monday_item_id"), "manifest.document.monday_item_id"
    )
    column_id = _require_string(
        entry.get("monday_column_id"), "manifest.document.monday_column_id"
    )
    document_type = _require_string(
        entry.get("document_type"), "manifest.document.document_type"
    )
    if item_id != document.monday_item_id or column_id != document.monday_column_id:
        raise WorkflowError("Monday archive entry is bound to another item or column")
    if document_type != document.document_type:
        raise WorkflowError("Monday archive entry is bound to another document type")


def _require_archive_entry(
    manifest: dict[str, Any], content_sha256: str
) -> dict[str, Any]:
    entry = manifest["documents"].get(content_sha256)
    if not isinstance(entry, dict):
        raise WorkflowError("PDF is not registered in the MERAVIQA archive")
    return entry


def _validated_archived_path(
    case_directory: Path, entry: dict[str, Any], content_sha256: str
) -> Path:
    filename = _safe_pdf_filename(
        _require_string(entry.get("filename"), "manifest.document.filename")
    )
    path = case_directory / filename
    if not path.is_file():
        raise WorkflowError("Archive manifest points to a missing PDF")
    if _file_sha256(path) != content_sha256:
        raise WorkflowError("Archived PDF checksum does not match manifest")
    return path


def _archive_result(
    document: SbPdfDocument,
    case_directory: Path,
    archived_path: Path,
    entry: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "tenant_id": document.tenant_id,
        "case_id": document.case_id,
        "case_folder": case_directory.name,
        "filename": archived_path.name,
        "content_sha256": document.content_sha256,
        "monday_status": entry.get("monday_status"),
        "upload_attempts": entry.get("upload_attempts", 0),
        "refused_attempts": entry.get("refused_attempts", 0),
    }


def _validate_reconciliation(
    evidence: MondayArchiveReconciliation, document: SbPdfDocument
) -> dict[str, Any]:
    if evidence.tenant_id != document.tenant_id:
        raise WorkflowError("Monday reconciliation tenant does not match document")
    if evidence.case_id != document.case_id:
        raise WorkflowError("Monday reconciliation case does not match document")
    if evidence.content_sha256 != document.content_sha256:
        raise WorkflowError("Monday reconciliation checksum does not match document")
    if evidence.outcome not in RECONCILIATION_OUTCOMES:
        raise WorkflowError("Monday reconciliation outcome is invalid")
    checked_at = _require_timestamp(evidence.checked_at, "reconciliation.checked_at")
    checked_by = _require_string(
        evidence.checked_by_ref, "reconciliation.checked_by_ref"
    )
    if not SHA256_RE.fullmatch(evidence.evidence_ref_sha256):
        raise WorkflowError("Monday reconciliation evidence must be SHA-256")
    return {
        "outcome": evidence.outcome,
        "checked_at": checked_at,
        "checked_by_ref": checked_by,
        "evidence_ref_sha256": evidence.evidence_ref_sha256,
    }


def _validate_monday_confirmation(confirmation: dict[str, Any], document: SbPdfDocument) -> str:
    if not isinstance(confirmation, dict):
        raise WorkflowError("Monday publisher must return a confirmation object")
    if confirmation.get("item_id") != document.monday_item_id:
        raise WorkflowError("Monday upload confirmation item does not match")
    if confirmation.get("column_id") != document.monday_column_id:
        raise WorkflowError("Monday upload confirmation column does not match")
    ref = _require_string(confirmation.get("content_sha256"), "Monday confirmation content_sha256")
    if ref != document.content_sha256:
        raise WorkflowError("Monday upload confirmation checksum does not match")
    return ref


def _deduplicated_destination(path: Path, checksum: str) -> Path:
    return path.with_name(f"{path.stem}-{checksum[:12]}{path.suffix}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    persisted = dict(document)
    persisted["payload_sha256"] = _manifest_payload_sha256(persisted)
    payload = json.dumps(persisted, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, payload)


def _manifest_payload_sha256(document: dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "payload_sha256"}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
