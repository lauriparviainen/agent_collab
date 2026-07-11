import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon_autostart import (
    AutostartError,
    AutostartStatus,
    SERVICE_NAME,
    autostart_status,
    disable_autostart,
    enable_autostart,
    managed_unit_installed,
    render_systemd_unit,
    systemd_owns_daemon,
)
from agent_collab.daemon_supervisor import DaemonStatus
from agent_collab.paths import GlobalDataPaths


class DaemonAutostartTests(unittest.TestCase):
    def _paths(self, root: Path) -> GlobalDataPaths:
        return GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "home")})

    def _completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_unit_uses_foreground_mode_and_only_selected_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            text = render_systemd_unit(
                paths=paths,
                interpreter=root / "venv" / "bin" / "python",
                env={
                    "PATH": "/custom/bin:/usr/bin",
                    "AGENT_COLLAB_HOME": str(paths.home),
                    "SECRET_KEY": "must-not-leak",
                },
            )

        self.assertIn('daemon" "run', text)
        self.assertIn('Environment="PATH=/custom/bin:/usr/bin"', text)
        self.assertIn("AGENT_COLLAB_HOME", text)
        self.assertNotIn("SECRET_KEY", text)
        self.assertNotIn('daemon" "start', text)
        self.assertNotIn("WorkingDirectory=", text)
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=on-failure", text)

    def test_unit_escapes_systemd_dollar_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = render_systemd_unit(
                paths=self._paths(root),
                interpreter=root / "$venv" / "bin" / "python",
                env={"PATH": "/opt/$tools/bin:/usr/bin"},
            )

        self.assertIn("$$venv", text)
        self.assertIn("PATH=/opt/$tools/bin:/usr/bin", text)

    def test_enable_installs_enables_starts_and_waits_for_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / "config" / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            real_python = root / "system" / "python"
            real_python.parent.mkdir()
            real_python.touch()
            interpreter.symlink_to(real_python)
            expected_status = AutostartStatus(True, True, True, True, True, unit, "healthy")
            commands = []

            def systemctl(*args, **_kwargs):
                commands.append(args)
                return self._completed()

            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart._ensure_durable_install") as durable,
                mock.patch("agent_collab.daemon_autostart._systemctl", side_effect=systemctl),
                mock.patch("agent_collab.daemon_autostart._systemctl_truth", return_value=False),
                mock.patch(
                    "agent_collab.daemon_autostart.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart._wait_for_health") as wait,
                mock.patch(
                    "agent_collab.daemon_autostart.autostart_status",
                    return_value=expected_status,
                ),
            ):
                result = enable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                )

            self.assertEqual(result, expected_status)
            self.assertTrue(managed_unit_installed(unit))
            durable.assert_called_once_with(interpreter)
            self.assertIn(f"# Agent-Collab-Interpreter: {interpreter}", unit.read_text())
            self.assertEqual(
                commands,
                [("daemon-reload",), ("enable", SERVICE_NAME), ("start", SERVICE_NAME)],
            )
            wait.assert_called_once()

    def test_unchanged_active_enable_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(
                    paths=paths,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                ),
                encoding="utf-8",
            )
            commands = []
            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch("agent_collab.daemon_autostart._systemctl_truth", return_value=True),
                mock.patch("agent_collab.daemon_autostart._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart.autostart_status",
                    return_value=AutostartStatus(True, True, True, True, True, unit),
                ),
            ):
                enable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                )

            self.assertEqual(commands, [("enable", SERVICE_NAME)])

    def test_enable_stops_manual_daemon_and_restores_it_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            manual = DaemonStatus(
                True,
                {"host": "127.0.0.1", "port": 8765, "default_workdir": None},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart._systemctl_truth", return_value=False),
                mock.patch(
                    "agent_collab.daemon_autostart._systemctl", return_value=self._completed()
                ),
                mock.patch("agent_collab.daemon_autostart.daemon_status", return_value=manual),
                mock.patch("agent_collab.daemon_autostart.stop_daemon") as stop,
                mock.patch(
                    "agent_collab.daemon_autostart._wait_for_health",
                    side_effect=AutostartError("not ready"),
                ),
                mock.patch("agent_collab.daemon_autostart._restore_manual_daemon") as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "not ready"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=root / "python",
                        env={"PATH": "/usr/bin"},
                    )

            stop.assert_called_once_with(paths)
            restore.assert_called_once_with(paths, manual.state)

    def test_enable_refuses_unmanaged_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit = root / SERVICE_NAME
            unit.write_text("[Service]\nExecStart=other\n", encoding="utf-8")
            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart._ensure_durable_install"),
            ):
                with self.assertRaisesRegex(AutostartError, "unmanaged"):
                    enable_autostart(
                        paths=self._paths(root),
                        unit_path=unit,
                        interpreter=root / "python",
                        env={"PATH": "/usr/bin"},
                    )

    def test_disable_is_idempotent_and_preserves_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            paths.ensure_dirs()
            paths.daemon_log_path.write_text("keep\n", encoding="utf-8")
            unit = root / SERVICE_NAME
            unit.write_text("# Managed by agent-collab. Do not edit.\n", encoding="utf-8")
            commands = []
            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch("agent_collab.daemon_autostart.daemon_status"),
            ):
                result = disable_autostart(paths=paths, unit_path=unit)
                second = disable_autostart(paths=paths, unit_path=unit)

            self.assertFalse(result.installed)
            self.assertFalse(second.installed)
            self.assertFalse(unit.exists())
            self.assertEqual(paths.daemon_log_path.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(
                commands,
                [("disable", "--now", SERVICE_NAME), ("daemon-reload",)],
            )

    def test_disable_stops_live_loaded_service_after_unit_was_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / "missing.service"
            commands = []
            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart.systemd_owns_daemon", return_value=True),
                mock.patch(
                    "agent_collab.daemon_autostart._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch("agent_collab.daemon_autostart.daemon_status"),
            ):
                status = disable_autostart(paths=paths, unit_path=unit)

            self.assertEqual(status.detail, "disabled")
            self.assertEqual(
                commands,
                [
                    ("stop", SERVICE_NAME),
                    ("disable", SERVICE_NAME),
                    ("daemon-reload",),
                ],
            )

    def test_status_separates_registration_service_health_and_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            old = root / "missing-python"
            unit.write_text(
                f"# Managed by agent-collab. Do not edit.\n# Agent-Collab-Interpreter: {old}\n",
                encoding="utf-8",
            )
            with (
                mock.patch("agent_collab.daemon_autostart._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart._systemctl_truth", return_value=True),
                mock.patch("agent_collab.daemon_autostart._health", return_value=(True, "healthy")),
            ):
                status = autostart_status(
                    paths=paths, unit_path=unit, interpreter=root / "current-python"
                )

            self.assertTrue(status.installed)
            self.assertTrue(status.enabled)
            self.assertTrue(status.active)
            self.assertTrue(status.healthy)
            self.assertFalse(status.definition_current)

    def test_live_systemd_state_routes_even_if_unit_was_removed_externally(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            paths.ensure_dirs()
            paths.state_path.write_text(json.dumps({"manager": "systemd"}), encoding="utf-8")
            with mock.patch(
                "agent_collab.daemon_autostart.daemon_status",
                return_value=DaemonStatus(True, {"manager": "systemd"}, "running"),
            ):
                self.assertTrue(systemd_owns_daemon(paths=paths, unit_path=Path(tmp) / "missing"))

    def test_stale_systemd_state_does_not_lock_out_manual_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with mock.patch(
                "agent_collab.daemon_autostart.daemon_status",
                return_value=DaemonStatus(False, {"manager": "systemd"}, "stale"),
            ):
                self.assertFalse(systemd_owns_daemon(paths=paths, unit_path=Path(tmp) / "missing"))


if __name__ == "__main__":
    unittest.main()
