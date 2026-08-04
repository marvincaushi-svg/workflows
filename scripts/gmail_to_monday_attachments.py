from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MONDAY_API = "https://api.monday.com/v2"
MONDAY_FILE_API = "https://api.monday.com/v2/file"


@dataclass(frozen=True)
class Config:
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    monday_token: str
    monday_board_id: int
    monday_file_column_id: str
    gmail_query: str
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "GMAIL_CLIENT_ID": os.getenv("GMAIL_CLIENT_ID", ""),
            "GMAIL_CLIENT_SECRET": os.getenv("GMAIL_CLIENT_SECRET", ""),
            "GMAIL_REFRESH_TOKEN": os.getenv("GMAIL_REFRESH_TOKEN", ""),
            "MONDAY_API_TOKEN": os.getenv("MONDAY_API_TOKEN", ""),
            "MONDAY_BOARD_ID": os.getenv("MONDAY_BOARD_ID", ""),
            "MONDAY_FILE_COLUMN_ID": os.getenv("MONDAY_FILE_COLUMN_ID", "file_mm03a3er"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
        return cls(
            gmail_client_id=required["GMAIL_CLIENT_ID"],
            gmail_client_secret=required["GMAIL_CLIENT_SECRET"],
            gmail_refresh_token=required["GMAIL_REFRESH_TOKEN"],
            monday_token=required["MONDAY_API_TOKEN"],
            monday_board_id=int(required["MONDAY_BOARD_ID"]),
            monday_file_column_id=required["MONDAY_FILE_COLUMN_ID"],
            gmail_query=os.getenv(
                "GMAIL_QUERY",
                '(from:(@sbenergetica.ch) OR from:(giada) OR from:(nickyta)) has:attachment filename:pdf newer_than:30d -label:monday-synced',
            ),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        )


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, data: bytes | None = None) -> Any:
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def gmail_access_token(config: Config) -> str:
    body = urllib.parse.urlencode(
        {
            "client_id": config.gmail_client_id,
            "client_secret": config.gmail_client_secret,
            "refresh_token": config.gmail_refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    result = http_json("https://oauth2.googleapis.com/token", method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body)
    return result["access_token"]


def gmail_get(token: str, path: str) -> Any:
    return http_json(f"{GMAIL_API}/{path}", headers={"Authorization": f"Bearer {token}"})


def gmail_modify(token: str, message_id: str, add_labels: list[str]) -> None:
    body = json.dumps({"addLabelIds": add_labels}).encode()
    http_json(
        f"{GMAIL_API}/messages/{message_id}/modify",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=body,
    )


def gmail_label_id(token: str, name: str) -> str:
    labels = gmail_get(token, "labels").get("labels", [])
    for label in labels:
        if label.get("name") == name:
            return label["id"]
    body = json.dumps({"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).encode()
    result = http_json(
        f"{GMAIL_API}/labels",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=body,
    )
    return result["id"]


def decode_header_value(headers: list[dict[str, str]], name: str) -> str:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def pdf_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [payload]
    while stack:
        part = stack.pop()
        stack.extend(part.get("parts", []))
        filename = part.get("filename", "")
        body = part.get("body", {})
        mime = part.get("mimeType", "")
        if filename.lower().endswith(".pdf") and mime == "application/pdf" and body.get("attachmentId"):
            found.append(part)
    return found


def normalized_words(text: str) -> list[str]:
    return [word for word in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", text.lower()) if len(word) >= 4]


def monday_graphql(config: Config, query: str, variables: dict[str, Any]) -> Any:
    result = http_json(
        MONDAY_API,
        method="POST",
        headers={"Authorization": config.monday_token, "Content-Type": "application/json", "API-Version": "2025-10"},
        data=json.dumps({"query": query, "variables": variables}).encode(),
    )
    if result.get("errors"):
        raise RuntimeError(f"Monday GraphQL error: {result['errors']}")
    return result["data"]


def monday_items(config: Config) -> list[dict[str, Any]]:
    query = """
    query ($board: [ID!]!) {
      boards(ids: $board) {
        items_page(limit: 500) { items { id name } }
      }
    }
    """
    data = monday_graphql(config, query, {"board": [str(config.monday_board_id)]})
    return data["boards"][0]["items_page"]["items"]


def match_item(items: list[dict[str, Any]], subject: str, filenames: list[str]) -> dict[str, Any] | None:
    haystack_words = set(normalized_words(" ".join([subject, *filenames])))
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        item_words = set(normalized_words(item["name"]))
        score = len(item_words & haystack_words)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda entry: (entry[0], len(entry[1]["name"])), reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def monday_upload(config: Config, item_id: str, filename: str, content: bytes) -> None:
    boundary = f"----workflowos{int(time.time() * 1000)}"
    query = "mutation ($file: File!) { add_file_to_column(file: $file, item_id: %s, column_id: \"%s\") { id } }" % (
        item_id,
        config.monday_file_column_id,
    )
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"query\"\r\n\r\n{query}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"variables[file]\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(
        MONDAY_FILE_API,
        method="POST",
        headers={"Authorization": config.monday_token, "Content-Type": f"multipart/form-data; boundary={boundary}", "API-Version": "2025-10"},
        data=b"".join(parts),
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Monday upload failed ({exc.code}): {detail}") from exc
    if result.get("errors"):
        raise RuntimeError(f"Monday upload error: {result['errors']}")


def main() -> int:
    config = Config.from_env()
    token = gmail_access_token(config)
    synced_label = gmail_label_id(token, "monday-synced")
    query = urllib.parse.urlencode({"q": config.gmail_query, "maxResults": 50})
    messages = gmail_get(token, f"messages?{query}").get("messages", [])
    items = monday_items(config)
    uploaded = 0
    skipped = 0

    for message_ref in messages:
        message_id = message_ref["id"]
        message = gmail_get(token, f"messages/{message_id}?format=full")
        payload = message["payload"]
        headers = payload.get("headers", [])
        subject = decode_header_value(headers, "Subject")
        parts = pdf_parts(payload)
        filenames = [part["filename"] for part in parts]
        item = match_item(items, subject, filenames)
        if not item:
            print(f"SKIP {message_id}: no unique Monday item match for {subject!r}")
            skipped += 1
            continue
        print(f"MATCH {message_id}: {subject!r} -> {item['name']} ({item['id']})")
        if config.dry_run:
            continue
        for part in parts:
            attachment_id = part["body"]["attachmentId"]
            attachment = gmail_get(token, f"messages/{message_id}/attachments/{attachment_id}")
            content = base64.urlsafe_b64decode(attachment["data"] + "===")
            monday_upload(config, item["id"], part["filename"], content)
            uploaded += 1
            print(f"UPLOADED {part['filename']} -> {item['name']}")
        gmail_modify(token, message_id, [synced_label])

    print(json.dumps({"messages": len(messages), "uploaded": uploaded, "skipped": skipped, "dry_run": config.dry_run}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
