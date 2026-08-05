"""Read-only Monday asset collection for verified document delivery.

The collector never mutates Monday and never persists presigned asset URLs.  A
stable locator contains only the board, item, column, and asset identifiers; the
short-lived download URL is resolved again immediately before an email adapter
loads the attachment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .automation import SUPPORTED_DOCUMENT_TYPES
from .core import WorkflowError, _require_mapping, _require_string
from .hostpoint_email import EmailAttachment


MONDAY_API_URL = "https://api.monday.com/v2"
DEFAULT_ASSET_HOSTS = (
    "prod-euc1-files-monday-com.s3.eu-central-1.amazonaws.com",
)
MAX_MONDAY_FILE_BYTES = 10 * 1024 * 1024
LOCATOR_RE = re.compile(
    r"^monday-asset://(?P<board>\d+)/(?P<item>\d+)/"
    r"(?P<column>[A-Za-z0-9_]+)/(?P<asset>\d+)$"
)

READ_ITEM_FILE_QUERY = """
query MeraviqaReadOnlyItemFiles($itemIds: [ID!]!, $columnIds: [String!]) {
  items(ids: $itemIds) {
    id
    board { id }
    column_values(ids: $columnIds) {
      id
      type
      ... on FileValue {
        files {
          __typename
          ... on FileAssetValue {
            asset_id
            asset_name: name
            asset {
              id
              name
              file_extension
              file_size
              public_url
            }
          }
          ... on FileAssetInvalidValue {
            invalid_asset_id: asset_id
            invalid_name: name
            error
          }
          ... on FileLinkValue {
            linked_file_id: file_id
            linked_name: name
            linked_url: url
          }
        }
      }
    }
  }
}
""".strip()


@dataclass(frozen=True)
class MondayReadOnlyConfig:
    """Tenant-specific Monday identifiers and a secret API token."""

    api_token: str = field(repr=False)
    expected_board_id: str
    document_columns: Mapping[str, str]
    allowed_asset_hosts: tuple[str, ...] = DEFAULT_ASSET_HOSTS
    api_url: str = MONDAY_API_URL
    max_file_bytes: int = MAX_MONDAY_FILE_BYTES

    def __post_init__(self) -> None:
        if not self.api_token or any(char in self.api_token for char in "\r\n"):
            raise WorkflowError("Monday API token is required")
        if not self.expected_board_id.isdigit():
            raise WorkflowError("Monday board id must be numeric")
        if self.api_url != MONDAY_API_URL:
            raise WorkflowError("Monday API endpoint must be https://api.monday.com/v2")
        if self.max_file_bytes <= 0 or self.max_file_bytes > MAX_MONDAY_FILE_BYTES:
            raise WorkflowError("Monday file limit must be between 1 byte and 10 MB")

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

        if not self.allowed_asset_hosts:
            raise WorkflowError("At least one Monday asset host is required")
        for host in self.allowed_asset_hosts:
            if (
                host != host.casefold()
                or "/" in host
                or ":" in host
                or not host.strip()
            ):
                raise WorkflowError("Monday asset hosts must be lowercase hostnames")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "MondayReadOnlyConfig":
        values = os.environ if environ is None else environ
        raw_columns = values.get("WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS", "")
        try:
            columns = json.loads(raw_columns)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                "WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS must be JSON"
            ) from exc
        if not isinstance(columns, dict):
            raise WorkflowError("Monday document columns must be a JSON object")
        raw_hosts = values.get("WORKFLOWOS_MONDAY_ASSET_HOSTS", "")
        hosts = tuple(
            host.strip().casefold() for host in raw_hosts.split(",") if host.strip()
        ) or DEFAULT_ASSET_HOSTS
        return cls(
            api_token=values.get("MONDAY_API_TOKEN", ""),
            expected_board_id=values.get("WORKFLOWOS_MONDAY_BOARD_ID", ""),
            document_columns=columns,
            allowed_asset_hosts=hosts,
        )


@dataclass(frozen=True)
class DownloadedAsset:
    content: bytes
    final_url: str
    content_type: str | None = None


@dataclass(frozen=True)
class _AssetRecord:
    board_id: str
    item_id: str
    column_id: str
    asset_id: str
    filename: str
    public_url: str = field(repr=False)
    advertised_size: int

    @property
    def locator(self) -> str:
        return (
            f"monday-asset://{self.board_id}/{self.item_id}/"
            f"{self.column_id}/{self.asset_id}"
        )


GraphqlTransport = Callable[[str, dict[str, Any], str], dict[str, Any]]
DownloadTransport = Callable[[str, int], DownloadedAsset]


class _ApprovedAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect target before urllib opens the next connection."""

    def __init__(self, validate_url: Callable[[str], None]) -> None:
        super().__init__()
        self._validate_url = validate_url

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> urllib.request.Request | None:
        self._validate_url(new_url)
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


