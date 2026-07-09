import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agent_collab.sh"


class ShellWrapperTests(unittest.TestCase):
    def test_help_prints_common_workflows(self):
        result = subprocess.run(
            [str(SCRIPT), "help"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("./agent_collab.sh smoke", result.stdout)
        self.assertIn("./agent_collab.sh daemon start", result.stdout)
        self.assertIn("-m agent_collab.cli", result.stdout)

    def test_rejects_selected_python_older_than_310(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_python = Path(tmp) / "python"
            fake_python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  echo "Python 3.9.25"
  exit 0
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env["AGENT_COLLAB_PYTHON"] = str(fake_python)
            result = subprocess.run(
                [str(SCRIPT), "help"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires Python >= 3.10", result.stderr)
        self.assertIn("Python 3.9.25", result.stderr)

    def test_unknown_args_pass_through_exactly(self):
        result, captured = self._run_with_fake_python(
            ["start", "--mock", "--workdir", "/tmp/with space", "Task with spaces"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"],
            [
                "-m",
                "agent_collab.cli",
                "start",
                "--mock",
                "--workdir",
                "/tmp/with space",
                "Task with spaces",
            ],
        )
        self.assertEqual(captured["PYTHONPATH"].split(os.pathsep)[0], str(ROOT))

    def test_daemon_command_does_not_inject_workdir(self):
        result, captured = self._run_with_fake_python(["daemon", "status"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"],
            ["-m", "agent_collab.cli", "daemon", "status"],
        )

    def test_daemon_start_keeps_user_default_workdir(self):
        result, captured = self._run_with_fake_python(
            ["daemon", "start", "--workdir", "/tmp/project"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"],
            [
                "-m",
                "agent_collab.cli",
                "daemon",
                "start",
                "--workdir",
                "/tmp/project",
            ],
        )
        self.assertEqual(captured["args"].count("--workdir"), 1)

    def test_test_command_runs_unittest_discover_from_repo_root(self):
        result, captured = self._run_with_fake_python(["test"], cwd=Path(tempfile.gettempdir()))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured["cwd"], str(ROOT))
        self.assertEqual(captured["args"], ["-m", "unittest", "discover", "-s", "tests", "-t", "."])

    def test_integration_test_command_runs_separate_package(self):
        result, captured = self._run_with_fake_python(["integration-test", "claude", "sdk", "--strict"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"], ["-m", "integration_tests", "claude", "sdk", "--strict"]
        )

    def test_smoke_command_uses_mock_runner(self):
        result, captured = self._run_with_fake_python(["smoke"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"],
            ["-m", "agent_collab.cli", "--mock", "--workdir", ".", "Smoke test"],
        )

    def _run_with_fake_python(self, args, cwd=ROOT):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            capture_path = tmp_path / "capture.txt"
            fake_python = bin_dir / "python3"
            fake_python.write_text(
                """#!/usr/bin/env bash
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'PYTHONPATH=%s\\n' "${PYTHONPATH:-}"
  printf 'argc=%s\\n' "$#"
  i=0
  for arg in "$@"; do
    printf 'arg%s=%s\\n' "$i" "$arg"
    i=$((i + 1))
  done
} > "$AGENT_COLLAB_CAPTURE"
exit 0
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
            env["AGENT_COLLAB_CAPTURE"] = str(capture_path)
            env["AGENT_COLLAB_PYTHON"] = str(fake_python)
            result = subprocess.run(
                [str(SCRIPT), *args],
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            captured = self._read_capture(capture_path) if capture_path.exists() else {}
            return result, captured

    def _read_capture(self, capture_path):
        values = {}
        for line in capture_path.read_text(encoding="utf-8").splitlines():
            key, value = line.split("=", 1)
            values[key] = value
        argc = int(values["argc"])
        values["args"] = [values[f"arg{i}"] for i in range(argc)]
        return values


if __name__ == "__main__":
    unittest.main()
