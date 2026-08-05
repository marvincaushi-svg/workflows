from __future__ import annotations

import hashlib
import unittest

from workflowos.core import WorkflowError
from workflowos.gmail_email import GmailSmtpConfig, GmailSmtpSender
from workflowos.hostpoint_email import EmailAttachment


class FakeSmtp:
    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.events, self.message, self.send_args = [], None, None
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, traceback): return False
    def ehlo(self): self.events.append("ehlo")
    def starttls(self, *, context): self.events.append("starttls")
    def login(self, username, password): self.events.append("login")
    def send_message(self, message, *, from_addr, to_addrs):
        self.events.append("send_message")
        self.message, self.send_args = message, (from_addr, to_addrs)
        return {}


class WorkflowOSGmailEmailTests(unittest.TestCase):
    def setUp(self):
        self.contents = {"asset-tag": b"tag", "asset-ia": b"ia", "asset-schema": b"schema"}
        self.connection = None
        def factory(host, port, **kwargs):
            self.connection = FakeSmtp(host, port, **kwargs)
            return self.connection
        self.factory = factory
        self.config = GmailSmtpConfig(
            password="app-password",
            tenant_id="tenant-pilot-001",
            username="automation@example.invalid",
            from_address="automation@example.invalid",
            from_name="Pilot Installations AG",
            organization_role="pilot_installer",
        )
        self.request = {
            "source": "workflowos",
            "tenant_id": "tenant-pilot-001",
            "from_organization_role": "pilot_installer",
            "recipient_email": "verified-sb@example.invalid",
            "case_id": "case-001",
            "template": "accepted_grid_documents",
            "attachment_document_types": ["tag_grid_connection_application", "installation_notice_ia", "single_line_diagram"],
            "attachment_locators": list(self.contents),
            "attachment_refs_sha256": [hashlib.sha256(value).hexdigest() for value in self.contents.values()],
            "idempotency_key": "a" * 64,
        }

    def sender(self, *, loader=None):
        return GmailSmtpSender(
            self.config,
            loader or (lambda locator: EmailAttachment(filename=f"{locator}.pdf", content=self.contents[locator])),
            smtp_factory=self.factory,
        )

    def test_sends_three_verified_monday_attachments_with_starttls(self):
        result = self.sender()(self.request)
        self.assertEqual(self.connection.events, ["ehlo", "starttls", "ehlo", "login", "send_message"])
        self.assertEqual(self.connection.send_args, ("automation@example.invalid", ["verified-sb@example.invalid"]))
        self.assertEqual(len(list(self.connection.message.iter_attachments())), 3)
        self.assertEqual(result["provider"], "gmail_smtp")

    def test_hash_mismatch_blocks_before_smtp(self):
        with self.assertRaisesRegex(WorkflowError, "hash does not match Monday"):
            self.sender(loader=lambda locator: EmailAttachment(filename="wrong.pdf", content=b"wrong"))(self.request)
        self.assertIsNone(self.connection)

    def test_duplicate_reference_is_blocked(self):
        request = dict(self.request)
        request["attachment_refs_sha256"] = [self.request["attachment_refs_sha256"][0]] * 3
        with self.assertRaisesRegex(WorkflowError, "Duplicate Monday attachment"):
            self.sender()(request)
        self.assertIsNone(self.connection)

    def test_total_size_over_20_mb_is_blocked(self):
        large = b"x" * (20 * 1024 * 1024 + 1)
        request = dict(self.request)
        request["attachment_document_types"] = ["single_line_diagram"]
        request["attachment_locators"] = ["large"]
        request["attachment_refs_sha256"] = [hashlib.sha256(large).hexdigest()]
        with self.assertRaisesRegex(WorkflowError, "20 MB"):
            self.sender(loader=lambda locator: EmailAttachment(filename="large.pdf", content=large))(request)
        self.assertIsNone(self.connection)

    def test_live_configuration_requires_explicit_switch(self):
        with self.assertRaisesRegex(WorkflowError, "LIVE_ENABLED=true"):
            GmailSmtpConfig.from_environment({"GMAIL_SMTP_PASSWORD": "secret"})

    def test_supports_a_different_tenant_without_changing_product_code(self):
        config = GmailSmtpConfig(
            password="tenant-app-password",
            tenant_id="tenant-example-001",
            username="automation@example.invalid",
            from_address="AUTOMATION@example.invalid",
            from_name="Example Installations AG",
            organization_role="example_installations",
        )
        request = dict(self.request)
        request["tenant_id"] = "tenant-example-001"
        request["from_organization_role"] = "example_installations"
        sender = GmailSmtpSender(
            config,
            lambda locator: EmailAttachment(
                filename=f"{locator}.pdf", content=self.contents[locator]
            ),
            smtp_factory=self.factory,
        )

        sender(request)

        self.assertEqual(
            self.connection.send_args,
            ("AUTOMATION@example.invalid", ["verified-sb@example.invalid"]),
        )
        self.assertIn(
            "Example Installations AG",
            self.connection.message.get_body(preferencelist=("plain",)).get_content(),
        )

    def test_tenant_sender_must_match_authenticated_account(self):
        with self.assertRaisesRegex(WorkflowError, "must match"):
            GmailSmtpConfig(
                password="secret",
                tenant_id="tenant-example-001",
                username="tenant@example.invalid",
                from_address="other@example.invalid",
            )

    def test_request_role_must_match_tenant_config(self):
        config = GmailSmtpConfig(
            password="secret",
            tenant_id="tenant-example-001",
            username="tenant@example.invalid",
            from_address="tenant@example.invalid",
            from_name="Tenant AG",
            organization_role="tenant",
        )
        request = dict(self.request)
        request["tenant_id"] = "tenant-example-001"
        with self.assertRaisesRegex(WorkflowError, "tenant config"):
            GmailSmtpSender(config, lambda locator: None)(request)
        self.assertIsNone(self.connection)

    def test_request_tenant_must_match_gmail_config_before_loading_files(self):
        request = dict(self.request)
        request["tenant_id"] = "tenant-other-001"

        with self.assertRaisesRegex(WorkflowError, "tenant does not match"):
            self.sender(loader=lambda locator: self.fail("loader must not run"))(
                request
            )

        self.assertIsNone(self.connection)


if __name__ == "__main__":
    unittest.main()
