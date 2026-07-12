import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT / "pyproject.toml"
LICENSE_PATH = ROOT / "LICENSE"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RUFF_VERSION = "0.15.20"


class StaticToolingContractTests(unittest.TestCase):
    def test_project_declares_and_ships_apache_license(self):
        pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
        license_text = LICENSE_PATH.read_text(encoding="utf-8")

        self.assertRegex(pyproject, r'(?m)^requires = \["setuptools>=77"\]$')
        self.assertRegex(pyproject, r'(?m)^license = "Apache-2\.0"$')
        self.assertRegex(pyproject, r'(?m)^license-files = \["LICENSE"\]$')
        self.assertIn("Apache License\n                           Version 2.0", license_text)
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)

    def test_mcp_guidance_document_is_declared_package_data(self):
        """The guidance Markdown must ship in wheels, not only in the checkout."""
        pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
        package_data = pyproject.split("[tool.setuptools.package-data]", 1)[1]
        self.assertIn('"mcp-guidance.md"', package_data)
        self.assertTrue((ROOT / "agent_collab" / "mcp-guidance.md").is_file())

    def test_pyproject_pins_and_configures_ruff_lint_and_format(self):
        pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")

        extras = self._toml_table(pyproject, "project.optional-dependencies")
        self.assertRegex(extras, rf'(?m)^dev\s*=\s*\[\s*"ruff=={RUFF_VERSION}",?\s*\]')
        ruff = self._toml_table(pyproject, "tool.ruff")
        self.assertRegex(ruff, r'(?m)^target-version\s*=\s*"py310"\s*$')
        self.assertRegex(ruff, r"(?m)^line-length\s*=\s*100\s*$")
        lint = self._toml_table(pyproject, "tool.ruff.lint")
        self.assertRegex(lint, r'(?m)^select\s*=\s*\["E4", "E7", "E9", "F"\]\s*$')
        formatter = self._toml_table(pyproject, "tool.ruff.format")
        self.assertRegex(formatter, r'(?m)^quote-style\s*=\s*"double"\s*$')
        self.assertRegex(formatter, r'(?m)^indent-style\s*=\s*"space"\s*$')
        self.assertRegex(formatter, r'(?m)^line-ending\s*=\s*"lf"\s*$')

    def test_base_install_is_sdk_free_and_all_extra_matches_provider_extras(self):
        """Vendor SDKs are opt-in extras; `all` must stay their exact union."""
        pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")

        project = self._toml_table(pyproject, "project")
        self.assertRegex(project, r"(?m)^dependencies\s*=\s*\[\]\s*$")

        extras = self._toml_table(pyproject, "project.optional-dependencies")
        provider_pins = set()
        for provider in ("claude", "codex", "antigravity", "xai"):
            pins = self._extra_requirements(extras, provider)
            self.assertEqual(len(pins), 1, f"{provider} extra must hold exactly one SDK pin")
            provider_pins.update(pins)
        self.assertEqual(set(self._extra_requirements(extras, "all")), provider_pins)

    def test_ci_runs_every_required_gate_with_pinned_actions(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertRegex(workflow, r"(?m)^  push:\s*$")
        self.assertRegex(workflow, r"(?m)^  pull_request:\s*$")
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn('python-version: ["3.10", "3.12"]', workflow)
        self.assertIn("python-version: ${{ matrix.python-version }}", workflow)
        self.assertIn(f"python -m pip install 'ruff=={RUFF_VERSION}'", workflow)
        self.assertIn("ruff check --output-format=github .", workflow)
        self.assertIn("ruff format --check .", workflow)
        self.assertIn("python -m unittest discover -s tests -t .", workflow)
        self.assertIn("./agent_collab.sh setup --check", workflow)
        self.assertIn("python -m pip install .", workflow)
        self.assertIn("agent-collab --help", workflow)

        uses = re.findall(r"(?m)^\s+uses:\s+([^\s#]+)", workflow)
        self.assertIn("actions/checkout", {action.partition("@")[0] for action in uses})
        self.assertIn("actions/setup-python", {action.partition("@")[0] for action in uses})
        for action in uses:
            _name, separator, revision = action.partition("@")
            self.assertEqual(separator, "@")
            self.assertRegex(revision, r"\A[0-9a-f]{40}\Z")

    @staticmethod
    def _extra_requirements(extras_table, name):
        match = re.search(rf"(?ms)^{re.escape(name)}\s*=\s*\[(.*?)\]", extras_table)
        if match is None:
            raise AssertionError(f"missing optional-dependencies extra: {name}")
        return re.findall(r'"([^"]+)"', match.group(1))

    @staticmethod
    def _toml_table(document, name):
        match = re.search(rf"(?ms)^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", document)
        if match is None:
            raise AssertionError(f"missing [{name}] table")
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
