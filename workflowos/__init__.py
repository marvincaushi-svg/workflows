"""WorkflowOS: evidence-driven workflow evaluation with verifiable audit logs."""

from .audit import build_audit_log, read_audit_log, verify_audit_log, write_audit_log
from .core import WorkflowError, build_case_from_email, evaluate_case, load_document
from .technical_review import evaluate_technical_review

__all__ = [
    "WorkflowError",
    "build_audit_log",
    "build_case_from_email",
    "evaluate_case",
    "evaluate_technical_review",
    "load_document",
    "read_audit_log",
    "verify_audit_log",
    "write_audit_log",
]

__version__ = "0.1.0"
