import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon_autostart_systemd import (
    AutostartError,
    AutostartStatus,
    SERVICE_NAME,
    _ReadyEndpointUnavailableError,
    _cleanup_failed_candidate,
    _cleanup_failed_manual_candidate,
    _establish_disabled_stopped_after_failure,
    _quarantine_unit,
    _prove_pids_exited,
    _pid_owns_listening_endpoint,
    _post_commit_recovery_cleanup,
    _stop_managed_and_reap,
    _systemctl_truth,
    _systemd_main_pid,
    autostart_status,
    disable_autostart,
    enable_autostart,
    inspect as systemd_inspect,
    managed_unit_installed,
    parse_systemd_unit,
    quiesce_for_install_locked,
    recovery_paths,
    render_systemd_unit,
    restart_locked,
    restore_after_install_locked,
    resolve_systemd_unit_path,
    start_locked,
    stop_locked,
    systemd_unit_path,
    _wait_for_health,
    _wait_for_legacy_health,
)
from agent_collab.daemon_service import (
    EndpointInUseError,
    ManagedCommand,
    ManagedServiceIdentity,
    SafeManagedRestoreError,
    atomic_rename_noreplace,
)
from agent_collab.daemon_supervisor import DaemonStatus
from agent_collab.paths import GlobalDataPaths


