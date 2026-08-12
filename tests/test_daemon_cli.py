import io
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.cli import main
from agent_collab.daemon_autostart import AutostartError, AutostartStatus


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
            "start": "start_managed_daemon",
            "stop": "stop_managed_daemon",
            "restart": "restart_managed_daemon",
        }
        for action, function in cases.items():
            with self.subTest(action=action):
                with (
                    mock.patch(
                        "agent_collab.daemon_autostart.managed_service_identity",
                        return_value=mock.Mock(
                            current_home_owned=True,
                            installed=True,
                            loaded=True,
                            owned=True,
                        ),
                    ),
                    mock.patch(
                        f"agent_collab.daemon_autostart.{function}",
                        return_value=self._status(
                            active=action != "stop", healthy=action != "stop"
                        ),
                    ) as delegated,
                    mock.patch("agent_collab.cli._print_live_daemon"),
                    mock.patch("sys.stdout", new_callable=io.StringIO),
                ):
                    code = main(["daemon", action])

                self.assertEqual(code, 0)
                delegated.assert_called_once_with()

    def test_lifecycle_commands_delegate_for_current_home_runtime_only_identity(self):
        cases = {
            "start": "start_managed_daemon",
            "stop": "stop_managed_daemon",
            "restart": "restart_managed_daemon",
        }
        for action, function in cases.items():
            with self.subTest(action=action):
                identity = mock.Mock(
                    current_home_owned=True,
                    installed=False,
                    loaded=False,
                    owned=True,
                    pid=4242,
                )
                with (
                    mock.patch(
                        "agent_collab.daemon_autostart.managed_service_identity",
                        return_value=identity,
                    ),
                    mock.patch(
                        f"agent_collab.daemon_autostart.{function}",
                        return_value=self._status(
                            active=action != "stop", healthy=action != "stop"
                        ),
                    ) as delegated,
                    mock.patch("agent_collab.cli._print_live_daemon"),
                    mock.patch("sys.stdout", new_callable=io.StringIO),
                ):
                    code = main(["daemon", action])

                self.assertEqual(code, 0)
                delegated.assert_called_once_with()

    def test_status_reports_indeterminate_manager_discovery_without_mutation(self):
        stopped = mock.Mock(
            running=False,
            state={},
            message="global agent-collab daemon is not running",
        )
        with (
            mock.patch(
                "agent_collab.daemon_autostart.managed_service_identity",
                side_effect=AutostartError("user bus unavailable"),
            ),
            mock.patch("agent_collab.daemon_supervisor.daemon_status", return_value=stopped),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = main(["daemon", "status"])

        self.assertEqual(code, 1)
        self.assertIn("discovery is indeterminate", stdout.getvalue())
        self.assertIn("Daemon not running", stdout.getvalue())

    def test_mutating_detached_command_fails_closed_on_indeterminate_manager(self):
        with (
            mock.patch(
                "agent_collab.daemon_autostart.managed_service_identity",
                side_effect=AutostartError("user bus unavailable"),
            ),
            mock.patch("agent_collab.daemon_autostart.start_detached_daemon") as start,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = main(["daemon", "start"])

        self.assertEqual(code, 1)
        self.assertIn("discovery is indeterminate", stderr.getvalue())
        start.assert_not_called()

    def test_logs_do_not_require_native_manager_discovery(self):
        with (
            mock.patch(
                "agent_collab.daemon_autostart.managed_service_identity"
            ) as managed_identity,
            mock.patch(
                "agent_collab.daemon_supervisor.tail_daemon_log", return_value="saved log\n"
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = main(["daemon", "logs"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "saved log\n\n")
        managed_identity.assert_not_called()

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
            takeover=False,
        )

    def test_internal_run_uses_foreground_managed_daemon(self):
        with (
            mock.patch("agent_collab.cli.sys.platform", "linux"),
            mock.patch("agent_collab.daemon_supervisor.run_managed_daemon") as run,
        ):
            code = main(["daemon", "run", "--port", "9000"])

        self.assertEqual(code, 0)
        run.assert_called_once_with(
            host="127.0.0.1", port=9000, default_workdir=None, manager="systemd"
        )

    def test_autostart_takeover_is_explicitly_forwarded_only_on_mutations(self):
        with (
            mock.patch(
                "agent_collab.daemon_autostart.disable_autostart",
                return_value=self._status(active=False, healthy=False),
            ) as disable,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = main(["daemon", "autostart", "disable", "--takeover"])

        self.assertEqual(code, 0)
        disable.assert_called_once_with(takeover=True)

    def test_autostart_status_prints_neutral_manager_and_definition_keys(self):
        with (
            mock.patch(
                "agent_collab.daemon_autostart.autostart_status", return_value=self._status()
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = main(["daemon", "autostart", "status"])

        self.assertEqual(code, 0)
        self.assertRegex(stdout.getvalue(), r"(?m)^  manager\s+systemd$")
        self.assertRegex(stdout.getvalue(), r"(?m)^  definition\s+/tmp/agent-collab.service$")


if __name__ == "__main__":
    unittest.main()
