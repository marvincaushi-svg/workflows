"""Tamper-evident, atomic persistence for the Monday automation state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import fcntl

from .core import WorkflowError


STATE_SCHEMA_VERSION = "1.0"


@contextmanager
def lock_automation_state(path: str | Path) -> Iterator[None]:
    """Serialize state transitions for one case across worker processes."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def save_automation_state(path: str | Path, state: dict[str, Any]) -> None:
    """Write state atomically with a checksum covering the canonical payload."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(state)
    envelope = {
        "schema_version": STATE_SCHEMA_VERSION,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "state": state,
    }
    encoded = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_automation_state(path: str | Path) -> dict[str, Any]:
    """Load persisted state and reject corruption or manual modification."""

    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("Automation state cannot be read") from exc
    if not isinstance(envelope, dict):
        raise WorkflowError("Automation state envelope must be an object")
    if envelope.get("schema_version") != STATE_SCHEMA_VERSION:
        raise WorkflowError("Unsupported automation state schema")
    state = envelope.get("state")
    if not isinstance(state, dict):
        raise WorkflowError("Automation state payload must be an object")
    expected = envelope.get("payload_sha256")
    observed = hashlib.sha256(_canonical_json(state)).hexdigest()
    if expected != observed:
        raise WorkflowError("Automation state checksum does not match")
    return state


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowError("Automation state must be JSON serializable") from exc
