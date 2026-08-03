"""Hostpoint SMTP adapter for guarded WorkflowOS email delivery."""

from __future__ import annotations

import hashlib
import os
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from .core import SHA256_RE, WorkflowError, _require_string


HOSTPOINT_SMTP_HOST = "asmtp.mail.hostpoint.ch"
HOSTPOINT_SMTP_STARTTLS_PORT = 587
AF_SMTP_ADDRESS = "marvin.caushi@elektro-af.ch"
SUPPORTED_TEMPLATES = {"accepted_grid_documents", "signed_safety_report"}


@dataclass(frozen=True)
class HostpointSmtpConfig:
    """Validated SMTP settings; the password is deliberately hidden from repr."""

    username: str
    password: str = field(repr=False)
    from_address: str = AF_SMTP_ADDRESS
    from_name: str = "A&F Elektro GmbH"
    host: str = HOSTPOINT_SMTP_HOST
    port: int = HOSTPOINT_SMTP_STARTTLS_PORT
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.host != HOSTPOINT_SMTP_HOST:
            raise WorkflowError("Hostpoint SMTP host must be asmtp.mail.hostpoint.ch")
        if self.port != HOSTPOINT_SMTP_STARTTLS_PORT:
            raise WorkflowError("Hostpoint SMTP must use STARTTLS on port 587")
        if self.username.casefold() != AF_SMTP_ADDRESS:
            raise WorkflowError(
                "Hostpoint SMTP username must be Marvin.Caushi@elektro-af.ch"
            )
        if self.from_address.casefold() != AF_SMTP_ADDRESS:
            raise WorkflowError(
                "Hostpoint SMTP sender must be Marvin.Caushi@elektro-af.ch"
            )
        if not self.password:
            raise WorkflowError("Hostpoint SMTP password is required")
        if not self.from_name.strip():
            raise WorkflowError("Hostpoint SMTP sender name is required")
        if self.timeout_seconds <= 0:
            raise WorkflowError("Hostpoint SMTP timeout must be positive")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_live_enabled: bool = True,
    ) -> HostpointSmtpConfig:
        """Load live credentials only after a separate explicit enable switch."""

        values = os.environ if environ is None else environ
        if (
            require_live_enabled
            and values.get("WORKFLOWOS_SMTP_LIVE_ENABLED", "").casefold() != "true"
        ):
            raise WorkflowError(
                "Hostpoint SMTP requires WORKFLOWOS_SMTP_LIVE_ENABLED=true"
            )
        try:
            port = int(
                values.get(
                    "WORKFLOWOS_SMTP_PORT", str(HOSTPOINT_SMTP_STARTTLS_PORT)
                )
            )
        except ValueError as exc:
            raise WorkflowError("WORKFLOWOS_SMTP_PORT must be an integer") from exc
        try:
            timeout = float(values.get("WORKFLOWOS_SMTP_TIMEOUT_SECONDS", "30"))
        except ValueError as exc:
            raise WorkflowError(
                "WORKFLOWOS_SMTP_TIMEOUT_SECONDS must be numeric"
            ) from exc
        return cls(
            username=values.get("WORKFLOWOS_SMTP_USERNAME", ""),
            password=values.get("WORKFLOWOS_SMTP_PASSWORD", ""),
            from_address=values.get("WORKFLOWOS_SMTP_FROM", AF_SMTP_ADDRESS),
            from_name=values.get("WORKFLOWOS_SMTP_FROM_NAME", "A&F Elektro GmbH"),
            host=values.get("WORKFLOWOS_SMTP_HOST", HOSTPOINT_SMTP_HOST),
            port=port,
            timeout_seconds=timeout,
        )


def check_hostpoint_connection(
    config: HostpointSmtpConfig,
    *,
    smtp_factory: SmtpFactory = smtplib.SMTP,
) -> dict[str, str]:
    """Authenticate and issue SMTP NOOP without creating or sending a message."""

    try:
        context = ssl.create_default_context()
        with smtp_factory(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
        ) as smtp:
            _start_tls(smtp, context)
            smtp.login(config.username, config.password)
            status_code, _ = smtp.noop()
    except (OSError, smtplib.SMTPException) as exc:
        raise WorkflowError(
            f"Hostpoint SMTP connection check failed: {type(exc).__name__}"
        ) from exc
    if status_code != 250:
        raise WorkflowError(
            f"Hostpoint SMTP connection check returned status {status_code}"
        )
    return {
        "provider": "hostpoint_smtp",
        "connection_status": "authenticated",
        "sender_email": config.from_address,
        "email_sent": "false",
    }


def send_hostpoint_self_test(
    config: HostpointSmtpConfig,
    *,
    smtp_factory: SmtpFactory = smtplib.SMTP,
) -> dict[str, str]:
    """Send one fixed, attachment-free test from and to the A&F mailbox."""

    message = EmailMessage()
    message["From"] = formataddr((config.from_name, config.from_address))
    message["To"] = AF_SMTP_ADDRESS
    message["Subject"] = "Test WorkflowOS – collegamento email A&F"
    message["Message-ID"] = make_msgid(domain="elektro-af.ch")
    message["X-WorkflowOS-Test"] = "hostpoint-self-test"
    message.set_content(
        "Questa è una email di prova inviata da WorkflowOS tramite Hostpoint.\n\n"
        "Mittente e destinatario: Marvin.Caushi@elektro-af.ch\n"
        "Nessun documento cliente è stato allegato.\n"
    )
    message_ref = hashlib.sha256(message.as_bytes()).hexdigest()

    try:
        context = ssl.create_default_context()
        with smtp_factory(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
        ) as smtp:
            _start_tls(smtp, context)
            smtp.login(config.username, config.password)
            refused = smtp.send_message(
                message,
                from_addr=config.from_address,
                to_addrs=[AF_SMTP_ADDRESS],
            )
    except (OSError, smtplib.SMTPException) as exc:
        raise WorkflowError(
            f"Hostpoint SMTP self-test failed: {type(exc).__name__}"
        ) from exc

    if refused:
        raise WorkflowError("Hostpoint SMTP refused the A&F self-test recipient")
    return {
        "provider": "hostpoint_smtp",
        "delivery_status": "sent",
        "sender_email": config.from_address,
        "recipient_email": AF_SMTP_ADDRESS,
        "message_ref_sha256": message_ref,
    }