class DaemonAutostartTests(unittest.TestCase):
    def setUp(self):
        lifecycle = mock.patch(
            "agent_collab.daemon_autostart_systemd.lifecycle_lock_held", return_value=True
        )
        registration = mock.patch(
            "agent_collab.daemon_autostart_systemd.registration_lock_held", return_value=True
        )
        lifecycle.start()
        registration.start()
        self.addCleanup(lifecycle.stop)
        self.addCleanup(registration.stop)

    def _paths(self, root: Path) -> GlobalDataPaths:
        return GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "home")})

    def _completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_systemd_mutation_requires_both_transaction_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            with mock.patch(
                "agent_collab.daemon_autostart_systemd.registration_lock_held",
                return_value=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "requires lifecycle and registration"):
                    start_locked(
                        paths=paths,
                        definition_path=root / SERVICE_NAME,
                        interpreter=root / "venv" / "bin" / "python",
                    )

    def test_post_commit_recovery_cleanup_reports_failures_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            recovery = root / "recovery"
            recovery.mkdir()
            with mock.patch(
                "agent_collab.daemon_autostart_systemd._remove_authorized_recoveries",
                side_effect=OSError("authorized cleanup denied"),
            ):
                warnings = _post_commit_recovery_cleanup(
                    definition,
                    paths,
                    root / "venv" / "bin" / "python",
                    recovery=recovery,
                    authorized_recoveries={},
                )

        self.assertEqual(len(warnings), 2)
        self.assertIn("recovery cleanup failed", warnings[0])
        self.assertIn("authorized recovery cleanup failed", warnings[1])

    def test_atomic_rename_noreplace_preserves_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.write_text("source", encoding="utf-8")
            destination.write_text("destination", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                atomic_rename_noreplace(source, destination)

            self.assertEqual(source.read_text(encoding="utf-8"), "source")
            self.assertEqual(destination.read_text(encoding="utf-8"), "destination")

    def test_failed_candidate_cleanup_captures_current_systemd_generation(self):
        paths = self._paths(Path("/tmp/agent-collab-cleanup-test"))
        definition = Path("/tmp/agent-collab-cleanup-test.service")
        command = ManagedCommand(Path("/tmp/venv/python"), "systemd", "127.0.0.1", 8765, None)
        observed = {111}
        with (
            mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemctl_truth",
                side_effect=[False, True, False],
            ),
            mock.patch("agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=222),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                return_value=(command, paths.home.resolve()),
            ),
            mock.patch(
                "agent_collab.daemon_autostart_systemd.daemon_status",
                return_value=DaemonStatus(False, {}, "stopped"),
            ),
            mock.patch("agent_collab.daemon_autostart_systemd._capture_runtime_systemd_pid"),
            mock.patch("agent_collab.daemon_autostart_systemd._stop_managed_and_reap") as stop,
            mock.patch("agent_collab.daemon_autostart_systemd._prove_pids_exited") as prove,
        ):
            _cleanup_failed_candidate(paths, definition, command, observed)

        stop.assert_called_once_with(paths, 222)
        prove.assert_called_once_with({111, 222})

    def test_failed_candidate_cleanup_checks_main_pid_while_inactive(self):
        paths = self._paths(Path("/tmp/agent-collab-inactive-cleanup-test"))
        definition = Path("/tmp/agent-collab-inactive-cleanup-test.service")
        command = ManagedCommand(Path("/tmp/venv/python"), "systemd", "127.0.0.1", 8765, None)
        with (
            mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
            ),
            mock.patch("agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=222),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                return_value=(command, paths.home.resolve()),
            ),
            mock.patch(
                "agent_collab.daemon_autostart_systemd.daemon_status",
                return_value=DaemonStatus(False, {}, "stopped"),
            ),
            mock.patch("agent_collab.daemon_autostart_systemd._capture_runtime_systemd_pid"),
            mock.patch("agent_collab.daemon_autostart_systemd._pid_alive", return_value=True),
        ):
            with self.assertRaisesRegex(AutostartError, "pid 222 remains alive"):
                _cleanup_failed_candidate(paths, definition, command, set())

    def test_failed_candidate_cleanup_refuses_an_unowned_replacement(self):
        paths = self._paths(Path("/tmp/agent-collab-collision-cleanup-test"))
        definition = Path("/tmp/agent-collab-collision-cleanup-test.service")
        command = ManagedCommand(Path("/tmp/venv/python"), "systemd", "127.0.0.1", 8765, None)
        replacement = ManagedCommand(Path("/tmp/other/python"), "systemd", "127.0.0.1", 9999, None)
        with (
            mock.patch("agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=333),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                return_value=(replacement, Path("/tmp/other-home")),
            ),
            mock.patch(
                "agent_collab.daemon_autostart_systemd.daemon_status",
                return_value=DaemonStatus(False, {}, "stopped"),
            ),
            mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
        ):
            with self.assertRaisesRegex(AutostartError, "process identity changed"):
                _cleanup_failed_candidate(paths, definition, command, set())

        systemctl.assert_not_called()

    def test_failed_manual_cleanup_refuses_an_unowned_replacement(self):
        paths = self._paths(Path("/tmp/agent-collab-manual-collision-test"))
        definition = Path("/tmp/agent-collab-manual-collision-test.service")
        command = ManagedCommand(Path("/tmp/venv/python"), "systemd", "127.0.0.1", 8765, None)
        replacement = ManagedCommand(Path("/tmp/other/python"), "systemd", "127.0.0.1", 9999, None)
        with (
            mock.patch("agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=333),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                return_value=(replacement, Path("/tmp/other-home")),
            ),
            mock.patch(
                "agent_collab.daemon_autostart_systemd.daemon_status",
                return_value=DaemonStatus(False, {}, "stopped"),
            ),
            mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
        ):
            with self.assertRaisesRegex(AutostartError, "process identity changed"):
                _cleanup_failed_manual_candidate(paths, definition, command, set())

        systemctl.assert_not_called()

    def test_systemd_transitional_state_is_treated_as_occupied(self):
        for state in ("activating", "deactivating", "maintenance", "refreshing"):
            with (
                self.subTest(state=state),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(returncode=3, stdout=f"{state}\n"),
                ),
            ):
                self.assertTrue(_systemctl_truth("is-active", SERVICE_NAME))

    def test_status_does_not_report_pidless_transition_as_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            interpreter = root / "venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            unit = root / SERVICE_NAME
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    side_effect=[True, True],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
            ):
                result = autostart_status(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertFalse(result.active)
            self.assertFalse(result.healthy)
            self.assertIn("transitioning", result.detail)

    def test_proven_disabled_stopped_install_residual_is_classified_safe(self):
        with (
            mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
            mock.patch(
                "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
            ),
        ):
            with self.assertRaises(SafeManagedRestoreError):
                _establish_disabled_stopped_after_failure(AutostartError("enable failed"))

    def test_managed_unit_detection_rejects_a_canonical_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            interpreter = root / "venv" / "bin" / "python"
            target = root / "target.service"
            target.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/bin"}),
                encoding="utf-8",
            )
            canonical = root / SERVICE_NAME
            canonical.symlink_to(target)
            with self.assertRaisesRegex(AutostartError, "cannot open systemd unit"):
                managed_unit_installed(canonical)

    def test_unit_quarantine_restores_an_inode_raced_during_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            interpreter = root / "venv" / "bin" / "python"
            canonical = root / SERVICE_NAME
            original = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/bin"}
            )
            replacement = original.replace("8765", "9000")
            canonical.write_text(original, encoding="utf-8")
            rename_calls = 0

            def raced_replace(source, target):
                nonlocal rename_calls
                if rename_calls == 0:
                    canonical.write_text(replacement, encoding="utf-8")
                rename_calls += 1
                return atomic_rename_noreplace(source, target)

            with mock.patch(
                "agent_collab.daemon_autostart_systemd.atomic_rename_noreplace",
                side_effect=raced_replace,
            ):
                with self.assertRaisesRegex(AutostartError, "identity changed"):
                    _quarantine_unit(
                        canonical,
                        paths,
                        interpreter,
                        authorized_recoveries={},
                        expected_content=original,
                    )
            self.assertEqual(canonical.read_text(encoding="utf-8"), replacement)
            self.assertFalse(any(path.exists() for path in recovery_paths(canonical)))

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

    def test_unit_home_metadata_must_be_complete_unique_and_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            interpreter = root / "venv" / "bin" / "python"
            content = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            cases = {
                "conflicting": content.replace(
                    f"AGENT_COLLAB_HOME={paths.home}",
                    f"AGENT_COLLAB_HOME={root / 'foreign'}",
                ),
                "duplicate marker": content.replace(
                    "[Unit]", f"# Agent-Collab-Home: {paths.home}\n[Unit]"
                ),
                "duplicate environment": content.replace(
                    "Restart=on-failure",
                    f'Environment="AGENT_COLLAB_HOME={paths.home}"\nRestart=on-failure',
                ),
                "incomplete": "\n".join(
                    line
                    for line in content.splitlines()
                    if 'Environment="AGENT_COLLAB_HOME=' not in line
                ),
                "malformed": content.replace(
                    f'Environment="AGENT_COLLAB_HOME={paths.home}"',
                    'Environment="AGENT_COLLAB_HOME"',
                ),
            }
            for name, candidate in cases.items():
                with self.subTest(name=name):
                    with self.assertRaisesRegex(AutostartError, "AGENT_COLLAB_HOME metadata"):
                        parse_systemd_unit(candidate)

            equivalent = content.replace(
                f"# Agent-Collab-Home: {paths.home}",
                f"# Agent-Collab-Home: {paths.home / '..' / paths.home.name}",
            )
            self.assertEqual(parse_systemd_unit(equivalent).effective_home, paths.home)

            legacy_environment_only = "\n".join(
                line
                for line in content.splitlines()
                if not line.startswith("# Agent-Collab-Home: ")
            )
            self.assertEqual(
                parse_systemd_unit(legacy_environment_only).effective_home,
                paths.home,
            )

    def test_unit_legacy_home_omission_uses_account_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            content = render_systemd_unit(
                paths=paths,
                interpreter=root / "venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            legacy = "\n".join(
                line
                for line in content.splitlines()
                if not line.startswith("# Agent-Collab-Home: ")
                and 'Environment="AGENT_COLLAB_HOME=' not in line
            )
            with mock.patch(
                "agent_collab.daemon_autostart_systemd.account_home",
                return_value=root / "account",
            ):
                identity = parse_systemd_unit(legacy)
            self.assertEqual(identity.effective_home, (root / "account" / ".agent-collab"))

    def test_unit_home_with_percent_round_trips_through_systemd_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "percent%home")})
            content = render_systemd_unit(
                paths=paths,
                interpreter=root / "venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            parsed = parse_systemd_unit(content)

            self.assertIn("percent%%home", content)
            self.assertEqual(parsed.effective_home, paths.home)

    def test_systemctl_state_queries_fail_closed(self):
        cases = (
            ("is-active", self._completed(1, stderr="Failed to connect to bus")),
            ("is-active", self._completed(1, stdout="mystery\n")),
            ("is-enabled", self._completed(1, stderr="user manager unavailable")),
            ("is-enabled", self._completed(0, stdout="mystery\n")),
        )
        for action, completed in cases:
            with self.subTest(action=action, output=completed.stdout or completed.stderr):
                with mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=completed,
                ):
                    with self.assertRaisesRegex(AutostartError, "indeterminate|inconsistent"):
                        _systemctl_truth(action, SERVICE_NAME)

    def test_legacy_health_restore_is_bound_to_stable_main_pid_and_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            runtime = DaemonStatus(True, {"pid": 444, "manager": "systemd"}, "running")
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[444, 444],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=runtime,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.legacy_health",
                    return_value=(True, "healthy"),
                ) as health,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._pid_owns_listening_endpoint",
                    return_value=True,
                ),
            ):
                _wait_for_legacy_health("127.0.0.1", 8765, paths, 1.0)

            health.assert_called_once_with(host="127.0.0.1", port=8765, paths=paths)

    def test_legacy_health_rejects_a_response_not_owned_by_main_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            runtime = DaemonStatus(True, {"pid": 444, "manager": "systemd"}, "running")
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.time.monotonic",
                    side_effect=[0.0, 0.0, 2.0],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.time.sleep"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    return_value=444,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=runtime,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._pid_owns_listening_endpoint",
                    return_value=False,
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.legacy_health") as health,
            ):
                with self.assertRaisesRegex(AutostartError, "does not own"):
                    _wait_for_legacy_health("127.0.0.1", 8765, paths, 1.0)

            health.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux procfs")
    def test_procfs_endpoint_ownership_is_pid_and_port_bound(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        self.assertTrue(_pid_owns_listening_endpoint(os.getpid(), "127.0.0.1", port))
        self.assertFalse(_pid_owns_listening_endpoint(os.getpid(), "127.0.0.1", port + 1))

    def test_systemd_readiness_tracks_every_candidate_generation_for_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            observed: set[int] = set()
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.time.monotonic",
                    side_effect=[0.0, 0.0, 2.0],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.time.sleep"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[111, 222],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(False, "not ready"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "did not become healthy"):
                    _wait_for_health("127.0.0.1", 8765, paths, 1.0, observed_pids=observed)

            self.assertEqual(observed, {111, 222})
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._pid_alive",
                    side_effect=lambda pid: pid == 111,
                ),
                self.assertRaisesRegex(AutostartError, "pid 111 remains alive"),
            ):
                _prove_pids_exited(observed)

        recognized = (
            ("is-active", self._completed(0, stdout="active\n"), True),
            ("is-active", self._completed(3, stdout="inactive\n"), False),
            ("is-enabled", self._completed(0, stdout="enabled\n"), True),
            ("is-enabled", self._completed(1, stdout="disabled\n"), False),
        )
        for action, completed, expected in recognized:
            with mock.patch(
                "agent_collab.daemon_autostart_systemd._systemctl", return_value=completed
            ):
                self.assertEqual(_systemctl_truth(action, SERVICE_NAME), expected)

    def test_systemd_main_pid_distinguishes_absence_from_query_failure(self):
        with mock.patch(
            "agent_collab.daemon_autostart_systemd._systemctl",
            return_value=self._completed(stdout="0\n"),
        ):
            self.assertIsNone(_systemd_main_pid())
        for completed in (
            self._completed(1, stderr="Failed to connect to bus"),
            self._completed(stdout=""),
            self._completed(stdout="not-a-pid\n"),
        ):
            with mock.patch(
                "agent_collab.daemon_autostart_systemd._systemctl", return_value=completed
            ):
                with self.assertRaisesRegex(AutostartError, "MainPID"):
                    _systemd_main_pid()

    def test_inspect_recovers_active_preupgrade_runtime_identity_from_main_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            command = ManagedCommand(interpreter, "systemd", "127.0.0.1", 8765, None)
            legacy = DaemonStatus(
                True,
                {
                    "pid": 321,
                    "manager": "systemd",
                    "home": str(paths.home),
                    "argv": [str(interpreter), str(root / "agent_collab" / "cli.py")],
                },
                "running",
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=True
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=321
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=legacy
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                    return_value=(command, paths.home),
                ) as process_identity,
            ):
                identity = systemd_inspect(
                    paths=paths, definition_path=unit, interpreter=interpreter
                )

            self.assertTrue(identity.current_home_owned)
            self.assertEqual(identity.source, "definition+process")
            self.assertEqual(identity.command, command)
            self.assertEqual(identity.effective_home, paths.home)
            process_identity.assert_called_once_with(321)

    def test_runtime_only_identity_requires_matching_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            prior_interpreter = root / "old-venv" / "bin" / "python"
            current_interpreter = root / "new-venv" / "bin" / "python"
            runtime = DaemonStatus(
                True,
                {
                    "pid": 321,
                    "manager": "systemd",
                    "home": str(paths.home),
                    "argv": [
                        str(prior_interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    return_value=True,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    return_value=321,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=runtime,
                ),
            ):
                identity = systemd_inspect(
                    paths=paths,
                    definition_path=unit,
                    interpreter=current_interpreter,
                )

            self.assertTrue(identity.owned)
            self.assertFalse(identity.current_home_owned)
            self.assertFalse(identity.installed)
            self.assertTrue(identity.loaded)
            self.assertEqual(identity.command.interpreter, prior_interpreter)
            self.assertEqual(identity.pid, 321)

    def test_runtime_only_identity_rejects_missing_command_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            runtime = DaemonStatus(
                True,
                {
                    "pid": 321,
                    "manager": "systemd",
                    "home": str(paths.home),
                },
                "running",
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    return_value=True,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    return_value=321,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=runtime,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                    return_value=(None, None),
                ) as process_identity,
            ):
                identity = systemd_inspect(
                    paths=paths,
                    definition_path=unit,
                    interpreter=interpreter,
                )

            self.assertTrue(identity.owned)
            self.assertFalse(identity.current_home_owned)
            self.assertIsNone(identity.command)
            self.assertEqual(identity.pid, 321)
            process_identity.assert_called_once_with(321)

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

    def test_fresh_unit_path_uses_manager_environment_not_caller_environment(self):
        completed = self._completed(stdout="HOME=/manager/home\nXDG_CONFIG_HOME=/manager/config\n")
        with (
            mock.patch("agent_collab.daemon_autostart_systemd._systemctl", return_value=completed),
            mock.patch.dict(
                "os.environ", {"HOME": "/caller/home", "XDG_CONFIG_HOME": "/caller/config"}
            ),
        ):
            path = systemd_unit_path()
        self.assertEqual(path, Path("/manager/config/systemd/user/agent-collab.service"))

    def test_manager_environment_rejects_duplicate_home_inputs(self):
        for duplicate in (
            "HOME=/manager/home\nHOME=/other/home\n",
            "XDG_CONFIG_HOME=/manager/config\nXDG_CONFIG_HOME=/other/config\n",
        ):
            with self.subTest(duplicate=duplicate.splitlines()[0]):
                with mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(stdout=duplicate),
                ):
                    with self.assertRaisesRegex(AutostartError, "duplicate.*indeterminate"):
                        systemd_unit_path()

    def test_existing_fragment_path_is_authoritative(self):
        environment = self._completed(stdout="HOME=/manager/home\n")
        fragment = self._completed(
            stdout="LoadState=loaded\nFragmentPath=/legacy/systemd/agent-collab.service\n"
        )
        with mock.patch(
            "agent_collab.daemon_autostart_systemd._systemctl",
            side_effect=[environment, fragment],
        ):
            path = resolve_systemd_unit_path()
        self.assertEqual(path, Path("/legacy/systemd/agent-collab.service"))

    def test_indeterminate_fragment_discovery_fails_closed(self):
        environment = self._completed(stdout="HOME=/manager/home\n")
        for state in ("loaded", "masked"):
            with self.subTest(state=state):
                ambiguous = self._completed(stdout=f"LoadState={state}\nFragmentPath=\n")
                with mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=[environment, ambiguous],
                ):
                    with self.assertRaisesRegex(AutostartError, "indeterminate"):
                        resolve_systemd_unit_path()

    def test_fragment_discovery_rejects_conflicting_duplicate_properties(self):
        environment = self._completed(stdout="HOME=/manager/home\n")
        for output in (
            "LoadState=loaded\nLoadState=not-found\nFragmentPath=/one/service\n",
            "LoadState=loaded\nFragmentPath=/one/service\nFragmentPath=/two/service\n",
        ):
            with self.subTest(output=output):
                with mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=[environment, self._completed(stdout=output)],
                ):
                    with self.assertRaisesRegex(AutostartError, "conflicting duplicate"):
                        resolve_systemd_unit_path()

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
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._ensure_durable_install"
                ) as durable,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl", side_effect=systemctl
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health") as wait,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
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
            runtime = DaemonStatus(
                True,
                {
                    "pid": 123,
                    "manager": "systemd",
                    "home": str(paths.home),
                    "argv": [
                        str(interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=True
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=123
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=runtime
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(True, "ready"),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
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

    def test_failed_unchanged_active_enable_restores_prior_disabled_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            runtime = DaemonStatus(
                True,
                {
                    "pid": 123,
                    "manager": "systemd",
                    "home": str(paths.home),
                    "argv": [
                        str(interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            commands = []
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    side_effect=[True, False, False],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **kwargs: (
                        commands.append((args, kwargs)) or self._completed()
                    ),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    return_value=123,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=runtime,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(True, "ready"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=[AutostartError("probe failed"), None],
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "probe failed"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                    )

            self.assertIn((("enable", SERVICE_NAME), {}), commands)
            self.assertIn((("disable", SERVICE_NAME), {"check": False}), commands)
            self.assertIn((("start", SERVICE_NAME), {}), commands)

    def test_unchanged_live_unhealthy_enable_performs_one_controlled_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            runtime = DaemonStatus(
                True,
                {
                    "pid": 123,
                    "manager": "systemd",
                    "home": str(paths.home),
                    "argv": [
                        str(interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            commands = []
            expected = AutostartStatus(True, True, True, True, True, unit)
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    side_effect=[True, True],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    return_value=123,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=runtime,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(False, "unhealthy"),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._stop_managed_and_reap") as stop,
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=expected,
                ),
            ):
                result = enable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                )

            self.assertEqual(result, expected)
            stop.assert_called_once_with(paths, 123)
            self.assertEqual(commands, [("enable", SERVICE_NAME), ("start", SERVICE_NAME)])

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
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=manual
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.stop_daemon") as stop,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=AutostartError("not ready"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._restore_manual_daemon"
                ) as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "not ready"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=root / "python",
                        env={"PATH": "/usr/bin"},
                    )

            stop.assert_called_once_with(paths, _lifecycle_locked=True)
            restore.assert_called_once_with(paths, manual.state)

    def test_enable_refuses_unrelated_requested_listener_even_with_active_service(self):
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
                    port=8765,
                ),
                encoding="utf-8",
            )
            runtime = DaemonStatus(
                True,
                {
                    "pid": 123,
                    "manager": "systemd",
                    "home": str(paths.home),
                    "argv": [
                        str(interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=True
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=123
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=runtime
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    side_effect=EndpointInUseError("unrelated listener"),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "unrelated listener"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        port=9000,
                    )

            systemctl.assert_not_called()

    def test_enable_refuses_active_foreign_runtime_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            foreign = DaemonStatus(
                True,
                {
                    "pid": 321,
                    "manager": "systemd",
                    "home": str(root / "foreign-home"),
                    "argv": [
                        str(root / "foreign-venv" / "bin" / "python"),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=True
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=321
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=foreign
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint"
                ) as reserve,
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "cannot be proven.*--takeover"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                    )

            reserve.assert_not_called()
            systemctl.assert_not_called()

    def test_explicit_takeover_replaces_active_foreign_systemd_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            foreign_home = root / "foreign-home"
            foreign_interpreter = root / "foreign-venv" / "bin" / "python"
            foreign = DaemonStatus(
                True,
                {
                    "pid": 654,
                    "manager": "systemd",
                    "home": str(foreign_home),
                    "argv": [
                        str(foreign_interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "systemd",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                },
                "running",
            )
            active_calls = 0
            enabled_calls = 0

            def truth(action, _service):
                nonlocal active_calls, enabled_calls
                if action == "is-active":
                    active_calls += 1
                    return active_calls == 1
                enabled_calls += 1
                return enabled_calls == 1

            expected = AutostartStatus(True, True, True, True, True, unit, "healthy")
            commands = []
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", side_effect=truth
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[654, 654, None],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._pid_alive", return_value=False),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=foreign
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    side_effect=EndpointInUseError("owned old listener"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=expected,
                ),
            ):
                result = enable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                    takeover=True,
                )

            self.assertEqual(result, expected)
            self.assertIn(("disable", SERVICE_NAME), commands)
            self.assertIn(("start", SERVICE_NAME), commands)
            self.assertNotIn(("restart", SERVICE_NAME), commands)
            self.assertFalse(any(path.exists() for path in recovery_paths(unit)))

    def test_enable_refuses_unmanaged_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit = root / SERVICE_NAME
            unit.write_text("[Service]\nExecStart=other\n", encoding="utf-8")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "unmanaged"):
                    enable_autostart(
                        paths=self._paths(root),
                        unit_path=unit,
                        interpreter=root / "python",
                        env={"PATH": "/usr/bin"},
                    )

    def test_changed_unit_reload_failure_restores_exact_prior_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            previous = render_systemd_unit(
                paths=paths,
                interpreter=interpreter,
                env={"PATH": "/usr/bin"},
                port=8765,
            )
            previous_bytes = previous.replace("\n", "\r\n").encode("utf-8")
            unit.write_bytes(previous_bytes)
            reload_count = 0

            def systemctl(*args, **_kwargs):
                nonlocal reload_count
                if args == ("daemon-reload",):
                    reload_count += 1
                    if reload_count == 2:
                        raise AutostartError("reload failed after replacement")
                return self._completed()

            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl", side_effect=systemctl
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "reload failed after replacement"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        port=9000,
                    )

            self.assertEqual(unit.read_bytes(), previous_bytes)
            self.assertFalse(any(path.exists() for path in recovery_paths(unit)))

    def test_enable_rejects_same_manager_orphan_before_unit_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            prior = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}, port=8765
            ).encode()
            unit.write_bytes(prior)
            orphan = DaemonStatus(
                True,
                {"pid": 808, "manager": "systemd", "home": str(paths.home)},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=orphan
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint"
                ) as reserve,
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "orphaned.*before enabling"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        port=9000,
                    )

            self.assertEqual(unit.read_bytes(), prior)
            reserve.assert_not_called()
            systemctl.assert_not_called()

    def test_enable_rolls_back_when_final_systemd_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            unhealthy = AutostartStatus(True, True, False, False, True, unit, "process exited")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=unhealthy,
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "did not remain.*healthy"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=root / "venv" / "bin" / "python",
                        env={"PATH": "/usr/bin"},
                    )

            self.assertFalse(unit.exists())

    def test_irreversible_systemd_takeover_failure_keeps_prior_unit_non_loadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            unit = root / SERVICE_NAME
            interpreter = root / "new-venv" / "bin" / "python"
            previous = render_systemd_unit(
                paths=foreign_paths,
                interpreter=root / "old-venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            unit.write_text(previous, encoding="utf-8")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._home_has_token", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=AutostartError("candidate failed"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "remains non-loadable"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        takeover=True,
                    )

            self.assertFalse(unit.exists())
            recoveries = [path for path in recovery_paths(unit) if path.exists()]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), previous)

    def test_disable_is_idempotent_and_preserves_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            paths.ensure_dirs()
            paths.daemon_log_path.write_text("keep\n", encoding="utf-8")
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
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.daemon_status"),
            ):
                result = disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)
                second = disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertFalse(result.installed)
            self.assertFalse(second.installed)
            self.assertFalse(unit.exists())
            self.assertEqual(paths.daemon_log_path.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(
                commands,
                [("disable", SERVICE_NAME), ("stop", SERVICE_NAME), ("daemon-reload",)],
            )

    def test_stop_refuses_same_manager_orphan_when_unit_is_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            identity = ManagedServiceIdentity(
                "systemd", "definition", True, True, True, False, True, command, paths.home
            )
            orphan = DaemonStatus(
                True,
                {"pid": 444, "manager": "systemd", "home": str(paths.home)},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=orphan
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "orphaned.*refusing"):
                    stop_locked(
                        paths=paths,
                        definition_path=root / SERVICE_NAME,
                        interpreter=command.interpreter,
                    )

            systemctl.assert_not_called()

    def test_start_and_restart_report_runtime_orphan_before_disabled_or_missing_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            identity = ManagedServiceIdentity(
                "systemd", "runtime", True, True, False, False, False, command, paths.home, 444
            )
            orphan = DaemonStatus(
                True,
                {"pid": 444, "manager": "systemd", "home": str(paths.home)},
                "running",
            )
            for operation in (start_locked, restart_locked):
                with (
                    self.subTest(operation=operation.__name__),
                    mock.patch(
                        "agent_collab.daemon_autostart_systemd.inspect", return_value=identity
                    ),
                    mock.patch(
                        "agent_collab.daemon_autostart_systemd.daemon_status", return_value=orphan
                    ),
                    mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
                ):
                    with self.assertRaisesRegex(AutostartError, "pid 444.*orphaned"):
                        operation(
                            paths=paths,
                            definition_path=root / SERVICE_NAME,
                            interpreter=command.interpreter,
                        )

                systemctl.assert_not_called()

    def test_start_and_restart_refuse_a_persistently_disabled_unit_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            identity = ManagedServiceIdentity(
                "systemd", "definition", True, True, True, False, False, command, paths.home
            )
            for operation in (start_locked, restart_locked):
                with (
                    self.subTest(operation=operation.__name__),
                    mock.patch(
                        "agent_collab.daemon_autostart_systemd.inspect", return_value=identity
                    ),
                    mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
                ):
                    with self.assertRaisesRegex(AutostartError, "autostart enable"):
                        operation(
                            paths=paths,
                            definition_path=root / SERVICE_NAME,
                            interpreter=command.interpreter,
                        )

                systemctl.assert_not_called()

    def test_start_is_a_noop_for_a_healthy_live_but_disabled_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            definition = root / SERVICE_NAME
            identity = ManagedServiceIdentity(
                "systemd",
                "definition+runtime",
                True,
                True,
                True,
                True,
                False,
                command,
                paths.home,
                321,
            )
            expected = AutostartStatus(True, False, True, True, True, definition)
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(True, "ready"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=expected,
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                result = start_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertEqual(result, expected)
            systemctl.assert_not_called()

    def test_stop_refuses_an_unattributable_replacement_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            identity = ManagedServiceIdentity(
                "systemd",
                "process-collision",
                False,
                False,
                False,
                True,
                False,
                None,
                None,
                999,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._stop_managed_and_reap"
                ) as stopped,
            ):
                with self.assertRaisesRegex(AutostartError, "not attributable"):
                    stop_locked(
                        paths=paths,
                        definition_path=root / SERVICE_NAME,
                        interpreter=root / "venv" / "bin" / "python",
                    )

            stopped.assert_not_called()

    def test_disable_refuses_same_manager_orphan_before_definition_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            content = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            unit.write_text(content, encoding="utf-8")
            orphan = DaemonStatus(
                True,
                {"pid": 445, "manager": "systemd", "home": str(paths.home)},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=orphan
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "orphaned.*refusing"):
                    disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertEqual(unit.read_text(encoding="utf-8"), content)
            systemctl.assert_not_called()

    def test_disable_proves_captured_main_pid_gone_before_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            content = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            unit.write_text(content, encoding="utf-8")
            command = ManagedCommand(interpreter, "systemd", "127.0.0.1", 8765, None)
            identity = ManagedServiceIdentity(
                "systemd",
                "definition+runtime",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
                446,
            )
            stopped = DaemonStatus(False, {"pid": 446, "manager": "systemd"}, "stopped")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=stopped
                ) as status,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[446, None],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._pid_alive", return_value=False),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
            ):
                result = disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertFalse(result.installed)
            self.assertFalse(unit.exists())
            self.assertGreaterEqual(status.call_count, 3)

    def test_stop_refuses_a_replaced_systemd_process_generation_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    return_value=999,
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "generation changed.*446.*999"):
                    _stop_managed_and_reap(paths, 446)

            systemctl.assert_not_called()

    def test_disable_keeps_definition_when_captured_main_pid_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            content = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            unit.write_text(content, encoding="utf-8")
            command = ManagedCommand(interpreter, "systemd", "127.0.0.1", 8765, None)
            identity = ManagedServiceIdentity(
                "systemd",
                "definition+runtime",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
                447,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[447, None],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._pid_alive", return_value=True),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "MainPID 447 remains alive"):
                    disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertEqual(unit.read_text(encoding="utf-8"), content)

    def test_takeover_disables_foreign_loaded_service_after_unit_was_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / "missing.service"
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            foreign_command = ManagedCommand(
                root / "foreign-venv" / "bin" / "python",
                "systemd",
                "127.0.0.1",
                8765,
                None,
            )
            recovery = recovery_paths(unit)[0]
            recovery.write_text(
                render_systemd_unit(
                    paths=foreign_paths,
                    interpreter=foreign_command.interpreter,
                    env={"PATH": "/usr/bin"},
                ),
                encoding="utf-8",
            )
            recovery.chmod(0o600)
            commands = []
            active_results = iter([True, True, False])

            def truth(action, _service):
                return next(active_results) if action == "is-active" else False

            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    side_effect=truth,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[321, 321, 321, None],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_process_identity",
                    return_value=(foreign_command, root / "foreign-home"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "--takeover"):
                    disable_autostart(paths=paths, unit_path=unit)
                status = disable_autostart(paths=paths, unit_path=unit, takeover=True)

            self.assertEqual(status.detail, "disabled")
            self.assertFalse(recovery.exists())
            self.assertEqual(
                commands,
                [
                    ("disable", SERVICE_NAME),
                    ("stop", SERVICE_NAME),
                    ("daemon-reload",),
                ],
            )

    def test_recovery_only_disable_proves_barrier_before_removing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            recovery = recovery_paths(unit)[0]
            recovery.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            recovery.chmod(0o600)
            order = []

            def truth(action, _service):
                order.append(action)
                return False

            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", side_effect=truth
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: order.append(args) or self._completed(),
                ) as systemctl,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
            ):
                result = disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertFalse(result.installed)
            self.assertFalse(recovery.exists())
            disable_at = order.index(("disable", SERVICE_NAME))
            proof_at = order.index("is-enabled", disable_at)
            self.assertLess(disable_at, proof_at)
            self.assertIn(mock.call("disable", SERVICE_NAME, check=False), systemctl.call_args_list)

    def test_recovery_only_disable_reports_post_commit_cleanup_failure_as_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            recovery = recovery_paths(unit)[0]
            recovery.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            recovery.chmod(0o600)
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    return_value=False,
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._remove_authorized_recoveries",
                    side_effect=OSError("permission denied"),
                ),
            ):
                result = disable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                )

            self.assertFalse(result.enabled)
            self.assertFalse(result.active)
            self.assertIn("authorized recovery cleanup failed: permission denied", result.detail)

    def test_disable_takeover_refuses_unattributable_active_systemd_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "systemd", "definition+process", True, False, True, True, True, None, None, 555
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(AutostartError, "unattributable.*takeover"):
                    disable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=interpreter,
                        takeover=True,
                    )

            systemctl.assert_not_called()

    def test_irreversible_takeover_barrier_failure_preserves_exact_prior_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            unit = root / SERVICE_NAME
            previous = (
                render_systemd_unit(
                    paths=foreign_paths,
                    interpreter=root / "foreign-venv" / "bin" / "python",
                    env={"PATH": "/usr/bin"},
                )
                .replace("\n", "\r\n")
                .encode()
            )
            unit.write_bytes(previous)
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    side_effect=[False, False, True],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._home_has_token", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "prior unit bytes were left untouched"):
                    enable_autostart(
                        paths=paths,
                        unit_path=unit,
                        interpreter=root / "new-venv" / "bin" / "python",
                        env={"PATH": "/usr/bin"},
                        takeover=True,
                    )

            self.assertEqual(unit.read_bytes(), previous)
            self.assertFalse(any(path.exists() for path in recovery_paths(unit)))

    def test_start_does_not_restore_detached_daemon_when_candidate_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            identity = ManagedServiceIdentity(
                "systemd", "definition", True, True, True, False, True, command, paths.home
            )
            detached = DaemonStatus(
                True,
                {"pid": 91, "manager": "detached", "host": command.host, "port": command.port},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=detached,
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.stop_daemon"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=AutostartError("candidate failed"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._cleanup_failed_manual_candidate",
                    side_effect=AutostartError("pid remains alive"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._restore_manual_daemon"
                ) as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "cleanup failed.*pid remains alive"):
                    start_locked(
                        paths=paths,
                        definition_path=root / SERVICE_NAME,
                        interpreter=command.interpreter,
                    )

            restore.assert_not_called()

    def test_start_cleans_up_when_final_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            definition = root / SERVICE_NAME
            identity = ManagedServiceIdentity(
                "systemd", "definition", True, True, True, False, True, command, paths.home
            )
            unhealthy = AutostartStatus(
                True, True, False, False, True, definition, "inactive", "systemd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=unhealthy,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._cleanup_failed_manual_candidate"
                ) as cleanup,
            ):
                with self.assertRaisesRegex(AutostartError, "did not remain"):
                    start_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            cleanup.assert_called_once_with(paths, definition, command, set())

    def test_recovery_only_disable_keeps_artifact_if_barrier_cannot_be_proven(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            recovery = recovery_paths(unit)[0]
            recovery.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            recovery.chmod(0o600)
            enabled_queries = 0

            def truth(action, _service):
                nonlocal enabled_queries
                if action == "is-enabled":
                    enabled_queries += 1
                    return enabled_queries == 2
                return False

            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", side_effect=truth
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "remains persistently enabled"):
                    disable_autostart(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertTrue(recovery.exists())

    def test_status_separates_registration_service_health_and_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            old = root / "missing-python"
            unit.write_text(
                render_systemd_unit(
                    paths=paths,
                    interpreter=old,
                    env={"PATH": "/usr/bin"},
                ),
                encoding="utf-8",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=True
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=123
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health", return_value=(True, "healthy")
                ),
            ):
                status = autostart_status(
                    paths=paths, unit_path=unit, interpreter=root / "current-python"
                )

            self.assertTrue(status.installed)
            self.assertTrue(status.enabled)
            self.assertTrue(status.active)
            self.assertTrue(status.healthy)
            self.assertFalse(status.definition_current)

    def test_status_reports_active_owned_service_when_unit_was_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            identity = ManagedServiceIdentity(
                "systemd", "runtime", True, True, False, True, True, command, paths.home, 321
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(True, "healthy"),
                ) as health,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=321
                ),
            ):
                result = autostart_status(
                    paths=paths, unit_path=unit, interpreter=command.interpreter
                )

            self.assertFalse(result.installed)
            self.assertTrue(result.active)
            self.assertTrue(result.healthy)
            self.assertFalse(result.definition_current)
            health.assert_called_once_with(
                host=command.host, port=command.port, paths=paths, expected_pid=321
            )

    def test_disable_reports_detached_daemon_remains_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            absent = ManagedServiceIdentity(
                "systemd", "absent", False, False, False, False, False, None, None
            )
            detached = DaemonStatus(True, {"pid": 555, "manager": "detached"}, "running")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=absent),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=detached
                ),
            ):
                result = disable_autostart(paths=paths, unit_path=root / SERVICE_NAME)

            self.assertIn("detached daemon pid 555 remains running", result.detail)

    def test_wait_for_health_binds_readiness_to_stable_systemd_main_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid",
                    side_effect=[123, 123],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._health",
                    return_value=(True, "healthy"),
                ) as health,
            ):
                _wait_for_health("127.0.0.1", 8765, paths, 1.0)

            health.assert_called_once_with(
                host="127.0.0.1", port=8765, paths=paths, expected_pid=123
            )

    def test_restart_restores_detached_daemon_when_systemd_start_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            definition = root / SERVICE_NAME
            definition.write_text(
                render_systemd_unit(
                    paths=paths,
                    interpreter=command.interpreter,
                    env={"PATH": "/usr/bin"},
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "systemd", "definition", True, True, True, False, True, command, paths.home
            )
            detached = DaemonStatus(
                True,
                {
                    "pid": 91,
                    "manager": "detached",
                    "host": command.host,
                    "port": command.port,
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=detached,
                ),
                mock.patch("agent_collab.daemon_autostart_systemd.stop_daemon") as stop,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=AutostartError("candidate failed"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._cleanup_failed_manual_candidate"
                ) as cleanup,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._restore_manual_daemon"
                ) as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "candidate failed"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            stop.assert_called_once_with(paths, _lifecycle_locked=True)
            cleanup.assert_called_once_with(paths, definition, command, set())
            restore.assert_called_once_with(paths, detached.state)

    def test_restart_cleans_up_when_final_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            definition = root / SERVICE_NAME
            definition.write_text(
                render_systemd_unit(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "systemd", "definition", True, True, True, False, True, command, paths.home
            )
            unhealthy = AutostartStatus(
                True, True, False, False, True, definition, "inactive", "systemd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=unhealthy,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._cleanup_failed_manual_candidate"
                ) as cleanup,
            ):
                with self.assertRaisesRegex(AutostartError, "did not remain"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            cleanup.assert_called_once_with(paths, definition, command, set())

    def test_restart_proves_the_captured_systemd_pid_exited_before_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            definition = root / SERVICE_NAME
            definition.write_text(
                render_systemd_unit(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "systemd",
                "definition+runtime",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
                812,
            )
            order = []
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._stop_managed_and_reap",
                    side_effect=lambda *_args: order.append("stop-proof"),
                ) as stopped,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda action, *_args, **_kwargs: (
                        order.append(action) or self._completed()
                    ),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=AutostartStatus(
                        True, True, True, True, True, definition, "healthy"
                    ),
                ),
            ):
                restart_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            stopped.assert_called_once_with(paths, 812)
            self.assertLess(order.index("stop-proof"), order.index("start"))

    def test_install_quiesce_proves_captured_systemd_pid_exited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            definition = root / SERVICE_NAME
            definition.write_text(
                render_systemd_unit(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "systemd",
                "definition+runtime",
                True,
                True,
                True,
                True,
                False,
                command,
                paths.home,
                913,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_systemd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._stop_managed_and_reap"
                ) as stopped,
            ):
                quiesce_for_install_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            stopped.assert_called_once_with(paths, 913)

    def test_failed_install_restores_previously_enabled_stopped_systemd_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": False,
                "mutation_succeeded": False,
            }
            expected = AutostartStatus(
                True, True, False, False, True, root / SERVICE_NAME, "installed but inactive"
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._ensure_durable_install"
                ) as durable,
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    side_effect=[False, True, False],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=expected,
                ),
            ):
                result = restore_after_install_locked(
                    snapshot,
                    paths=paths,
                    definition_path=root / SERVICE_NAME,
                    interpreter=root / "venv" / "bin" / "python",
                )

            self.assertEqual(result, expected)
            durable.assert_called_once_with(root / "venv" / "bin" / "python")
            systemctl.assert_called_once_with("enable", SERVICE_NAME)

    def test_failed_install_keeps_disabled_fail_safe_when_stopped_restore_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": False,
                "mutation_succeeded": False,
            }
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._ensure_durable_install",
                    side_effect=AutostartError("interpreter broken"),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
            ):
                with self.assertRaisesRegex(
                    AutostartError, "prior enabled but stopped.*remains disabled"
                ):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=root / SERVICE_NAME,
                        interpreter=root / "venv" / "bin" / "python",
                    )

            systemctl.assert_called_once_with("disable", "--now", SERVICE_NAME, check=False)

    def test_failed_install_restores_previously_running_systemd_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": True,
                "command": command,
                "mutation_succeeded": False,
            }
            expected = AutostartStatus(True, True, True, True, True, definition, "healthy")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl") as systemctl,
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health") as ready,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=expected,
                ),
            ):
                result = restore_after_install_locked(
                    snapshot,
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertEqual(result, expected)
            self.assertEqual(
                systemctl.call_args_list,
                [mock.call("start", SERVICE_NAME), mock.call("enable", SERVICE_NAME)],
            )
            ready.assert_called_once_with(
                command.host, command.port, paths, 5.0, observed_pids=mock.ANY
            )

    def test_failed_install_accepts_pid_bound_legacy_systemd_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": True,
                "command": command,
                "mutation_succeeded": False,
            }
            new_ready_unavailable = AutostartStatus(
                True, True, True, False, True, definition, "ready endpoint not found"
            )
            runtime = DaemonStatus(True, {"pid": 444, "manager": "systemd"}, "running")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=_ReadyEndpointUnavailableError("ready endpoint not found"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_legacy_health",
                    return_value=444,
                ) as legacy_ready,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=new_ready_unavailable,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=444
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status", return_value=runtime
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._cleanup_failed_candidate"
                ) as cleanup,
            ):
                result = restore_after_install_locked(
                    snapshot,
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertTrue(result.healthy)
            self.assertIn("legacy authenticated endpoint", result.detail)
            legacy_ready.assert_called_once_with(
                command.host, command.port, paths, 5.0, observed_pids=mock.ANY
            )
            cleanup.assert_not_called()

    def test_failed_install_does_not_use_legacy_fallback_for_modern_identity_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": True,
                "command": command,
                "mutation_succeeded": False,
            }
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_systemd._systemctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_health",
                    side_effect=AutostartError("readiness manager mismatch"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._wait_for_legacy_health"
                ) as legacy_ready,
                mock.patch("agent_collab.daemon_autostart_systemd._cleanup_failed_candidate"),
            ):
                with self.assertRaisesRegex(SafeManagedRestoreError, "autostart remains disabled"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            legacy_ready.assert_not_called()

    def test_install_restore_rejects_missing_or_stale_systemd_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            command = ManagedCommand(
                root / "venv" / "bin" / "python", "systemd", "127.0.0.1", 8765, None
            )
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": False,
                "running": False,
                "command": command,
            }
            stale = AutostartStatus(False, False, False, False, False, definition, "missing")
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=stale,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ) as systemctl,
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "could not prove"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            self.assertNotIn(
                mock.call("disable", SERVICE_NAME, check=False), systemctl.call_args_list
            )

    def test_inspect_reports_and_preserves_foreign_recovery_beside_current_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            foreign = recovery_paths(unit)[0]
            foreign.write_text(
                render_systemd_unit(
                    paths=foreign_paths,
                    interpreter=root / "foreign-venv" / "bin" / "python",
                    env={"PATH": "/usr/bin"},
                ),
                encoding="utf-8",
            )
            foreign.chmod(0o600)
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
            ):
                identity = systemd_inspect(
                    paths=paths, definition_path=unit, interpreter=interpreter
                )
                reported = autostart_status(paths=paths, unit_path=unit, interpreter=interpreter)

            self.assertTrue(identity.current_home_owned)
            self.assertEqual(identity.source, "definition+recovery")
            self.assertIn(str(foreign), identity.detail)
            self.assertIn(str(foreign), reported.detail)
            self.assertTrue(foreign.exists())

    def test_inspect_reports_and_preserves_malformed_recovery_beside_current_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            collision = recovery_paths(unit)[0]
            collision.write_text("not a managed unit\n", encoding="utf-8")
            collision.chmod(0o600)

            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    return_value=False,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
            ):
                identity = systemd_inspect(
                    paths=paths, definition_path=unit, interpreter=interpreter
                )

            self.assertTrue(identity.current_home_owned)
            self.assertIn("preserved unowned systemd recovery collision", identity.detail)
            self.assertEqual(collision.read_text(encoding="utf-8"), "not a managed unit\n")

    def test_takeover_enable_preserves_unsnapshotted_foreign_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            foreign = recovery_paths(unit)[0]
            foreign_bytes = render_systemd_unit(
                paths=foreign_paths,
                interpreter=root / "foreign-venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            foreign.write_text(foreign_bytes, encoding="utf-8")
            foreign.chmod(0o600)
            expected = AutostartStatus(True, True, True, True, True, unit, "healthy")
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_durable_install"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    return_value=self._completed(),
                ),
                mock.patch("agent_collab.daemon_autostart_systemd._wait_for_health"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.autostart_status",
                    return_value=expected,
                ),
            ):
                result = enable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                    takeover=True,
                )

            self.assertEqual(result, expected)
            self.assertEqual(foreign.read_text(encoding="utf-8"), foreign_bytes)

    def test_disable_finishes_after_last_resort_quarantine_and_reports_lost_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            unit = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            unit.write_text(
                render_systemd_unit(paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}),
                encoding="utf-8",
            )
            for index, recovery in enumerate(recovery_paths(unit)):
                foreign_paths = GlobalDataPaths.resolve(
                    env={"AGENT_COLLAB_HOME": str(root / f"foreign-home-{index}")}
                )
                recovery.write_text(
                    render_systemd_unit(
                        paths=foreign_paths,
                        interpreter=root / f"foreign-venv-{index}" / "bin" / "python",
                        env={"PATH": "/usr/bin"},
                    ),
                    encoding="utf-8",
                )
                recovery.chmod(0o600)
            commands = []
            with (
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_supported"),
                mock.patch("agent_collab.daemon_autostart_systemd._ensure_systemd_user_manager"),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl",
                    side_effect=lambda *args, **_kwargs: commands.append(args) or self._completed(),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemd_main_pid", return_value=None
                ),
            ):
                result = disable_autostart(
                    paths=paths,
                    unit_path=unit,
                    interpreter=interpreter,
                    takeover=True,
                )

            self.assertFalse(unit.exists())
            self.assertIn("critical: exact prior systemd unit bytes were lost", result.detail)
            self.assertIn(("stop", SERVICE_NAME), commands)
            self.assertIn(("daemon-reload",), commands)
            self.assertTrue(all(recovery.exists() for recovery in recovery_paths(unit)))

    def test_quarantine_uses_fallback_when_primary_recovery_is_unowned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            content = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            definition.write_text(content, encoding="utf-8")
            primary, fallback = recovery_paths(definition)
            primary.write_text("unowned collision", encoding="utf-8")

            selected = _quarantine_unit(definition, paths, interpreter, authorized_recoveries={})

            self.assertEqual(selected, fallback)
            self.assertEqual(primary.read_text(encoding="utf-8"), "unowned collision")
            self.assertEqual(fallback.read_text(encoding="utf-8"), content)

    def test_quarantine_preserves_a_primary_collision_created_at_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            content = render_systemd_unit(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            definition.write_text(content, encoding="utf-8")
            primary, fallback = recovery_paths(definition)

            def race_primary(source, destination):
                if destination == primary:
                    primary.write_text("concurrent collision", encoding="utf-8")
                    raise FileExistsError(primary)
                atomic_rename_noreplace(source, destination)

            with mock.patch(
                "agent_collab.daemon_autostart_systemd.atomic_rename_noreplace",
                side_effect=race_primary,
            ):
                selected = _quarantine_unit(
                    definition, paths, interpreter, authorized_recoveries={}
                )

            self.assertEqual(selected, fallback)
            self.assertEqual(primary.read_text(encoding="utf-8"), "concurrent collision")
            self.assertEqual(fallback.read_text(encoding="utf-8"), content)

    def test_inspect_reports_and_preserves_unowned_recovery_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / SERVICE_NAME
            interpreter = root / "venv" / "bin" / "python"
            collision = recovery_paths(definition)[0]
            collision.write_text("not a managed unit", encoding="utf-8")
            collision.chmod(0o600)

            with (
                mock.patch(
                    "agent_collab.daemon_autostart_systemd._systemctl_truth",
                    return_value=False,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_systemd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
            ):
                identity = systemd_inspect(
                    paths=paths,
                    definition_path=definition,
                    interpreter=interpreter,
                )

            self.assertFalse(identity.owned)
            self.assertIn("preserved unowned systemd recovery collision", identity.detail)
            self.assertIn(str(collision), identity.detail)
            self.assertEqual(collision.read_text(encoding="utf-8"), "not a managed unit")


if __name__ == "__main__":
    unittest.main()
