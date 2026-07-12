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
        self.assertIn("./agent_collab.sh install", result.stdout)
        self.assertIn("./agent_collab.sh daemon start", result.stdout)
        self.assertIn("-m agent_collab.cli", result.stdout)

    def test_install_dispatches_to_user_installer(self):
        result, captured = self._run_with_fake_python(["install", "--editable"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            captured["args"],
            [
                "-m",
                "agent_collab.user_install",
                "--repo-root",
                str(ROOT),
                "--venv",
                str(Path.home() / ".agent-collab" / "venv"),
                "--editable",
            ],
        )

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

    def test_test_command_runs_static_checks_then_unittest_from_repo_root(self):
        result, commands = self._run_test_with_recording_python(cwd=Path(tempfile.gettempdir()))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            commands,
            [
                (str(ROOT), ["-m", "ruff", "--version"]),
                (str(ROOT), ["-m", "ruff", "check", "."]),
                (str(ROOT), ["-m", "ruff", "format", "--check", "."]),
                (
                    str(ROOT),
                    ["-m", "unittest", "discover", "-s", "tests", "-t", "."],
                ),
            ],
        )

    def test_test_command_explains_missing_ruff(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_python = Path(tmp) / "python3"
            fake_python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "ruff" ]]; then
  printf '%s: No module named ruff\\n' "$0" >&2
  exit 1
fi
exit 0
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env["AGENT_COLLAB_PYTHON"] = str(fake_python)
            result = subprocess.run(
                [str(SCRIPT), "test"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Ruff is not installed", result.stderr)
        self.assertIn("pip install -e '.[dev]'", result.stderr)

    def test_integration_test_command_runs_separate_package(self):
        result, captured = self._run_with_fake_python(
            ["integration-test", "claude_sdk", "--strict"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured["args"], ["-m", "integration_tests", "claude_sdk", "--strict"])

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

    def _run_test_with_recording_python(self, cwd=ROOT):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_python = tmp_path / "python3"
            capture_path = tmp_path / "commands.txt"
            fake_python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
  exit 0
fi
printf '%s' "$PWD" >> "$AGENT_COLLAB_CAPTURE"
for arg in "$@"; do
  printf '\037%s' "$arg" >> "$AGENT_COLLAB_CAPTURE"
done
printf '\n' >> "$AGENT_COLLAB_CAPTURE"
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            env = os.environ.copy()
            env["AGENT_COLLAB_PYTHON"] = str(fake_python)
            env["AGENT_COLLAB_CAPTURE"] = str(capture_path)
            result = subprocess.run(
                [str(SCRIPT), "test"],
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            commands = []
            for line in capture_path.read_text(encoding="utf-8").splitlines():
                command_cwd, *args = line.split("\x1f")
                commands.append((command_cwd, args))
            return result, commands

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
