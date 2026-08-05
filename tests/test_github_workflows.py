from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOWOS_IMPORT_RE = re.compile(
    r"^[ \t]*from\s+(workflowos(?:\.[A-Za-z_]\w*)*)\s+import\s+"
    r"(?:\((?P<multiline>.*?)\)|(?P<single>[^\n]+))",
    re.MULTILINE | re.DOTALL,
)
# An environment variable that turns a guarded adapter into a real sender or
# a real writer, e.g. WORKFLOWOS_SMTP_LIVE_ENABLED or
# WORKFLOWOS_MONDAY_UPLOAD_ENABLED set to true.
LIVE_SWITCH_RE = re.compile(
    r"^[ \t]*\w*(?:LIVE_ENABLED|UPLOAD_ENABLED)[ \t]*:[ \t]*[\"']?true[\"']?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
AUTOMATIC_TRIGGER_RE = re.compile(
    r"^[ \t]{2}(push|pull_request|schedule):[ \t]*$", re.MULTILINE
)
CONFIRMATION_GATE_RE = re.compile(r"if:.*inputs\.confirm\w*\s*==")


class GitHubWorkflowIntegrityTests(unittest.TestCase):
    def test_embedded_workflowos_imports_resolve(self):
        checked_imports = 0

        for workflow_path in sorted(WORKFLOWS.glob("*.yml")):
            source = workflow_path.read_text(encoding="utf-8")
            for match in WORKFLOWOS_IMPORT_RE.finditer(source):
                module_name = match.group(1)
                imported_names = match.group("multiline") or match.group("single")
                module = importlib.import_module(module_name)
                for imported_name in imported_names.split(","):
                    symbol = imported_name.strip()
                    if not symbol:
                        continue
                    checked_imports += 1
                    self.assertTrue(
                        hasattr(module, symbol),
                        f"{workflow_path.name} imports missing {module_name}.{symbol}",
                    )

        self.assertGreater(checked_imports, 0, "no embedded WorkflowOS imports checked")

    def test_workflows_arming_external_writes_need_manual_confirmation(self):
        """A live switch may only be armed by a deliberate human dispatch.

        Parsed as text on purpose: the project has no runtime dependencies and
        CI installs none, so a YAML library must not be required to enforce
        this.
        """

        armed = 0
        for workflow_path in sorted(WORKFLOWS.glob("*.yml")):
            source = workflow_path.read_text(encoding="utf-8")
            if not LIVE_SWITCH_RE.search(source):
                continue
            armed += 1
            name = workflow_path.name

            self.assertIn(
                "workflow_dispatch:",
                source,
                f"{name} arms an external write without a manual dispatch",
            )
            for automatic in AUTOMATIC_TRIGGER_RE.findall(source):
                self.fail(
                    f"{name} arms an external write and still triggers on "
                    f"{automatic.strip()}"
                )
            self.assertRegex(
                source,
                CONFIRMATION_GATE_RE,
                f"{name} arms an external write without a confirmation gate",
            )

        self.assertGreater(armed, 0, "no workflow arming an external write found")

    def test_only_the_test_suite_runs_automatically(self):
        automatic = {
            workflow_path.name
            for workflow_path in sorted(WORKFLOWS.glob("*.yml"))
            if AUTOMATIC_TRIGGER_RE.search(
                workflow_path.read_text(encoding="utf-8")
            )
        }

        self.assertEqual(automatic, {"test.yml"})


if __name__ == "__main__":
    unittest.main()