@dataclass(frozen=True)
class EmailAttachment:
    """Attachment bytes returned by the private Monday asset resolver."""

    filename: str
    content: bytes
    content_type: str = "application/pdf"

    def __post_init__(self) -> None:
        if not self.filename or self.filename in {".", ".."}:
            raise WorkflowError("Email attachment filename is required")
        if any(character in self.filename for character in ("/", "\\", "\r", "\n")):
            raise WorkflowError("Email attachment filename must not contain a path")
        if not isinstance(self.content, bytes) or not self.content:
            raise WorkflowError("Email attachment content must be non-empty bytes")
        parts = self.content_type.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise WorkflowError("Email attachment content_type must be type/subtype")


AttachmentLoader = Callable[[str], EmailAttachment]
SmtpFactory = Callable[..., Any]


def _start_tls(smtp: Any, context: ssl.SSLContext) -> None:
    """Upgrade a plain SMTP connection before sending credentials."""

    smtp.ehlo()
    smtp.starttls(context=context)
    smtp.ehlo()


class HostpointSmtpSender:
    """Callable email adapter compatible with handle_monday_file_event."""

    def __init__(
        self,
        config: HostpointSmtpConfig,
        attachment_loader: AttachmentLoader,
        *,
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        self._config = config
        self._attachment_loader = attachment_loader
        self._smtp_factory = smtp_factory

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        message, recipient_email = self._build_message(request)
        message_bytes = message.as_bytes()
        message_ref = hashlib.sha256(message_bytes).hexdigest()

        try:
            context = ssl.create_default_context()
            with self._smtp_factory(
                self._config.host,
                self._config.port,
                timeout=self._config.timeout_seconds,
            ) as smtp:
                _start_tls(smtp, context)
                smtp.login(self._config.username, self._config.password)
                refused = smtp.send_message(
                    message,
                    from_addr=self._config.from_address,
                    to_addrs=[recipient_email],
                )
        except (OSError, smtplib.SMTPException) as exc:
            raise WorkflowError(
                f"Hostpoint SMTP delivery failed: {type(exc).__name__}"
            ) from exc

        if refused:
            raise WorkflowError("Hostpoint SMTP refused the verified recipient")
        return {
            "source": "email_adapter",
            "provider": "hostpoint_smtp",
            "delivery_status": "sent",
            "recipient_email": recipient_email,
            "message_ref_sha256": message_ref,
        }

    def _build_message(
        self, request: dict[str, Any]
    ) -> tuple[EmailMessage, str]:
        if request.get("source") != "workflowos":
            raise WorkflowError("SMTP request source must be workflowos")
        if request.get("from_organization_role") != "af_elektro":
            raise WorkflowError("SMTP request sender role must be af_elektro")

        recipient_email = _require_string(
            request.get("recipient_email"), "email_request.recipient_email"
        )
        if "\r" in recipient_email or "\n" in recipient_email or "@" not in recipient_email:
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
        if not isinstance(locators, list) or not isinstance(references, list):
            raise WorkflowError("SMTP attachment locators and refs must be lists")
        if not locators or len(locators) != len(references):
            raise WorkflowError("SMTP attachment locators and refs must align")

        subject, body = _render_template(template, case_id)
        message = EmailMessage()
        message["From"] = formataddr(
            (self._config.from_name, self._config.from_address)
        )
        message["To"] = recipient_email
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain="elektro-af.ch")
        message["X-WorkflowOS-Idempotency-Key"] = idempotency_key
        message.set_content(body)

        for locator_value, reference_value in zip(locators, references, strict=True):
            locator = _require_string(locator_value, "email_request.attachment_locator")
            expected_ref = _require_string(
                reference_value, "email_request.attachment_ref_sha256"
            )
            if not SHA256_RE.fullmatch(expected_ref):
                raise WorkflowError("SMTP attachment ref must be SHA-256")
            attachment = self._attachment_loader(locator)
            if not isinstance(attachment, EmailAttachment):
                raise WorkflowError("attachment_loader must return EmailAttachment")
            observed_ref = hashlib.sha256(attachment.content).hexdigest()
            if observed_ref != expected_ref:
                raise WorkflowError("SMTP attachment content hash does not match Monday")
            main_type, sub_type = attachment.content_type.split("/", 1)
            message.add_attachment(
                attachment.content,
                maintype=main_type,
                subtype=sub_type,
                filename=attachment.filename,
            )
        return message, recipient_email


def _render_template(template: str, case_id: str) -> tuple[str, str]:
    if template == "accepted_grid_documents":
        return (
            f"Documentazione pratica accettata – {case_id}",
            "Buongiorno,\n\n"
            "in allegato trasmettiamo TAG, IA e schema relativi alla commessa "
            f"{case_id}.\n\nCordiali saluti\nA&F Elektro GmbH\n",
        )
    return (
        f"RaSi/SiNa impianto ultimato – {case_id}",
        "Buongiorno,\n\n"
        "in allegato trasmettiamo il RaSi/SiNa firmato relativo alla commessa "
        f"{case_id}.\n\nCordiali saluti\nA&F Elektro GmbH\n",
    )
