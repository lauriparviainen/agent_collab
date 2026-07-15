import io
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.cli import main
from agent_collab.daemon_autostart import AutostartStatus


class DaemonCliTests(unittest.TestCase):
    def _status(self, *, active=True, healthy=True):
        return AutostartStatus(
            installed=True,
            enabled=True,
            active=active,
            healthy=healthy,
            definition_current=True,
            unit_path=Path("/tmp/agent-collab.service"),
            detail="healthy" if healthy else "stopped",
        )

    def test_token_command_prints_ensured_token_on_plain_stdout(self):
        with (
            mock.patch(
                "agent_collab.config.ensure_daemon_token", return_value="tok-abc123"
            ) as ensure,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = main(["daemon", "token"])

        self.assertEqual(code, 0)
        ensure.assert_called_once_with()
        # Plain, single-line token so it composes into client setup commands.
        self.assertEqual(stdout.getvalue().strip(), "tok-abc123")

    def test_existing_lifecycle_commands_delegate_when_systemd_owns_daemon(self):
        cases = {
            "start": "start_systemd_daemon",
            "stop": "stop_systemd_daemon",
            "restart": "restart_systemd_daemon",
        }
        for action, function in cases.items():
            with self.subTest(action=action):
                with (
                    mock.patch(
                        "agent_collab.daemon_autostart.systemd_owns_daemon", return_value=True
                    ),
                    mock.patch(
                        f"agent_collab.daemon_autostart.{function}",
                        return_value=self._status(
                            active=action != "stop", healthy=action != "stop"
                        ),
                    ) as delegated,
                    mock.patch("sys.stdout", new_callable=io.StringIO),
                ):
                    code = main(["daemon", action])

                self.assertEqual(code, 0)
                delegated.assert_called_once_with()

    def test_autostart_status_exit_code_reflects_complete_health(self):
        with (
            mock.patch(
                "agent_collab.daemon_autostart.autostart_status",
                return_value=self._status(active=True, healthy=False),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = main(["daemon", "autostart", "status"])

        self.assertEqual(code, 1)
        self.assertRegex(stdout.getvalue(), r"(?m)^  installed\s+true$")
        self.assertRegex(stdout.getvalue(), r"(?m)^  healthy\s+false$")

    def test_autostart_enable_passes_service_options(self):
        with (
            mock.patch(
                "agent_collab.daemon_autostart.enable_autostart",
                return_value=self._status(),
            ) as enable,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = main(
                [
                    "daemon",
                    "autostart",
                    "enable",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9000",
                    "--workdir",
                    ".",
                ]
            )

        self.assertEqual(code, 0)
        enable.assert_called_once_with(
            host="127.0.0.1",
            port=9000,
            default_workdir=Path(".").resolve(),
        )

    def test_internal_run_uses_foreground_managed_daemon(self):
        with mock.patch("agent_collab.daemon_supervisor.run_managed_daemon") as run:
            code = main(["daemon", "run", "--port", "9000"])

        self.assertEqual(code, 0)
        run.assert_called_once_with(host="127.0.0.1", port=9000, default_workdir=None)


if __name__ == "__main__":
    unittest.main()
