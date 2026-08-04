from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from workflowos.core import WorkflowError
from workflowos.state_store import (
    load_automation_state,
    lock_automation_state,
    save_automation_state,
)


class AutomationStateStoreTests(unittest.TestCase):
    def test_state_survives_separate_write_and_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {"case_id": "case-1", "processed_events": {"a" * 64: "b" * 64}}
            save_automation_state(path, state)
            self.assertEqual(load_automation_state(path), state)

    def test_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_automation_state(path, {"case_id": "case-1"})
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["state"]["case_id"] = "case-2"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "checksum"):
                load_automation_state(path)

    def test_replacing_existing_state_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_automation_state(path, {"sequence": 1})
            save_automation_state(path, {"sequence": 2})
            self.assertEqual(load_automation_state(path), {"sequence": 2})
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_state_lock_serializes_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_automation_state(path, {"sequence": 0})
            first_entered = threading.Event()
            allow_first_to_leave = threading.Event()
            order = []

            def first_worker():
                with lock_automation_state(path):
                    order.append("first")
                    first_entered.set()
                    allow_first_to_leave.wait(timeout=2)

            def second_worker():
                first_entered.wait(timeout=2)
                with lock_automation_state(path):
                    order.append("second")

            first = threading.Thread(target=first_worker)
            second = threading.Thread(target=second_worker)
            first.start()
            second.start()
            first_entered.wait(timeout=2)
            self.assertEqual(order, ["first"])
            allow_first_to_leave.set()
            first.join(timeout=2)
            second.join(timeout=2)
            self.assertEqual(order, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
