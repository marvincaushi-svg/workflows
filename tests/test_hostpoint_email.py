from __future__ import annotations

import hashlib
import smtplib
import unittest

from workflowos.core import WorkflowError
from workflowos.hostpoint_email import (
    AF_SMTP_ADDRESS,
    EmailAttachment,
    HostpointSmtpConfig,
    HostpointSmtpSender,
    check_hostpoint_connection,
    send_hostpoint_self_test,
)


class FakeSmtp:
    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.login_args = None
        self.message = None
        self.send_args = None
        self.refused = {}
        self.noop_status = 250
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def login(self, username, password):
        self.events.append("login")
        self.login_args = (username, password)

    def ehlo(self):
        self.events.append("ehlo")
        return 250, b"OK"

    def starttls(self, *, context):
        self.events.append("starttls")
        self.tls_context = context
        return 220, b"Ready to start TLS"

    def send_message(self, message, *, from_addr, to_addrs):
        self.events.append("send_message")
        self.message = message
        self.send_args = (from_addr, to_addrs)
        return self.refused

    def noop(self):
        self.events.append("noop")
        return self.noop_status, b"OK"


class WorkflowOSHostpointEmailTests(unittest.TestCase):
    def setUp(self):
        self.content = b"sanitized test pdf bytes"
        self.attachment_ref = hashlib.sha256(self.content).hexdigest()
        self.connections = []

        def smtp_factory(host, port, **kwargs):
            connection = FakeSmtp(host, port, **kwargs)
            self.connections.append(connection)
            return connection

        self.smtp_factory = smtp_factory
        self.config = HostpointSmtpConfig(
            username="Marvin.Caushi@elektro-af.ch",
            password="local-test-secret",
        )
        self.request = {
            "source": "workflowos",
            "from_organization_role": "af_elektro",
            "recipient_email": "sb-recipient@example.invalid",
            "case_id": "pilot-pv-001",
            "template": "signed_safety_report",
            "attachment_locators": ["monday-asset-1"],
            "attachment_refs_sha256": [self.attachment_ref],
            "idempotency_key": "a" * 64,
        }

    def sender(self, *, content=None):
        attachment_content = self.content if content is None else content
        return HostpointSmtpSender(
            self.config,
            lambda locator: EmailAttachment(
                filename="RaSi-SiNa.pdf",
                content=attachment_content,
            ),
            smtp_factory=self.smtp_factory,
        )

    def test_hostpoint_sender_uses_starttls_login_and_verified_attachment(self):
        confirmation = self.sender()(self.request)

        self.assertEqual(len(self.connections), 1)
        connection = self.connections[0]
        self.assertEqual(connection.host, "asmtp.mail.hostpoint.ch")
        self.assertEqual(connection.port, 587)
        self.assertNotIn("context", connection.kwargs)
        self.assertIsNotNone(connection.tls_context)
        self.assertEqual(
            connection.events,
            ["ehlo", "starttls", "ehlo", "login", "send_message"],
        )
        self.assertEqual(
            connection.login_args,
            ("Marvin.Caushi@elektro-af.ch", "local-test-secret"),
        )
        self.assertEqual(connection.send_args, (AF_SMTP_ADDRESS, [self.request["recipient_email"]]))
        self.assertEqual(len(list(connection.message.iter_attachments())), 1)
        self.assertEqual(confirmation["delivery_status"], "sent")
        self.assertEqual(confirmation["recipient_email"], self.request["recipient_email"])
        self.assertRegex(confirmation["message_ref_sha256"], r"^[0-9a-f]{64}$")

    def test_attachment_hash_mismatch_blocks_before_smtp_connection(self):
        with self.assertRaisesRegex(WorkflowError, "hash does not match Monday"):
            self.sender(content=b"changed document")(self.request)

        self.assertEqual(self.connections, [])

    def test_environment_requires_explicit_live_enable_and_company_sender(self):
        base = {
            "WORKFLOWOS_SMTP_USERNAME": AF_SMTP_ADDRESS,
            "WORKFLOWOS_SMTP_PASSWORD": "secret",
        }
        with self.assertRaisesRegex(WorkflowError, "LIVE_ENABLED=true"):
            HostpointSmtpConfig.from_environment(base)

        mismatched_sender = {
            **base,
            "WORKFLOWOS_SMTP_LIVE_ENABLED": "true",
            "WORKFLOWOS_SMTP_USERNAME": "marvin.caushi@gmail.com",
            "WORKFLOWOS_SMTP_FROM": AF_SMTP_ADDRESS,
        }
        with self.assertRaisesRegex(WorkflowError, "match the authenticated account"):
            HostpointSmtpConfig.from_environment(mismatched_sender)

    def test_supports_a_different_tenant_without_changing_product_code(self):
        config = HostpointSmtpConfig(
            username="automation@example.com",
            password="tenant-secret",
            from_address="automation@example.com",
            from_name="Example Operations AG",
            organization_role="tenant_operations",
        )
        request = {
            **self.request,
            "from_organization_role": "tenant_operations",
        }
        sender = HostpointSmtpSender(
            config,
            lambda locator: EmailAttachment(
                filename="Safety-report.pdf",
                content=self.content,
            ),
            smtp_factory=self.smtp_factory,
        )

        confirmation = sender(request)

        connection = self.connections[0]
        self.assertEqual(
            connection.login_args,
            ("automation@example.com", "tenant-secret"),
        )
        self.assertEqual(
            connection.send_args,
            ("automation@example.com", [request["recipient_email"]]),
        )
        self.assertIn(
            "Example Operations AG",
            connection.message.get_body(preferencelist=("plain",)).get_content(),
        )
        self.assertEqual(confirmation["delivery_status"], "sent")

    def test_request_role_must_match_tenant_config(self):
        request = {**self.request, "from_organization_role": "other_tenant"}

        with self.assertRaisesRegex(WorkflowError, "tenant config"):
            self.sender()(request)

        self.assertEqual(self.connections, [])

    def test_duplicate_reference_is_blocked_before_smtp(self):
        request = {
            **self.request,
            "attachment_locators": ["monday-asset-1", "monday-asset-2"],
            "attachment_refs_sha256": [self.attachment_ref, self.attachment_ref],
        }

        with self.assertRaisesRegex(WorkflowError, "Duplicate Monday attachment"):
            self.sender()(request)

        self.assertEqual(self.connections, [])

    def test_total_size_over_20_mb_is_blocked_before_smtp(self):
        content = b"x" * (20 * 1024 * 1024 + 1)
        request = {
            **self.request,
            "attachment_refs_sha256": [hashlib.sha256(content).hexdigest()],
        }

        with self.assertRaisesRegex(WorkflowError, "20 MB"):
            self.sender(content=content)(request)

        self.assertEqual(self.connections, [])

    def test_connection_check_authenticates_without_creating_email(self):
        result = check_hostpoint_connection(
            self.config,
            smtp_factory=self.smtp_factory,
        )

        self.assertEqual(result["connection_status"], "authenticated")
        self.assertEqual(result["email_sent"], "false")
        self.assertEqual(len(self.connections), 1)
        connection = self.connections[0]
        self.assertEqual(
            connection.login_args,
            ("Marvin.Caushi@elektro-af.ch", "local-test-secret"),
        )
        self.assertIsNone(connection.message)
        self.assertEqual(
            connection.events,
            ["ehlo", "starttls", "ehlo", "login", "noop"],
        )

    def test_smtp_failure_does_not_return_false_delivery_confirmation(self):
        def failing_factory(host, port, **kwargs):
            raise smtplib.SMTPConnectError(421, "test failure")

        sender = HostpointSmtpSender(
            self.config,
            lambda locator: EmailAttachment(
                filename="RaSi-SiNa.pdf",
                content=self.content,
            ),
            smtp_factory=failing_factory,
        )
        with self.assertRaisesRegex(WorkflowError, "SMTPConnectError"):
            sender(self.request)

    def test_self_test_sends_only_to_af_mailbox_without_attachments(self):
        confirmation = send_hostpoint_self_test(
            self.config,
            smtp_factory=self.smtp_factory,
        )

        self.assertEqual(len(self.connections), 1)
        connection = self.connections[0]
        self.assertEqual(
            connection.send_args,
            (AF_SMTP_ADDRESS, [AF_SMTP_ADDRESS]),
        )
        self.assertEqual(connection.message["To"], AF_SMTP_ADDRESS)
        self.assertEqual(connection.message["X-WorkflowOS-Test"], "hostpoint-self-test")
        self.assertEqual(list(connection.message.iter_attachments()), [])
        self.assertEqual(confirmation["delivery_status"], "sent")
        self.assertEqual(confirmation["recipient_email"], AF_SMTP_ADDRESS)

    def test_self_test_failure_does_not_return_false_delivery_confirmation(self):
        def failing_factory(host, port, **kwargs):
            raise smtplib.SMTPConnectError(421, "test failure")

        with self.assertRaisesRegex(WorkflowError, "SMTPConnectError"):
            send_hostpoint_self_test(
                self.config,
                smtp_factory=failing_factory,
            )

    def test_password_is_hidden_from_config_representation(self):
        self.assertNotIn("local-test-secret", repr(self.config))


if __name__ == "__main__":
    unittest.main()
