"""Verify the upload body on a real socket, as a standards-compliant server sees it.

The uploader's transport is otherwise only exercised through an injected
callable, so the bytes it actually puts on the wire were never parsed back.
These tests post the real body to a local HTTP server and reassemble it with
the standard library's MIME parser, which is the same shape of parser a
GraphQL multipart endpoint runs.

This proves the request is well formed.  It does not prove monday.com accepts
it: only a real upload with a real token can do that.
"""

from __future__ import annotations

import email.parser
import email.policy
import hashlib
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from workflowos.monday_uploads import _add_file_mutation, _multipart_upload_body


PDF = b"%PDF-1.7\nTAG collaudo\n%%EOF\n"


class _CapturingHandler(BaseHTTPRequestHandler):
    """Accept one POST, keep the raw body and headers, answer like Monday."""

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers["Content-Length"])
        self.server.captured_body = self.rfile.read(length)
        self.server.captured_type = self.headers["Content-Type"]
        self.server.captured_auth = self.headers.get("Authorization")
        payload = json.dumps(
            {
                "data": {
                    "add_file_to_column": {
                        "id": "9001",
                        "name": "TAG.pdf",
                        "file_size": len(PDF),
                    }
                }
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        del args


class MondayUploadWireFormatTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
        self.server.captured_body = None
        self.server.captured_type = None
        self.server.captured_auth = None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d/v2/file" % self.server.server_address[1]

    def post_upload(self, content=PDF, filename="TAG.pdf"):
        """Send the real body with the real headers over a real socket."""

        mutation = _add_file_mutation("3141537766", "file_mm0355a1")
        body, content_type = _multipart_upload_body(mutation, content, filename)
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": "test-token",
                "Content-Type": content_type,
                "User-Agent": "MERAVIQA-Monday-Upload/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def parsed_parts(self):
        """Reassemble the captured body the way a receiving server would."""

        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: "
            + self.server.captured_type.encode("utf-8")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + self.server.captured_body
        )
        self.assertTrue(message.is_multipart(), "body is not multipart")
        parts = {}
        for part in message.iter_parts():
            disposition = part["Content-Disposition"]
            name = disposition.params["name"]
            parts[name] = part
        return parts

    def test_server_receives_four_named_parts(self):
        self.post_upload()
        parts = self.parsed_parts()

        self.assertEqual(
            sorted(parts), ["image", "map", "query", "variables"]
        )

    def test_query_part_carries_the_exact_mutation(self):
        self.post_upload()
        parts = self.parsed_parts()

        received = parts["query"].get_payload(decode=True).decode("utf-8")
        self.assertEqual(
            received, _add_file_mutation("3141537766", "file_mm0355a1")
        )
        self.assertIn("item_id: 3141537766", received)
        self.assertIn('column_id: "file_mm0355a1"', received)

    def test_map_binds_the_file_part_to_the_file_variable(self):
        self.post_upload()
        parts = self.parsed_parts()

        mapping = json.loads(parts["map"].get_payload(decode=True))
        variables = json.loads(parts["variables"].get_payload(decode=True))
        self.assertEqual(mapping, {"image": "variables.file"})
        self.assertEqual(variables, {"file": None})
        # The map names a part that is actually present.
        self.assertIn(next(iter(mapping)), parts)

    def test_pdf_arrives_byte_identical(self):
        self.post_upload()
        parts = self.parsed_parts()

        received = parts["image"].get_payload(decode=True)
        self.assertEqual(received, PDF)
        self.assertEqual(
            hashlib.sha256(received).hexdigest(), hashlib.sha256(PDF).hexdigest()
        )
        self.assertEqual(
            parts["image"]["Content-Disposition"].params["filename"], "TAG.pdf"
        )
        self.assertEqual(parts["image"].get_content_type(), "application/pdf")

    def test_binary_pdf_with_crlf_and_boundary_like_bytes_survives(self):
        awkward = b"%PDF-1.7\r\n--MERAVIQA\r\n\x00\x01\x02binary\xff\xfe\r\n%%EOF\r\n"
        self.post_upload(content=awkward)
        parts = self.parsed_parts()

        self.assertEqual(parts["image"].get_payload(decode=True), awkward)

    def test_authorization_header_reaches_the_server(self):
        self.post_upload()

        self.assertEqual(self.server.captured_auth, "test-token")
        self.assertTrue(
            self.server.captured_type.startswith("multipart/form-data; boundary=")
        )

    def test_response_is_parsed_into_the_asset_confirmation(self):
        payload = self.post_upload()

        asset = payload["data"]["add_file_to_column"]
        self.assertEqual(asset["id"], "9001")
        self.assertEqual(asset["file_size"], len(PDF))


if __name__ == "__main__":
    unittest.main()