class _RejectApiRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent an authenticated Monday API request from changing destination."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        raise WorkflowError("Monday API redirects are not allowed")


class MondayReadOnlyCollector:
    """Resolve one exact Monday file-column asset without any write operation."""

    def __init__(
        self,
        config: MondayReadOnlyConfig,
        *,
        graphql_transport: GraphqlTransport | None = None,
        download_transport: DownloadTransport | None = None,
    ) -> None:
        self._config = config
        self._graphql_transport = graphql_transport or self._post_graphql
        self._download_transport = download_transport or self._download

    def tenant_binding(self) -> dict[str, Any]:
        """Return the non-secret board/column binding used by the collector."""

        return {
            "expected_board_id": self._config.expected_board_id,
            "document_columns": dict(self._config.document_columns),
        }

    def collect_file_event(
        self,
        *,
        item_id: str,
        case_id: str,
        column_id: str,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a sanitized event after resolving and hashing one exact PDF."""

        normalized_item_id = self._require_numeric(item_id, "Monday item id")
        normalized_case_id = _require_string(case_id, "case_id")
        record = self._fetch_asset(normalized_item_id, column_id, asset_id)
        attachment = self._download_record(record)
        attachment_ref = hashlib.sha256(attachment.content).hexdigest()
        event_payload = {
            "board_id": record.board_id,
            "item_id": record.item_id,
            "column_id": record.column_id,
            "asset_id": record.asset_id,
            "attachment_ref_sha256": attachment_ref,
        }
        event_ref = hashlib.sha256(
            json.dumps(
                event_payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        return {
            "source": "monday",
            "event_type": "file_column_changed",
            "board_id": record.board_id,
            "item_id": record.item_id,
            "case_id": normalized_case_id,
            "column_id": record.column_id,
            "event_ref_sha256": event_ref,
            "attachment_ref_sha256": attachment_ref,
            "asset_locator": record.locator,
        }

    def load_attachment(self, locator: str) -> EmailAttachment:
        """Re-resolve a stable locator so expired URLs are never persisted."""

        match = LOCATOR_RE.fullmatch(locator)
        if match is None:
            raise WorkflowError("Monday asset locator is invalid")
        parts = match.groupdict()
        if parts["board"] != self._config.expected_board_id:
            raise WorkflowError("Monday asset locator board does not match config")
        record = self._fetch_asset(
            parts["item"], parts["column"], parts["asset"]
        )
        return self._download_record(record)

    def _fetch_asset(
        self, item_id: str, column_id: str, asset_id: str | None
    ) -> _AssetRecord:
        document_type = self._config.document_columns.get(column_id)
        if document_type is None:
            raise WorkflowError("Monday column is not configured for documents")
        del document_type
        if asset_id is not None:
            asset_id = self._require_numeric(asset_id, "Monday asset id")

        response = self._graphql_transport(
            READ_ITEM_FILE_QUERY,
            {"itemIds": [item_id], "columnIds": [column_id]},
            self._config.api_token,
        )
        data = _require_mapping(response, "Monday GraphQL response")
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise WorkflowError("Monday item could not be resolved uniquely")
        item = _require_mapping(items[0], "Monday item")
        if _require_string(item.get("id"), "Monday item id") != item_id:
            raise WorkflowError("Monday returned a different item")
        board = _require_mapping(item.get("board"), "Monday item board")
        board_id = _require_string(board.get("id"), "Monday item board id")
        if board_id != self._config.expected_board_id:
            raise WorkflowError("Monday item belongs to a different board")

        values = item.get("column_values")
        if not isinstance(values, list) or len(values) != 1:
            raise WorkflowError("Monday file column could not be resolved uniquely")
        value = _require_mapping(values[0], "Monday file column")
        if value.get("id") != column_id or value.get("type") != "file":
            raise WorkflowError("Monday returned a different or non-file column")
        files = value.get("files")
        if not isinstance(files, list):
            raise WorkflowError("Monday file column payload is invalid")

        candidates = [
            _require_mapping(entry, "Monday file")
            for entry in files
            if isinstance(entry, dict) and entry.get("__typename") == "FileAssetValue"
        ]
        if asset_id is not None:
            candidates = [entry for entry in candidates if entry.get("asset_id") == asset_id]
        if len(candidates) != 1:
            raise WorkflowError(
                "Monday file must resolve to exactly one uploaded asset"
            )

        selected = candidates[0]
        selected_asset_id = self._require_numeric(
            _require_string(selected.get("asset_id"), "Monday asset id"),
            "Monday asset id",
        )
        asset = _require_mapping(selected.get("asset"), "Monday asset")
        if _require_string(asset.get("id"), "Monday asset id") != selected_asset_id:
            raise WorkflowError("Monday file and asset identifiers do not match")
        filename = _require_string(asset.get("name"), "Monday asset name")
        extension = _require_string(
            asset.get("file_extension"), "Monday asset extension"
        )
        if extension.casefold() not in {"pdf", ".pdf"} or not filename.casefold().endswith(
            ".pdf"
        ):
            raise WorkflowError("Monday document must be a PDF")
        size = asset.get("file_size")
        if not isinstance(size, int) or size <= 0:
            raise WorkflowError("Monday asset size is invalid")
        if size > self._config.max_file_bytes:
            raise WorkflowError("Monday document exceeds the 10 MB file limit")
        public_url = _require_string(asset.get("public_url"), "Monday asset URL")
        self._validate_asset_url(public_url)
        return _AssetRecord(
            board_id=board_id,
            item_id=item_id,
            column_id=column_id,
            asset_id=selected_asset_id,
            filename=filename,
            public_url=public_url,
            advertised_size=size,
        )

    def _download_record(self, record: _AssetRecord) -> EmailAttachment:
        downloaded = self._download_transport(
            record.public_url, self._config.max_file_bytes
        )
        if not isinstance(downloaded, DownloadedAsset):
            raise WorkflowError("Monday download transport returned an invalid result")
        self._validate_asset_url(downloaded.final_url)
        content = downloaded.content
        if not isinstance(content, bytes) or not content:
            raise WorkflowError("Monday document download is empty")
        if len(content) > self._config.max_file_bytes:
            raise WorkflowError("Monday document exceeds the 10 MB file limit")
        if len(content) != record.advertised_size:
            raise WorkflowError("Monday document size changed during download")
        if not content.startswith(b"%PDF-"):
            raise WorkflowError("Monday document content is not a PDF")
        content_type = (downloaded.content_type or "").split(";", 1)[0].casefold()
        if content_type and content_type not in {
            "application/pdf",
            "application/octet-stream",
        }:
            raise WorkflowError("Monday document content type is not PDF")
        return EmailAttachment(
            filename=record.filename,
            content=content,
            content_type="application/pdf",
        )

    def _validate_asset_url(self, url: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise WorkflowError("Monday asset URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.hostname not in self._config.allowed_asset_hosts
        ):
            raise WorkflowError("Monday asset URL host is not approved")

    def _post_graphql(
        self, query: str, variables: dict[str, Any], token: str
    ) -> dict[str, Any]:
        if not query.lstrip().startswith("query ") or "mutation" in query.casefold():
            raise WorkflowError("Monday collector accepts read-only queries only")
        request = urllib.request.Request(
            self._config.api_url,
            data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
            headers={
                "Authorization": token,
                "Content-Type": "application/json",
                "User-Agent": "MERAVIQA-Monday-ReadOnly/1.0",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_RejectApiRedirectHandler())
        try:
            with opener.open(request, timeout=30) as response:
                if response.geturl() != self._config.api_url:
                    raise WorkflowError("Monday API response URL changed")
                payload = json.load(response)
        except WorkflowError:
            raise
        except (OSError, ValueError) as exc:
            raise WorkflowError("Monday read-only API request failed") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise WorkflowError("Monday read-only API returned errors")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WorkflowError("Monday read-only API response has no data")
        return data

    def _download(self, url: str, max_bytes: int) -> DownloadedAsset:
        request = urllib.request.Request(
            url, headers={"User-Agent": "MERAVIQA-Monday-ReadOnly/1.0"}
        )
        opener = urllib.request.build_opener(
            _ApprovedAssetRedirectHandler(self._validate_asset_url)
        )
        try:
            with opener.open(request, timeout=30) as response:
                content = response.read(max_bytes + 1)
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type")
        except OSError as exc:
            raise WorkflowError("Monday document download failed") from exc
        return DownloadedAsset(content, final_url, content_type)

    @staticmethod
    def _require_numeric(value: str, field: str) -> str:
        normalized = _require_string(value, field)
        if not normalized.isdigit():
            raise WorkflowError(f"{field} must be numeric")
        return normalized
