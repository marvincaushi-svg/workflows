"""WorkflowOS: evidence-driven workflow evaluation with verifiable audit logs."""

from .automation import handle_monday_file_event, initialize_automation_state
from .audit import build_audit_log, read_audit_log, verify_audit_log, write_audit_log
from .control_plane import (
    AutomationCatalogError,
    build_event_execution_plan,
    build_health_report,
    build_schedule_execution_plan,
    normalize_runtime_state,
    record_execution_outcome,
    resolve_communication_policy,
    validate_automation_catalog,
)
from .core import WorkflowError, build_case_from_email, evaluate_case, load_document
from .hostpoint_email import (
    EmailAttachment,
    HostpointSmtpConfig,
    HostpointSmtpSender,
    check_hostpoint_connection,
)
from .monday_assets import MondayReadOnlyCollector, MondayReadOnlyConfig
from .monday_pipeline import (
    DeliveryReconciliation,
    DurableMondayEmailPipeline,
    MondayFileChange,
    inspect_delivery_outbox,
    reconcile_delivery_state,
)
from .technical_review import (
    configure_sb_document_handoff,
    create_remediation_plan,
    create_remediation_work,
    evaluate_technical_review,
    record_monday_document_notification,
    record_sb_email_delivery,
)

__all__ = [
    "AutomationCatalogError",
    "EmailAttachment",
    "HostpointSmtpConfig",
    "HostpointSmtpSender",
    "WorkflowError",
    "build_audit_log",
    "build_case_from_email",
    "build_event_execution_plan",
    "build_health_report",
    "build_schedule_execution_plan",
    "check_hostpoint_connection",
    "configure_sb_document_handoff",
    "create_remediation_plan",
    "create_remediation_work",
    "evaluate_case",
    "evaluate_technical_review",
    "handle_monday_file_event",
    "initialize_automation_state",
    "MondayReadOnlyCollector",
    "MondayReadOnlyConfig",
    "DurableMondayEmailPipeline",
    "DeliveryReconciliation",
    "MondayFileChange",
    "inspect_delivery_outbox",
    "reconcile_delivery_state",
    "load_document",
    "normalize_runtime_state",
    "read_audit_log",
    "record_execution_outcome",
    "record_monday_document_notification",
    "record_sb_email_delivery",
    "resolve_communication_policy",
    "validate_automation_catalog",
    "verify_audit_log",
    "write_audit_log",
]

__version__ = "0.2.0"
