"""Gmail SMTP adapter for verified WorkflowOS Monday attachments."""

from __future__ import annotations

import hashlib
import os
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from .core import SHA256_RE, WorkflowError, _require_string
from .hostpoint_email import AttachmentLoader, EmailAttachment, SmtpFactory, _start_tls


GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_STARTTLS_PORT = 587
DEFAULT_GMAIL_ADDRESS = "marvin.caushi@gmail.com"
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
SUPPORTED_TEMPLATES = {"accepted_grid_documents", "signed_safety_report"}


@dataclass(frozen=True)
class GmailSmtpConfig:
    """Validated Gmail settings; the app password is hidden from repr."""

    password: str = field(repr=False)
    username: str = DEFAULT_GMAIL_ADDRESS
    from_address: str = DEFAULT_GMAIL_ADDRESS
    from_name: str = "A&F Elektro GmbH"
    organization_role: str = "af_elektro"
    host: str = GMAIL_SMTP_HOST
    port: int = GMAIL_SMTP_STARTTLS_PORT
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.host != GMAIL_SMTP_HOST or self.port != GMAIL_SMTP_STARTTLS_PORT:
            raise WorkflowError("Gmail SMTP must use smtp.gmail.com STARTTLS on port 587")
        if "@" not in self.username or any(char in self.username for char in "\r\n"):
            raise WorkflowError("Gmail SMTP username must be a valid email address")
        if self.from_address.casefold() != self.username.casefold():
            raise WorkflowError("Gmail SMTP sender must match the authenticated account")
        if not self.password:
            raise WorkflowError("Gmail app password is required")
        if not self.from_name.strip() or not self.organization_role.strip():
            raise WorkflowError("Gmail sender name and organization role are required")
        if any(char in self.from_name for char in "\r\n"):
            raise WorkflowError("Gmail sender name is invalid")
        if self.timeout_seconds <= 0:
            raise WorkflowError("Gmail timeout must be positive")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_live_enabled: bool = True,
    ) -> "GmailSmtpConfig":
        values = os.environ if environ is None else environ
        if (
            require_live_enabled
            and values.get("WORKFLOWOS_GMAIL_LIVE_ENABLED", "").casefold() != "true"
        ):
            raise WorkflowError("Gmail SMTP requires WORKFLOWOS_GMAIL_LIVE_ENABLED=true")
        try:
            timeout = float(values.get("WORKFLOWOS_GMAIL_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise WorkflowError("WORKFLOWOS_GMAIL_TIMEOUT_SECONDS must be numeric") from exc
        return cls(
            password=values.get("GMAIL_SMTP_PASSWORD", ""),
            username=values.get("WORKFLOWOS_GMAIL_USERNAME", DEFAULT_GMAIL_ADDRESS),
            from_address=values.get("WORKFLOWOS_GMAIL_FROM", DEFAULT_GMAIL_ADDRESS),
            from_name=values.get("WORKFLOWOS_GMAIL_FROM_NAME", "A&F Elektro GmbH"),
            organization_role=values.get(
                "WORKFLOWOS_ORGANIZATION_ROLE", "af_elektro"
            ),
            host=values.get("WORKFLOWOS_GMAIL_HOST", GMAIL_SMTP_HOST),
            port=int(values.get("WORKFLOWOS_GMAIL_PORT", str(GMAIL_SMTP_STARTTLS_PORT))),
            timeout_seconds=timeout,
        )


class GmailSmtpSender:
    """Send a verified WorkflowOS request with the matching Monday attachments."""

    def __init__(
        self,
        config: GmailSmtpConfig,
        attachment_loader: AttachmentLoader,
        *,
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        self._config = config
        self._attachment_loader = attachment_loader
        self._smtp_factory = smtp_factory

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        message, recipient = self._build_message(request)
        message_ref = hashlib.sha256(message.as_bytes()).hexdigest()
        try:
            with self._smtp_factory(
                self._config.host,
                self._config.port,
                timeout=self._config.timeout_seconds,
            ) as smtp:
                _start_tls(smtp, ssl.create_default_context())
                smtp.login(self._config.username, self._config.password)
                refused = smtp.send_message(
                    message,
                    from_addr=self._config.from_address,
                    to_addrs=[recipient],
                )
        except (OSError, smtplib.SMTPException) as exc:
            raise WorkflowError(f"Gmail SMTP delivery failed: {type(exc).__name__}") from exc
        if refused:
            raise WorkflowError("Gmail SMTP refused the verified recipient")
        return {
            "source": "email_adapter",
            "provider": "gmail_smtp",
            "delivery_status": "sent",
            "recipient_email": recipient,
            "message_ref_sha256": message_ref,
        }

    def _build_message(self, request: dict[str, Any]) -> tuple[EmailMessage, str]:
        if request.get("source") != "workflowos":
            raise WorkflowError("SMTP request source must be workflowos")
        if request.get("from_organization_role") != self._config.organization_role:
            raise WorkflowError("SMTP request sender role does not match tenant config")

        recipient = _require_string(
            request.get("recipient_email"), "email_request.recipient_email"
        )
        if "\r" in recipient or "\n" in recipient or "@" not in recipient:
            raise WorkflowError("SMTP recipient email is invalid")
        case_id = _require_string(request.get("case_id"), "email_request.case_id")
        template = _require_string(request.get("template"), "email_request.template")
        if template not in SUPPORTED_TEMPLATES:
            raise WorkflowError(f"Unsupported SMTP template: {template}")
        idempotency_key = _require_string(
            request.get("idempotency_key"), "email_request.idempotency_key"
        )
        if not SHA256_RE.fullmatch(idempotency_key):
            raise WorkflowError("email_request.idempotency_key must be SHA-256")

        locators = request.get("attachment_locators")
        references = request.get("attachment_refs_sha256")
        document_types = request.get("attachment_document_types")
        if not all(isinstance(value, list) for value in (locators, references, document_types)):
            raise WorkflowError("Gmail attachment metadata must be lists")
        if not locators or len(locators) != len(references) or len(locators) != len(document_types):
            raise WorkflowError("Gmail attachment metadata must align")

        attachments: list[EmailAttachment] = []
        seen_refs: set[str] = set()
        seen_names: set[str] = set()
        total_bytes = 0
        for locator_value, reference_value in zip(locators, references, strict=True):
            locator = _require_string(locator_value, "email_request.attachment_locator")
            expected_ref = _require_string(
                reference_value, "email_request.attachment_ref_sha256"
            )
            if not SHA256_RE.fullmatch(expected_ref):
                raise WorkflowError("Gmail attachment ref must be SHA-256")
            if expected_ref in seen_refs:
                raise WorkflowError("Duplicate Monday attachment is not allowed")
            attachment = self._attachment_loader(locator)
            if not isinstance(attachment, EmailAttachment):
                raise WorkflowError("attachment_loader must return EmailAttachment")
            observed_ref = hashlib.sha256(attachment.content).hexdigest()
            if observed_ref != expected_ref:
                raise WorkflowError("Gmail attachment content hash does not match Monday")
            normalized_name = attachment.filename.casefold()
            if normalized_name in seen_names:
                raise WorkflowError("Duplicate attachment filename is not allowed")
            seen_refs.add(expected_ref)
            seen_names.add(normalized_name)
            total_bytes += len(attachment.content)
            if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
                raise WorkflowError("Monday attachments exceed the 20 MB email limit")
            attachments.append(attachment)

        subject, body = _render_template(template, case_id, self._config.from_name)
        message = EmailMessage()
        message["From"] = formataddr((self._config.from_name, self._config.from_address))
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain="gmail.com")
        message["X-WorkflowOS-Idempotency-Key"] = idempotency_key
        message.set_content(body)
        for attachment in attachments:
            main_type, sub_type = attachment.content_type.split("/", 1)
            message.add_attachment(
                attachment.content,
                maintype=main_type,
                subtype=sub_type,
                filename=attachment.filename,
            )
        return message, recipient


def _render_template(template: str, case_id: str, organization_name: str) -> tuple[str, str]:
    if template == "accepted_grid_documents":
        return (
            f"Documentazione pratica accettata – {case_id}",
            "Buongiorno,\n\nin allegato trasmettiamo TAG, IA e schema relativi "
            f"alla commessa {case_id}.\n\nCordiali saluti\n{organization_name}\n",
        )
    return (
        f"RaSi/SiNa impianto ultimato – {case_id}",
        "Buongiorno,\n\nin allegato trasmettiamo il RaSi/SiNa firmato relativo "
        f"alla commessa {case_id}.\n\nCordiali saluti\n{organization_name}\n",
    )
