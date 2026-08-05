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

from .core import SHA256_RE, WorkflowError, _require_string


MAX_ARCHIVE_PDF_BYTES = 10 * 1024 * 1024
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
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
        case_directory = self._root / validated.tenant_id / _safe_name(validated.case_name, "case_name")
        case_directory.mkdir(parents=True, exist_ok=True)
        filename = _safe_pdf_filename(validated.filename)
        destination = case_directory / filename
        manifest_path = case_directory / ".meraviqa-documents.json"
        manifest = _load_manifest(manifest_path, validated.tenant_id, validated.case_id)

        existing = manifest["documents"].get(validated.content_sha256)
        if existing is not None:
            existing_path = case_directory / _require_string(existing.get("filename"), "manifest.document.filename")
            if not existing_path.is_file():
                raise WorkflowError("Archive manifest points to a missing PDF")
            if _file_sha256(existing_path) != validated.content_sha256:
                raise WorkflowError("Archived PDF checksum does not match manifest")
            return {
                "status": "already_archived", "tenant_id": validated.tenant_id,
                "case_id": validated.case_id, "case_folder": case_directory.name,
                "filename": existing_path.name, "content_sha256": validated.content_sha256,
                "monday_status": existing.get("monday_status", "pending"),
            }

        if destination.exists() and _file_sha256(destination) != validated.content_sha256:
            destination = _deduplicated_destination(destination, validated.content_sha256)
        _atomic_write_bytes(destination, validated.content)

        monday_status = "pending"
        monday_ref_sha256: str | None = None
        if self._monday_publisher is not None:
            try:
                confirmation = self._monday_publisher(validated.monday_item_id, validated.monday_column_id, destination.name, validated.content)
                monday_ref_sha256 = _validate_monday_confirmation(confirmation, validated)
                monday_status = "uploaded"
            except Exception:
                monday_status = "pending"

        manifest["documents"][validated.content_sha256] = {
            "filename": destination.name, "document_type": validated.document_type,
            "monday_item_id": validated.monday_item_id,
            "monday_column_id": validated.monday_column_id,
            "monday_status": monday_status, "monday_ref_sha256": monday_ref_sha256,
        }
        _atomic_write_json(manifest_path, manifest)
        return {
            "status": "archived", "tenant_id": validated.tenant_id,
            "case_id": validated.case_id, "case_folder": case_directory.name,
            "filename": destination.name, "content_sha256": validated.content_sha256,
            "monday_status": monday_status,
        }


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
        return {"schema_version": "1.0", "tenant_id": tenant_id, "case_id": case_id, "documents": {}}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("MERAVIQA archive manifest is unreadable") from exc
    if manifest.get("tenant_id") != tenant_id or manifest.get("case_id") != case_id:
        raise WorkflowError("MERAVIQA archive folder belongs to another case")
    if not isinstance(manifest.get("documents"), dict):
        raise WorkflowError("MERAVIQA archive manifest documents are invalid")
    return manifest


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
    payload = json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, payload)
