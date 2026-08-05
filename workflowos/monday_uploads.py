"""Guarded Monday file upload bound to one tenant board and document column.

The uploader is the only component allowed to write a file into Monday.  It is
disabled unless an explicit environment switch is set, it refuses redirects on
both the authenticated read and the authenticated upload, and it verifies that
the target item belongs to the configured board before transmitting any byte.

The confirmation it returns states what Monday actually accepted: the asset id
created by the mutation and, when Monday reports it, the stored file size.  The
checksum in the confirmation is the digest of the bytes this process
transmitted; Monday does not return a server-side content hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .automation import SUPPORTED_DOCUMENT_TYPES
from .core import WorkflowError, _require_mapping, _require_string
from .monday_assets import MONDAY_API_URL, TENANT_ID_RE, _RejectApiRedirectHandler


MONDAY_FILE_API_URL = "https://api.monday.com/v2/file"
MAX_MONDAY_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_ENABLED_VARIABLE = "WORKFLOWOS_MONDAY_UPLOAD_ENABLED"
SAFE_UPLOAD_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}\.pdf$")

READ_ITEM_BOARD_QUERY = """
query MeraviqaVerifyItemBoard($itemIds: [ID!]!) {
  items(ids: $itemIds) {
    id
    board { id }
  }
}
""".strip()

def _add_file_mutation(item_id: str, column_id: str) -> str:
    """Build Monday's documented upload mutation from already validated ids.

    Monday's file endpoint takes the target inline and only `$file` as a
    variable.  Both identifiers reach this function already constrained to
    digits and to a configured alphanumeric column, so neither can carry
    GraphQL syntax.
    """

    if not item_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9_]+", column_id):
        raise WorkflowError("Monday upload target is not safe to inline")
    return (
        "mutation MeraviqaAddFileToColumn($file: File!) {"
        f" add_file_to_column(item_id: {item_id}, column_id: \"{column_id}\","
        " file: $file) { id name file_size } }"
    )

GraphqlReader = Callable[[str, dict[str, Any], str], dict[str, Any]]
FileUploader = Callable[[str, str, str, bytes, str], dict[str, Any]]


@dataclass(frozen=True)
class MondayUploadConfig:
    """Tenant-specific write binding and a secret API token."""

    api_token: str = field(repr=False)
    tenant_id: str
    expected_board_id: str
    document_columns: Mapping[str, str]
    api_url: str = MONDAY_API_URL
    file_api_url: str = MONDAY_FILE_API_URL
    max_file_bytes: int = MAX_MONDAY_UPLOAD_BYTES

    def __post_init__(self) -> None:
        if not self.api_token or any(char in self.api_token for char in "\r\n"):
            raise WorkflowError("Monday API token is required")
        if not TENANT_ID_RE.fullmatch(self.tenant_id):
            raise WorkflowError("Monday tenant id is invalid")
        if not self.expected_board_id.isdigit():
            raise WorkflowError("Monday board id must be numeric")
        if self.api_url != MONDAY_API_URL:
            raise WorkflowError("Monday API endpoint must be https://api.monday.com/v2")
        if self.file_api_url != MONDAY_FILE_API_URL:
            raise WorkflowError(
                "Monday file endpoint must be https://api.monday.com/v2/file"
            )
        if self.max_file_bytes <= 0 or self.max_file_bytes > MAX_MONDAY_UPLOAD_BYTES:
            raise WorkflowError("Monday upload limit must be between 1 byte and 10 MB")

        columns = dict(self.document_columns)
        if not columns or set(columns.values()) != SUPPORTED_DOCUMENT_TYPES:
            raise WorkflowError(
                "Monday columns must map TAG, IA, schema and RaSi/SiNa"
            )
        if len(columns) != len(SUPPORTED_DOCUMENT_TYPES):
            raise WorkflowError("Each Monday document type must map to one column")
        for column_id in columns:
            if not re.fullmatch(r"[A-Za-z0-9_]+", column_id):
                raise WorkflowError("Monday column ids must be alphanumeric")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_live_enabled: bool = True,
    ) -> "MondayUploadConfig":
        """Load the write binding only after a separate explicit enable switch."""

        values = os.environ if environ is None else environ
        if (
            require_live_enabled
            and values.get(UPLOAD_ENABLED_VARIABLE, "").casefold() != "true"
        ):
            raise WorkflowError(
                f"Monday upload requires {UPLOAD_ENABLED_VARIABLE}=true"
            )
        raw_columns = values.get("WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS", "")
        try:
            columns = json.loads(raw_columns)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                "WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS must be JSON"
            ) from exc
        if not isinstance(columns, dict):
            raise WorkflowError("Monday document columns must be a JSON object")
        return cls(
            api_token=values.get("MONDAY_API_TOKEN", ""),
            tenant_id=values.get("WORKFLOWOS_TENANT_ID", ""),
            expected_board_id=values.get("WORKFLOWOS_MONDAY_BOARD_ID", ""),
            document_columns=columns,
        )


class MondayGuardedUploader:
    """Publish one verified PDF into the exact item and column it belongs to."""

    def __init__(
        self,
        config: MondayUploadConfig,
        *,
        graphql_reader: GraphqlReader | None = None,
        file_uploader: FileUploader | None = None,
    ) -> None:
        self._config = config
        self._graphql_reader = graphql_reader or self._post_read_query
        self._file_uploader = file_uploader or self._post_file

    def tenant_binding(self) -> dict[str, Any]:
        """Return the non-secret board/column binding used by the uploader."""

        return {
            "tenant_id": self._config.tenant_id,
            "expected_board_id": self._config.expected_board_id,
            "document_columns": dict(self._config.document_columns),
        }

    def verify_binding(self, item_id: str) -> dict[str, Any]:
        """Check the token and the board binding without uploading anything."""

        normalized = _require_numeric(item_id, "Monday item id")
        board_id = self._resolve_item_board(normalized)
        return {
            "status": "ok",
            "tenant_id": self._config.tenant_id,
            "item_id": normalized,
            "board_id": board_id,
            "uploaded": False,
        }

    def publisher(self) -> Callable[[str, str, str, bytes], dict[str, Any]]:
        """Return the publisher callable expected by MeraviqaDocumentArchive."""

        return self.publish_pdf

    def publish_pdf(
        self, item_id: str, column_id: str, filename: str, content: bytes
    ) -> dict[str, Any]:
        """Upload one PDF after validating the target, the column and the bytes."""

        normalized_item_id = _require_numeric(item_id, "Monday item id")
        normalized_column_id = _require_string(column_id, "Monday column id")
        if normalized_column_id not in self._config.document_columns:
            raise WorkflowError("Monday column is not configured for documents")
        normalized_filename = _require_string(filename, "Monday upload filename")
        if not SAFE_UPLOAD_FILENAME_RE.fullmatch(normalized_filename):
            raise WorkflowError("Monday upload filename must be a simple PDF name")
        if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
            raise WorkflowError("Monday upload content must be a PDF")
        if not content or len(content) > self._config.max_file_bytes:
            raise WorkflowError("Monday upload exceeds the configured size limit")

        board_id = self._resolve_item_board(normalized_item_id)
        content_sha256 = hashlib.sha256(content).hexdigest()
        response = self._file_uploader(
            _add_file_mutation(normalized_item_id, normalized_column_id),
            normalized_item_id,
            normalized_column_id,
            content,
            normalized_filename,
        )
        asset = _require_mapping(
            _require_mapping(response, "Monday upload response").get(
                "add_file_to_column"
            ),
            "Monday add_file_to_column",
        )
        asset_id = _require_numeric(
            str(asset.get("id", "")), "Monday uploaded asset id"
        )
        _validate_stored_size(asset.get("file_size"), len(content))
        return {
            "item_id": normalized_item_id,
            "column_id": normalized_column_id,
            "board_id": board_id,
            "asset_id": asset_id,
            "content_sha256": content_sha256,
            "transmitted_bytes": len(content),
        }

    def _resolve_item_board(self, item_id: str) -> str:
        data = self._graphql_reader(
            READ_ITEM_BOARD_QUERY,
            {"itemIds": [item_id]},
            self._config.api_token,
        )
        items = _require_mapping(data, "Monday GraphQL response").get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise WorkflowError("Monday item was not resolved exactly once")
        item = _require_mapping(items[0], "Monday item")
        if str(item.get("id", "")) != item_id:
            raise WorkflowError("Monday item id does not match the request")
        board = _require_mapping(item.get("board"), "Monday item board")
        board_id = str(board.get("id", ""))
        if board_id != self._config.expected_board_id:
            raise WorkflowError("Monday item belongs to another board")
        return board_id

    def _post_read_query(
        self, query: str, variables: dict[str, Any], token: str
    ) -> dict[str, Any]:
        if not query.lstrip().startswith("query ") or "mutation" in query.casefold():
            raise WorkflowError("Monday binding check accepts read-only queries only")
        request = urllib.request.Request(
            self._config.api_url,
            data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
            headers={
                "Authorization": token,
                "Content-Type": "application/json",
                "User-Agent": "MERAVIQA-Monday-Upload/1.0",
            },
            method="POST",
        )
        return _open_json(request, self._config.api_url)

    def _post_file(
        self,
        mutation: str,
        item_id: str,
        column_id: str,
        content: bytes,
        filename: str,
    ) -> dict[str, Any]:
        del item_id, column_id
        if not mutation.lstrip().startswith("mutation "):
            raise WorkflowError("Monday upload accepts the add_file mutation only")
        body, content_type = _multipart_upload_body(mutation, content, filename)
        request = urllib.request.Request(
            self._config.file_api_url,
            data=body,
            headers={
                "Authorization": self._config.api_token,
                "Content-Type": content_type,
                "User-Agent": "MERAVIQA-Monday-Upload/1.0",
            },
            method="POST",
        )
        return _open_json(request, self._config.file_api_url)


def _open_json(request: urllib.request.Request, expected_url: str) -> dict[str, Any]:
    """Post an authenticated request that must not change destination."""

    opener = urllib.request.build_opener(_RejectApiRedirectHandler())
    try:
        with opener.open(request, timeout=60) as response:
            if response.geturl() != expected_url:
                raise WorkflowError("Monday API response URL changed")
            payload = json.load(response)
    except WorkflowError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkflowError("Monday API request failed") from exc
    if not isinstance(payload, dict) or payload.get("errors"):
        raise WorkflowError("Monday API returned errors")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise WorkflowError("Monday API response has no data")
    return data


def _multipart_upload_body(
    mutation: str, content: bytes, filename: str
) -> tuple[bytes, str]:
    """Build a GraphQL multipart request without touching the PDF bytes."""

    boundary = _unused_boundary(content)
    variables = json.dumps({"file": None})
    marker = f"--{boundary}\r\n".encode("utf-8")
    parts = [
        marker,
        b'Content-Disposition: form-data; name="query"\r\n\r\n',
        mutation.encode("utf-8"),
        b"\r\n",
        marker,
        b'Content-Disposition: form-data; name="variables"\r\n\r\n',
        variables.encode("utf-8"),
        b"\r\n",
        marker,
        b'Content-Disposition: form-data; name="map"\r\n\r\n',
        b'{"image":"variables.file"}',
        b"\r\n",
        marker,
        (
            'Content-Disposition: form-data; name="image"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8"),
        b"Content-Type: application/pdf\r\n\r\n",
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _unused_boundary(content: bytes) -> str:
    for _ in range(8):
        boundary = f"MERAVIQA{secrets.token_hex(24)}"
        if boundary.encode("utf-8") not in content:
            return boundary
    raise WorkflowError("Monday upload boundary could not be generated")


def _validate_stored_size(reported: Any, transmitted: int) -> None:
    """Compare Monday's stored size with the transmitted length when reported."""

    if reported is None:
        return
    try:
        stored = int(reported)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("Monday upload file size is not numeric") from exc
    if stored != transmitted:
        raise WorkflowError("Monday stored a different number of bytes")


def _require_numeric(value: str, field_name: str) -> str:
    normalized = _require_string(value, field_name)
    if not normalized.isdigit():
        raise WorkflowError(f"{field_name} must be numeric")
    return normalized
