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


if __name__ == "__main__":
    unittest.main()
