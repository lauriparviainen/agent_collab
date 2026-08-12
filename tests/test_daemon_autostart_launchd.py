import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon_autostart_launchd import (
    LABEL,
    PLIST_MARKER,
    LaunchdSnapshot,
    _bootout_and_wait,
    _bootout_matching,
    _cleanup_candidate,
    _disabled_override,
    _post_commit_recovery_cleanup,
    _quarantine_definition,
    definition_identity,
    disable_locked,
    enable_locked,
    inspect as launchd_inspect,
    launchd_snapshot,
    parse_launchd_plist,
    quiesce_for_install_locked,
    recovery_paths,
    render_launchd_plist,
    restart_locked,
    restore_after_install_locked,
    start_locked,
    status,
    stop_locked,
)
from agent_collab.daemon_service import (
    AutostartError,
    AutostartStatus,
    EndpointInUseError,
    ManagedCommand,
    ManagedServiceIdentity,
    atomic_rename_noreplace,
)
from agent_collab.daemon_supervisor import DaemonStatus
from agent_collab.paths import GlobalDataPaths


class LaunchdAutostartTests(unittest.TestCase):
    def setUp(self):
        lifecycle = mock.patch(
            "agent_collab.daemon_autostart_launchd.lifecycle_lock_held", return_value=True
        )
        registration = mock.patch(
            "agent_collab.daemon_autostart_launchd.registration_lock_held", return_value=True
        )
        lifecycle.start()
        registration.start()
        self.addCleanup(lifecycle.stop)
        self.addCleanup(registration.stop)

    def _paths(self, root: Path) -> GlobalDataPaths:
        return GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "agent home")})

    def _command(self, root: Path) -> ManagedCommand:
        return ManagedCommand(
            interpreter=root / "venv" / "bin" / "python",
            manager="launchd",
            host="127.0.0.1",
            port=8765,
            default_workdir=None,
        )

    def test_launchd_mutation_requires_both_transaction_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            with mock.patch(
                "agent_collab.daemon_autostart_launchd.registration_lock_held",
                return_value=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "requires lifecycle and registration"):
                    start_locked(
                        paths=paths,
                        definition_path=root / f"{LABEL}.plist",
                        interpreter=root / "venv" / "bin" / "python",
                    )

    def test_inspect_routes_current_home_runtime_when_definition_and_job_are_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            runtime = DaemonStatus(
                True,
                {
                    "pid": 4242,
                    "manager": "launchd",
                    "home": str(paths.home),
                    "argv": [
                        str(command.interpreter),
                        "-m",
                        "agent_collab.cli",
                        "daemon",
                        "run",
                        "--manager",
                        "launchd",
                        "--host",
                        command.host,
                        "--port",
                        str(command.port),
                    ],
                },
                "running",
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(False, None, None, False, False),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
            ):
                identity = launchd_inspect(
                    paths=paths,
                    definition_path=root / f"{LABEL}.plist",
                    interpreter=command.interpreter,
                )

            self.assertEqual(identity.source, "runtime")
            self.assertTrue(identity.owned)
            self.assertTrue(identity.current_home_owned)
            self.assertFalse(identity.installed)
            self.assertFalse(identity.loaded)
            self.assertEqual(identity.command, command)
            self.assertEqual(identity.pid, 4242)

    def test_post_commit_recovery_cleanup_reports_failures_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            recovery = root / "recovery"
            recovery.mkdir()
            with mock.patch(
                "agent_collab.daemon_autostart_launchd._remove_authorized_recoveries",
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

    def test_bootout_service_not_found_is_ignored_only_without_a_captured_pid(self):
        missing = subprocess.CompletedProcess([], 1, "", "Could not find service")
        with (
            mock.patch("agent_collab.daemon_autostart_launchd._launchctl", return_value=missing),
            mock.patch("agent_collab.daemon_autostart_launchd._wait_for_pid_exit") as wait,
        ):
            _bootout_and_wait(None)
            wait.assert_called_once_with(None)
            with self.assertRaisesRegex(AutostartError, "Could not find service"):
                _bootout_and_wait(123)

    def test_definition_identity_rejects_a_canonical_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            target = root / "target.plist"
            target.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            canonical = root / f"{LABEL}.plist"
            canonical.symlink_to(target)
            with self.assertRaisesRegex(AutostartError, "cannot open LaunchAgent definition"):
                definition_identity(canonical)

    def test_quarantine_restores_a_canonical_inode_raced_during_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            canonical = root / f"{LABEL}.plist"
            original = render_launchd_plist(
                paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
            )
            replacement = original.replace("<string>127.0.0.1</string>", "<string>::1</string>")
            canonical.write_text(original, encoding="utf-8")
            rename_calls = 0

            def raced_replace(source, target):
                nonlocal rename_calls
                if rename_calls == 0:
                    canonical.write_text(replacement, encoding="utf-8")
                rename_calls += 1
                return atomic_rename_noreplace(source, target)

            with mock.patch(
                "agent_collab.daemon_autostart_launchd.atomic_rename_noreplace",
                side_effect=raced_replace,
            ):
                with self.assertRaisesRegex(AutostartError, "identity changed"):
                    _quarantine_definition(
                        canonical,
                        paths,
                        command.interpreter,
                        authorized_recoveries={},
                        expected_content=original,
                    )
            self.assertEqual(canonical.read_text(encoding="utf-8"), replacement)
            self.assertFalse(any(path.exists() for path in recovery_paths(canonical)))

    def test_bootout_revalidates_loaded_command_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._command(root)
            replacement = ManagedCommand(
                expected.interpreter,
                expected.manager,
                expected.host,
                expected.port + 1,
                expected.default_workdir,
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(True, 999, replacement, True, False),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait") as bootout,
            ):
                with self.assertRaisesRegex(AutostartError, "loaded command changed"):
                    _bootout_matching(expected)

            bootout.assert_not_called()

    def test_bootout_waits_for_captured_pid_after_external_unload(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._command(Path(tmp))
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.time.monotonic",
                    side_effect=[0.0, 13.0],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._pid_alive", return_value=True),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
            ):
                with self.assertRaisesRegex(AutostartError, "pid 999 did not exit"):
                    _bootout_matching(expected, 999)

            launchctl.assert_not_called()

            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._pid_alive", return_value=False),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
            ):
                _bootout_matching(expected, 999)
            launchctl.assert_not_called()

    def test_candidate_cleanup_proves_every_observed_launchd_pid_exited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            candidate = LaunchdSnapshot(True, 222, command, True, False)

            def wait(pid):
                if pid == 111:
                    raise AutostartError("old generation remains")

            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=candidate,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_pid_exit",
                    side_effect=wait,
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "old generation remains"):
                    _cleanup_candidate(paths, command, {111, 222})

    def test_plist_uses_foreground_launchd_shape_and_selected_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            interpreter = root / "venv with spaces" / "bin" / "python"
            text = render_launchd_plist(
                paths=paths,
                interpreter=interpreter,
                env={"PATH": "/custom/bin:/usr/bin", "SECRET": "must-not-leak"},
                default_workdir=root / "work & review",
            )

        self.assertIn(PLIST_MARKER, text)
        document = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(document["Label"], LABEL)
        self.assertEqual(document["ProgramArguments"][0], str(interpreter))
        self.assertEqual(
            document["ProgramArguments"][1:7],
            ["-m", "agent_collab.cli", "daemon", "run", "--manager", "launchd"],
        )
        self.assertEqual(
            document["EnvironmentVariables"],
            {"PATH": "/custom/bin:/usr/bin", "AGENT_COLLAB_HOME": str(paths.home)},
        )
        self.assertEqual(document["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(document["ExitTimeOut"], 10)
        self.assertNotIn("ProcessType", document)
        self.assertNotIn("WorkingDirectory", document)
        self.assertNotIn("SECRET", text)

    @unittest.skipUnless(sys.platform == "darwin", "native plutil is macOS-only")
    def test_rendered_plist_passes_native_plutil_lint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = render_launchd_plist(
                paths=self._paths(root),
                interpreter=root / "venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            candidate = root / "agent.plist"
            candidate.write_text(text, encoding="utf-8")
            result = subprocess.run(
                ["plutil", "-lint", str(candidate)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_parse_requires_marker_label_manager_and_current_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            interpreter = root / "venv" / "bin" / "python"
            text = render_launchd_plist(
                paths=paths,
                interpreter=interpreter,
                env={"PATH": "/usr/bin"},
            )
            identity = parse_launchd_plist(
                text, current_paths=paths, expected_interpreter=interpreter
            )
            self.assertTrue(identity.current_home_owned)
            self.assertEqual(identity.command.manager, "launchd")
            self.assertEqual(identity.effective_home, paths.home)

            with self.assertRaisesRegex(AutostartError, "not owned"):
                parse_launchd_plist(text.replace(PLIST_MARKER, ""))
            with self.assertRaisesRegex(AutostartError, "expected 'launchd'"):
                parse_launchd_plist(
                    text.replace("<string>launchd</string>", "<string>systemd</string>")
                )

    def test_launchctl_print_parser_recovers_pid_and_argument_boundaries(self):
        output = """gui/501/io.github.lauriparviainen.agent-collab = {
    arguments = {
        0 = \"/tmp/venv with spaces/bin/python\"
        1 = -m
        2 = agent_collab.cli
        3 = daemon
        4 = run
        5 = --manager
        6 = launchd
        7 = --host
        8 = 127.0.0.1
        9 = --port
        10 = 9000
    }
    environment = {
        AGENT_COLLAB_HOME => /tmp/agent home
    }
    pid = 4321
}
"""
        completed = subprocess.CompletedProcess([], 0, output, "")
        with (
            mock.patch(
                "agent_collab.daemon_autostart_launchd._disabled_override",
                return_value=False,
            ),
            mock.patch(
                "agent_collab.daemon_autostart_launchd._launchctl",
                return_value=completed,
            ),
        ):
            snapshot = launchd_snapshot()

        self.assertTrue(snapshot.loaded)
        self.assertTrue(snapshot.attributable)
        self.assertEqual(snapshot.pid, 4321)
        self.assertEqual(snapshot.command.interpreter, Path("/tmp/venv with spaces/bin/python"))
        self.assertEqual(snapshot.command.port, 9000)
        self.assertEqual(snapshot.effective_home, Path("/tmp/agent home").resolve())

    def test_launchctl_print_parser_accepts_bare_argument_array_entries(self):
        output = """gui/501/io.github.lauriparviainen.agent-collab = {
    arguments = {
        \"/tmp/venv with spaces/bin/python\"
        -m
        agent_collab.cli
        daemon
        run
        --manager
        launchd
        --host
        ::1
        --port
        9000
    }
    pid = 4321
}
"""
        completed = subprocess.CompletedProcess([], 0, output, "")
        with (
            mock.patch(
                "agent_collab.daemon_autostart_launchd._disabled_override",
                return_value=False,
            ),
            mock.patch(
                "agent_collab.daemon_autostart_launchd._launchctl",
                return_value=completed,
            ),
        ):
            snapshot = launchd_snapshot()

        self.assertTrue(snapshot.attributable)
        self.assertEqual(snapshot.command.interpreter, Path("/tmp/venv with spaces/bin/python"))
        self.assertEqual(snapshot.command.host, "::1")
        self.assertEqual(snapshot.command.port, 9000)

    def test_disabled_override_parser_is_structural_and_fail_closed(self):
        cases = [
            ('disabled services = {\n    "other.label" => true\n}\n', False),
            ("disabled services = {\n    malformed unrelated entry\n}\n", False),
            (f'disabled services = {{\n    "{LABEL}" => true\n}}\n', True),
            (f'disabled services = {{\n    "{LABEL}" => false\n}}\n', False),
            (f'disabled services = {{\n    "{LABEL}" => disabled\n}}\n', True),
            (f'disabled services = {{\n    "{LABEL}" => enabled\n}}\n', False),
        ]
        for output, expected in cases:
            with self.subTest(output=output):
                completed = subprocess.CompletedProcess([], 0, output, "")
                with mock.patch(
                    "agent_collab.daemon_autostart_launchd._launchctl",
                    return_value=completed,
                ):
                    self.assertEqual(_disabled_override(), expected)

        malformed = [
            "",
            "disabled services = nope\n",
            f'disabled services = {{\n    "{LABEL}" => maybe\n}}\n',
            f'disabled services = {{\n    "{LABEL}" => true\n    "{LABEL}" => false\n}}\n',
        ]
        for output in malformed:
            with self.subTest(malformed=output):
                completed = subprocess.CompletedProcess([], 0, output, "")
                with mock.patch(
                    "agent_collab.daemon_autostart_launchd._launchctl",
                    return_value=completed,
                ):
                    with self.assertRaisesRegex(AutostartError, "malformed|ambiguous|duplicate"):
                        _disabled_override()

    def test_loaded_job_home_and_arguments_override_disk_ownership_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            current = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=current.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            foreign_home = root / "foreign-home"
            foreign = ManagedCommand(
                root / "foreign-venv" / "bin" / "python",
                "launchd",
                "127.0.0.1",
                8765,
                None,
            )
            native = LaunchdSnapshot(True, 333, foreign, True, False, "", foreign_home.resolve())
            runtime = DaemonStatus(
                True,
                {
                    "pid": 333,
                    "manager": "launchd",
                    "home": str(foreign_home),
                },
                "running",
            )
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=native,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
            ):
                identity = launchd_inspect(
                    paths=paths,
                    definition_path=definition,
                    interpreter=current.interpreter,
                )

            self.assertFalse(identity.current_home_owned)
            self.assertEqual(identity.effective_home, foreign_home.resolve())
            self.assertEqual(identity.command, foreign)
            self.assertEqual(identity.source, "definition+loaded-drift")

    def test_status_binds_authenticated_readiness_to_stable_native_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd",
                "definition+loaded",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
                123,
                "loaded",
            )
            native = LaunchdSnapshot(True, 123, command, True, False)
            command.interpreter.parent.mkdir(parents=True)
            command.interpreter.touch()
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[native, native],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.readiness",
                    return_value=(True, "healthy", {"pid": 123, "manager": "launchd"}),
                ) as probe,
            ):
                result = status(
                    paths=paths,
                    definition_path=root / "agent.plist",
                    interpreter=command.interpreter,
                )

        self.assertTrue(result.healthy)
        probe.assert_called_once_with(
            host="127.0.0.1",
            port=8765,
            paths=paths,
            expected_manager="launchd",
            expected_pid=123,
        )

    def test_fresh_enable_installs_bootstraps_and_waits_for_pid_bound_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            interpreter = root / "venv" / "bin" / "python"
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            expected = AutostartStatus(
                True, True, True, True, True, definition, "healthy", "launchd"
            )
            calls = []

            def launchctl(*args, **_kwargs):
                calls.append(args)
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._launchctl",
                    side_effect=launchctl,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._disabled_override",
                    return_value=False,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[unloaded, unloaded],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready") as wait,
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=expected),
            ):
                result = enable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                    host="127.0.0.1",
                    port=8765,
                    default_workdir=None,
                    readiness_timeout=5.0,
                    takeover=False,
                )

            self.assertEqual(result, expected)
            self.assertTrue(definition.exists())
            self.assertIn(("enable", mock.ANY), calls)
            self.assertIn(("bootstrap", mock.ANY, str(definition)), calls)
            wait.assert_called_once_with(
                paths,
                "127.0.0.1",
                8765,
                5.0,
                expected_command=mock.ANY,
                observed_pids=set(),
            )

    def test_takeover_replaces_loaded_drift_even_when_plist_bytes_are_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            interpreter = root / "new-venv" / "bin" / "python"
            expected_text = render_launchd_plist(
                paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
            )
            definition.write_text(expected_text, encoding="utf-8")
            prior_command = ManagedCommand(
                root / "old-venv" / "bin" / "python",
                "launchd",
                "127.0.0.1",
                8765,
                None,
            )
            prior = LaunchdSnapshot(
                True, 444, prior_command, True, False, effective_home=paths.home
            )
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            runtime = DaemonStatus(
                True,
                {
                    "pid": 444,
                    "manager": "launchd",
                    "home": str(paths.home),
                    "host": prior_command.host,
                    "port": prior_command.port,
                },
                "running",
            )
            expected_status = AutostartStatus(
                True, True, True, True, True, definition, "healthy", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[prior, unloaded],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching") as bootout,
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.status",
                    return_value=expected_status,
                ),
            ):
                result = enable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=interpreter,
                    env={"PATH": "/usr/bin"},
                    host="127.0.0.1",
                    port=8765,
                    default_workdir=None,
                    readiness_timeout=5.0,
                    takeover=True,
                )

            self.assertEqual(result, expected_status)
            self.assertEqual(definition.read_text(encoding="utf-8"), expected_text)
            self.assertFalse(any(path.exists() for path in recovery_paths(definition)))
            bootout.assert_called_once_with(prior_command, 444)
            launchctl.assert_called_once_with("bootstrap", mock.ANY, str(definition))

    def test_fresh_enable_rolls_back_if_final_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            interpreter = root / "venv" / "bin" / "python"
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            unhealthy = AutostartStatus(
                True,
                True,
                False,
                False,
                True,
                definition,
                "process exited",
                "launchd",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[unloaded, unloaded],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=unhealthy),
                mock.patch("agent_collab.daemon_autostart_launchd._cleanup_candidate") as cleanup,
            ):
                with self.assertRaisesRegex(AutostartError, "did not remain.*healthy"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        host="127.0.0.1",
                        port=8765,
                        default_workdir=None,
                        readiness_timeout=5.0,
                        takeover=False,
                    )

            self.assertFalse(definition.exists())
            cleanup.assert_called_once()

    def test_failed_enable_restores_crlf_plist_bytes_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            prior_text = render_launchd_plist(
                paths=paths,
                interpreter=command.interpreter,
                env={"PATH": "/usr/bin"},
                port=8765,
            )
            prior_bytes = prior_text.replace("\n", "\r\n").encode()
            definition.write_bytes(prior_bytes)
            unloaded = LaunchdSnapshot(False, None, None, False, False)

            def launchctl(*args, **_kwargs):
                if args and args[0] == "bootstrap":
                    raise AutostartError("bootstrap failed")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._launchctl", side_effect=launchctl
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
            ):
                with self.assertRaisesRegex(AutostartError, "bootstrap failed"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertEqual(definition.read_bytes(), prior_bytes)

    def test_disable_quarantines_before_teardown_and_preserves_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            paths.ensure_dirs()
            paths.daemon_log_path.write_text("keep\n", encoding="utf-8")
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            interpreter = root / "venv" / "bin" / "python"
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disable,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(False, None, None, False, True),
                ),
            ):
                result = disable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=interpreter,
                    takeover=False,
                )

            self.assertFalse(result.installed)
            self.assertFalse(definition.exists())
            self.assertFalse(any(path.exists() for path in recovery_paths(definition)))
            self.assertEqual(paths.daemon_log_path.read_text(encoding="utf-8"), "keep\n")
            disable.assert_called_once_with(True)

    def test_disable_reports_detached_daemon_remains_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            identity = ManagedServiceIdentity(
                "launchd", "absent", False, False, False, False, False, None, None
            )
            detached = DaemonStatus(True, {"pid": 777, "manager": "detached"}, "running")
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=detached
                ),
            ):
                result = disable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=root / "venv" / "bin" / "python",
                    takeover=False,
                )

            self.assertIn("detached daemon pid 777 remains running", result.detail)

    def test_stop_and_disable_refuse_unloaded_same_manager_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            orphan = DaemonStatus(
                True, {"pid": 818, "manager": "launchd", "home": str(paths.home)}, "running"
            )
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            for operation in (stop_locked, disable_locked):
                with self.subTest(operation=operation.__name__):
                    with (
                        mock.patch(
                            "agent_collab.daemon_autostart_launchd._ensure_launchd_available"
                        ),
                        mock.patch(
                            "agent_collab.daemon_autostart_launchd.inspect", return_value=identity
                        ),
                        mock.patch(
                            "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                            return_value=unloaded,
                        ),
                        mock.patch(
                            "agent_collab.daemon_autostart_launchd.daemon_status",
                            return_value=orphan,
                        ),
                        mock.patch(
                            "agent_collab.daemon_autostart_launchd._set_disabled"
                        ) as disabled,
                    ):
                        kwargs = {
                            "paths": paths,
                            "definition_path": definition,
                            "interpreter": command.interpreter,
                        }
                        if operation is disable_locked:
                            kwargs["takeover"] = False
                        with self.assertRaisesRegex(AutostartError, "orphaned.*unloaded"):
                            operation(**kwargs)

                    disabled.assert_not_called()
                    self.assertTrue(definition.exists())

    def test_start_and_restart_report_runtime_orphan_before_disabled_or_missing_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd", "runtime", True, True, False, False, False, command, paths.home, 818
            )
            orphan = DaemonStatus(
                True, {"pid": 818, "manager": "launchd", "home": str(paths.home)}, "running"
            )
            unloaded = LaunchdSnapshot(False, None, None, False, True)
            for operation in (start_locked, restart_locked):
                with (
                    self.subTest(operation=operation.__name__),
                    mock.patch(
                        "agent_collab.daemon_autostart_launchd.inspect", return_value=identity
                    ),
                    mock.patch(
                        "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                        return_value=unloaded,
                    ),
                    mock.patch(
                        "agent_collab.daemon_autostart_launchd.daemon_status",
                        return_value=orphan,
                    ),
                    mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                ):
                    with self.assertRaisesRegex(AutostartError, "pid 818.*orphaned"):
                        operation(
                            paths=paths,
                            definition_path=root / f"{LABEL}.plist",
                            interpreter=command.interpreter,
                        )

                launchctl.assert_not_called()

    def test_missing_plist_start_returns_prominent_autostart_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd", "loaded", True, True, False, True, True, command, paths.home
            )
            dormant = LaunchdSnapshot(True, None, command, True, False, effective_home=paths.home)
            healthy = AutostartStatus(
                False, False, True, True, True, definition, "healthy", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=dormant
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=healthy),
            ):
                result = start_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertFalse(result.installed)
            self.assertIn("installed=false", result.detail)
            self.assertIn("autostart enable", result.detail)

    def test_start_cleans_up_when_final_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            native = LaunchdSnapshot(False, None, None, False, False)
            unhealthy = AutostartStatus(
                True, True, False, False, True, definition, "exited", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=native
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=unhealthy),
                mock.patch("agent_collab.daemon_autostart_launchd._cleanup_candidate") as cleanup,
            ):
                with self.assertRaisesRegex(AutostartError, "did not remain active"):
                    start_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            cleanup.assert_called_once_with(paths, command, set())

    def test_enable_refuses_unknown_listener_before_definition_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            interpreter = root / "venv" / "bin" / "python"
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(False, None, None, False, False),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    side_effect=EndpointInUseError("unattributable listener"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
            ):
                with self.assertRaisesRegex(AutostartError, "unattributable listener"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        host="127.0.0.1",
                        port=8765,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertFalse(definition.exists())
            launchctl.assert_not_called()

    def test_irreversible_cross_home_takeover_failure_keeps_old_plist_non_loadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            interpreter = root / "new-venv" / "bin" / "python"
            previous = render_launchd_plist(
                paths=foreign_paths,
                interpreter=root / "old-venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            definition.write_text(previous, encoding="utf-8")
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            events = []

            def record_disabled(value):
                events.append(("disabled", value))

            from agent_collab.paths import atomic_write_private_text as real_atomic_write

            def record_write(path, content):
                events.append(("write", path))
                real_atomic_write(path, content)

            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._home_has_token", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._set_disabled",
                    side_effect=record_disabled,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.atomic_write_private_text",
                    side_effect=record_write,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("candidate failed"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "non-loadable"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=interpreter,
                        env={"PATH": "/usr/bin"},
                        host="127.0.0.1",
                        port=8765,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=True,
                    )

            self.assertFalse(definition.exists())
            self.assertEqual(events[:2], [("disabled", True), ("write", definition)])
            recoveries = [path for path in recovery_paths(definition) if path.exists()]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), previous)

    def test_irreversible_takeover_barrier_failure_leaves_prior_plist_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign-home")}
            )
            previous = render_launchd_plist(
                paths=foreign_paths,
                interpreter=root / "old-venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            definition.write_text(previous, encoding="utf-8")
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._home_has_token", return_value=False
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._set_disabled",
                    side_effect=AutostartError("override denied"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.atomic_write_private_text"
                ) as write,
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
            ):
                with self.assertRaisesRegex(
                    AutostartError, "prior registration was left untouched"
                ):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=root / "new-venv" / "bin" / "python",
                        env={"PATH": "/usr/bin"},
                        host="127.0.0.1",
                        port=8765,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=True,
                    )

            self.assertEqual(definition.read_text(encoding="utf-8"), previous)
            write.assert_not_called()
            launchctl.assert_not_called()

    def test_failed_enable_does_not_reload_previously_disabled_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, True, effective_home=paths.home)
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            runtime = DaemonStatus(
                True,
                {
                    "pid": 444,
                    "manager": "launchd",
                    "home": str(paths.home),
                    "host": command.host,
                    "port": command.port,
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[prior, unloaded, prior],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_pid_exit"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("candidate failed"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "candidate failed") as raised:
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertIn("live-plus-disabled", str(raised.exception))
            self.assertEqual(
                [call for call in launchctl.call_args_list if call.args[0] == "bootstrap"],
                [mock.call("bootstrap", mock.ANY, str(definition))],
            )

    def test_failed_enable_proves_restored_prior_job_pid_bound_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, False, effective_home=paths.home)
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            runtime = DaemonStatus(
                True,
                {
                    "pid": 444,
                    "manager": "launchd",
                    "home": str(paths.home),
                    "host": command.host,
                    "port": command.port,
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[prior, unloaded, prior],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch("agent_collab.daemon_autostart_launchd._cleanup_candidate"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=[AutostartError("candidate failed"), None],
                ) as ready,
            ):
                with self.assertRaisesRegex(AutostartError, "candidate failed"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertEqual(ready.call_count, 2)
            restored = ready.call_args_list[1]
            self.assertEqual(restored.args[:4], (paths, command.host, command.port, 1.0))
            self.assertEqual(restored.kwargs, {"expected_command": command})

    def test_failed_enable_does_not_restore_prior_job_until_candidate_cleanup_is_proven(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, False, effective_home=paths.home)
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            runtime = DaemonStatus(
                True,
                {
                    "pid": 444,
                    "manager": "launchd",
                    "home": str(paths.home),
                    "host": command.host,
                    "port": command.port,
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[prior, unloaded],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("candidate failed"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._cleanup_candidate",
                    side_effect=AutostartError("pid remains alive"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._preserve_prior_bytes_nonloadably"
                ) as preserve,
                mock.patch("agent_collab.daemon_autostart_launchd._restore_manual") as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "cleanup could not be proven"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            bootstrap_calls = [
                call for call in launchctl.call_args_list if call.args[0] == "bootstrap"
            ]
            self.assertEqual(len(bootstrap_calls), 1)
            preserve.assert_called_once()
            restore.assert_not_called()

    def test_enable_failure_before_bootout_does_not_bootstrap_loaded_prior_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, False, effective_home=paths.home)
            detached = DaemonStatus(
                True,
                {
                    "pid": 77,
                    "manager": "detached",
                    "host": command.host,
                    "port": command.port,
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[prior, prior],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=detached,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.stop_daemon",
                    side_effect=AutostartError("detached stop failed"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready") as ready,
            ):
                with self.assertRaisesRegex(AutostartError, "detached stop failed"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertFalse(
                any(call.args and call.args[0] == "bootstrap" for call in launchctl.call_args_list)
            )
            ready.assert_called_once_with(
                paths,
                command.host,
                command.port,
                1.0,
                expected_command=command,
            )

    def test_idempotent_enable_failure_leaves_untouched_prior_job_enabled_and_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, False, effective_home=paths.home)
            runtime = DaemonStatus(
                True,
                {"pid": 444, "manager": "launchd", "home": str(paths.home)},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=prior
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.readiness",
                    return_value=(True, "ready", 444),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("generation changed"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching") as bootout,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "existing launchd daemon changed"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=command.port,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )
            disabled.assert_not_called()
            bootout.assert_not_called()

    def test_healthy_override_clear_does_not_roll_back_when_recovery_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, True, effective_home=paths.home)
            runtime = DaemonStatus(
                True,
                {"pid": 444, "manager": "launchd", "home": str(paths.home)},
                "running",
            )
            expected = AutostartStatus(
                True, True, True, True, True, definition, "healthy", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=prior,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=runtime,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.readiness",
                    return_value=(True, "ready", 444),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=expected),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._remove_authorized_recoveries",
                    side_effect=OSError("permission denied"),
                ),
            ):
                result = enable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                    env={"PATH": "/usr/bin"},
                    host=command.host,
                    port=command.port,
                    default_workdir=None,
                    readiness_timeout=1.0,
                    takeover=False,
                )

            self.assertTrue(result.enabled)
            self.assertTrue(result.healthy)
            self.assertIn("recovery cleanup failed: permission denied", result.detail)
            disabled.assert_called_once_with(False)

    def test_idempotent_enable_redisable_failure_quarantines_prior_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            previous = render_launchd_plist(
                paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
            )
            definition.write_text(previous, encoding="utf-8")
            prior = LaunchdSnapshot(True, 444, command, True, True, effective_home=paths.home)
            runtime = DaemonStatus(
                True,
                {"pid": 444, "manager": "launchd", "home": str(paths.home)},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=prior
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.readiness",
                    return_value=(True, "ready", 444),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("generation changed"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._set_disabled",
                    side_effect=[None, AutostartError("re-disable denied")],
                ) as disabled,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "critical.*disabled override"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=command.port,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertEqual(disabled.call_args_list, [mock.call(False), mock.call(True)])
            self.assertFalse(definition.exists())
            recoveries = [path for path in recovery_paths(definition) if path.exists()]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), previous)

    def test_failed_prior_launchd_rollback_reestablishes_disabled_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            prior = LaunchdSnapshot(True, 444, command, True, False, effective_home=paths.home)
            unloaded = LaunchdSnapshot(False, None, None, False, False)

            def launchctl(*args, **_kwargs):
                if args and args[0] == "bootstrap":
                    raise AutostartError("bootstrap failed")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[prior, unloaded, unloaded, unloaded],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(
                        True,
                        {
                            "pid": 444,
                            "manager": "launchd",
                            "home": str(paths.home),
                        },
                        "running",
                    ),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_pid_exit"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._launchctl", side_effect=launchctl
                ),
            ):
                with self.assertRaisesRegex(
                    AutostartError, "bootstrap failed.*persistently disabled"
                ):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertEqual(disabled.call_args_list[-2:], [mock.call(False), mock.call(True)])

    def test_enable_requires_takeover_for_ambiguous_loaded_job_without_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            command = self._command(root)
            native = LaunchdSnapshot(True, 123, command, True, False)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=native,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "cannot be proven.*--takeover"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=command.port,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertFalse(definition.exists())
            disabled.assert_not_called()

    def test_enable_refuses_unrelated_requested_listener_even_with_old_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            command = self._command(root)
            definition.parent.mkdir(parents=True)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            native = LaunchdSnapshot(True, 123, command, True, False)
            runtime = DaemonStatus(
                True,
                {
                    "pid": 123,
                    "manager": "launchd",
                    "host": command.host,
                    "port": command.port,
                    "home": str(paths.home),
                },
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=native,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=runtime
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    side_effect=EndpointInUseError("unrelated listener"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "unrelated listener"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            disabled.assert_not_called()

    def test_enable_redisable_failure_keeps_prior_bytes_non_loadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            definition.parent.mkdir(parents=True)
            command = self._command(root)
            previous = render_launchd_plist(
                paths=paths,
                interpreter=command.interpreter,
                env={"PATH": "/usr/bin"},
                port=command.port,
            )
            definition.write_text(previous, encoding="utf-8")
            unloaded_disabled = LaunchdSnapshot(False, None, None, False, True)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded_disabled,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._set_disabled",
                    side_effect=[None, AutostartError("re-disable denied")],
                ) as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("candidate failed"),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "critical.*disabled override"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=9000,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            self.assertEqual(
                disabled.call_args_list,
                [mock.call(False), mock.call(True)],
            )
            self.assertFalse(definition.exists())
            recoveries = [path for path in recovery_paths(definition) if path.exists()]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_text(encoding="utf-8"), previous)

    def test_enable_cleans_partially_loaded_candidate_when_bootstrap_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            command = self._command(root)
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            candidate = LaunchdSnapshot(True, 555, command, True, False)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd._lint_plist"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[unloaded, unloaded, candidate, candidate],
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._launchctl",
                    side_effect=AutostartError("bootstrap reported failure"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait") as bootout,
            ):
                with self.assertRaisesRegex(AutostartError, "bootstrap reported failure"):
                    enable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        env={"PATH": "/usr/bin"},
                        host=command.host,
                        port=command.port,
                        default_workdir=None,
                        readiness_timeout=1.0,
                        takeover=False,
                    )

            bootout.assert_called_once_with(555)

    def test_restart_restores_detached_daemon_when_launchd_start_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            detached = DaemonStatus(
                True,
                {
                    "pid": 88,
                    "manager": "detached",
                    "host": command.host,
                    "port": command.port,
                    "home": str(paths.home),
                },
                "running",
            )
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=detached,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd.stop_daemon") as stop,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("candidate failed"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._restore_manual") as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "candidate failed"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            stop.assert_called_once_with(paths, _lifecycle_locked=True)
            restore.assert_called_once_with(paths, detached)

    def test_restart_cleans_up_when_final_status_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            native = LaunchdSnapshot(False, None, None, True, False)
            unhealthy = AutostartStatus(
                True, True, False, False, True, definition, "exited", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=native
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=unhealthy),
                mock.patch("agent_collab.daemon_autostart_launchd._cleanup_candidate") as cleanup,
            ):
                with self.assertRaisesRegex(AutostartError, "did not remain"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            cleanup.assert_called_once_with(paths, command, set())

    def test_restart_cleans_up_failed_prior_generation_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, True, True, command, paths.home
            )
            native = LaunchdSnapshot(True, 444, command, True, False)
            readiness_calls = 0

            def fail_readiness(*_args, **kwargs):
                nonlocal readiness_calls
                readiness_calls += 1
                kwargs["observed_pids"].add(555 if readiness_calls == 1 else 666)
                raise AutostartError(
                    "candidate failed" if readiness_calls == 1 else "restore failed"
                )

            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=native
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint",
                    return_value=[],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=fail_readiness,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._cleanup_candidate") as cleanup,
            ):
                with self.assertRaisesRegex(AutostartError, "prior launchd daemon restore failed"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            self.assertEqual(cleanup.call_count, 2)
            cleanup.assert_has_calls(
                [mock.call(paths, command, {555}), mock.call(paths, command, {666})]
            )

    def test_restart_does_not_restore_prior_state_when_candidate_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            prior = LaunchdSnapshot(True, 444, command, True, False)
            detached = DaemonStatus(
                True,
                {"pid": 88, "manager": "detached", "host": command.host, "port": command.port},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot", return_value=prior
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=detached
                ),
                mock.patch("agent_collab.daemon_autostart_launchd.stop_daemon"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.reserve_server_endpoint", return_value=[]
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("candidate failed"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._cleanup_candidate",
                    side_effect=AutostartError("pid remains alive"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._restore_manual") as restore,
            ):
                with self.assertRaisesRegex(AutostartError, "cleanup failed.*pid remains alive"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            bootstrap_calls = [
                call for call in launchctl.call_args_list if call.args[0] == "bootstrap"
            ]
            self.assertEqual(len(bootstrap_calls), 1)
            restore.assert_not_called()

    def test_restart_refuses_unattributable_loaded_collision_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd",
                "definition+collision",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(True, 444, None, False, False),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait") as bootout,
            ):
                with self.assertRaisesRegex(AutostartError, "loaded program is not agent-collab"):
                    restart_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            bootout.assert_not_called()

    def test_start_refuses_unattributable_loaded_collision_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd",
                "definition+collision",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(True, 444, None, False, False),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
            ):
                with self.assertRaisesRegex(AutostartError, "loaded program is not agent-collab"):
                    start_locked(
                        paths=paths,
                        definition_path=root / f"{LABEL}.plist",
                        interpreter=command.interpreter,
                    )

            launchctl.assert_not_called()

    def test_disable_refuses_unattributable_loaded_collision_before_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd",
                "definition+collision",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(True, 444, None, False, False),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "loaded program is not agent-collab"):
                    disable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        takeover=False,
                    )

            disabled.assert_not_called()

    def test_disable_refuses_loaded_job_identity_drift_before_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd",
                "definition+loaded",
                True,
                True,
                True,
                True,
                True,
                command,
                paths.home,
                444,
            )
            foreign_home = root / "foreign-home"
            drifted = ManagedCommand(
                root / "foreign-venv" / "bin" / "python",
                "launchd",
                command.host,
                command.port,
                None,
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(
                        True, 555, drifted, True, False, effective_home=foreign_home
                    ),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "identity changed"):
                    disable_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                        takeover=False,
                    )

            disabled.assert_not_called()

    def test_install_quiesce_refuses_in_memory_argument_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            drifted = ManagedCommand(
                command.interpreter, command.manager, command.host, 9999, command.default_workdir
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition+loaded", True, True, True, True, True, drifted, paths.home
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(True, 123, drifted, True, False),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "in-memory arguments differ"):
                    quiesce_for_install_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            disabled.assert_not_called()

    def test_install_quiesce_refuses_unloaded_same_manager_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            identity = ManagedServiceIdentity(
                "launchd", "definition", True, True, True, False, True, command, paths.home
            )
            orphan = DaemonStatus(
                True,
                {"pid": 321, "manager": "launchd", "home": str(paths.home)},
                "running",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(False, None, None, False, False),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status", return_value=orphan
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "orphaned"):
                    quiesce_for_install_locked(
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            disabled.assert_not_called()

    def test_failed_install_restore_reestablishes_disabled_barrier_and_cleans_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            candidate = LaunchdSnapshot(True, 777, command, True, False)
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": True,
                "command": command,
            }
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd._wait_for_ready",
                    side_effect=AutostartError("new code failed"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=candidate,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait") as bootout,
            ):
                with self.assertRaisesRegex(AutostartError, "autostart remains disabled"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            self.assertEqual(disabled.call_args_list, [mock.call(False), mock.call(True)])
            bootout.assert_called_once_with(777)

    def test_failed_install_restores_previously_enabled_stopped_service_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": False,
                "command": command,
                "mutation_succeeded": False,
            }
            definition = root / f"{LABEL}.plist"
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            expected = AutostartStatus(
                True,
                True,
                False,
                False,
                True,
                definition,
                "installed but not loaded",
                "launchd",
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(False, None, None, False, False),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=expected),
            ):
                result = restore_after_install_locked(
                    snapshot,
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertEqual(result, expected)
            disabled.assert_called_once_with(False)
            launchctl.assert_not_called()

    def test_install_preserves_recovery_only_launchd_state_without_disabled_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / f"{LABEL}.plist"
            recovery = recovery_paths(definition)[0]
            recovery_bytes = render_launchd_plist(
                paths=paths,
                interpreter=command.interpreter,
                env={"PATH": "/usr/bin"},
            ).encode()
            recovery.write_bytes(recovery_bytes)
            recovery.chmod(0o600)
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            with (
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                snapshot = quiesce_for_install_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )
                snapshot["mutation_succeeded"] = False
                result = restore_after_install_locked(
                    snapshot,
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertFalse(snapshot["installed"])
            self.assertFalse(definition.exists())
            self.assertEqual(recovery.read_bytes(), recovery_bytes)
            self.assertFalse(result.installed)
            disabled.assert_not_called()

    def test_recovery_only_install_restore_rejects_changed_recovery_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / f"{LABEL}.plist"
            recovery = recovery_paths(definition)[0]
            prior = render_launchd_plist(
                paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
            )
            changed = render_launchd_plist(
                paths=paths,
                interpreter=command.interpreter,
                env={"PATH": "/usr/bin"},
                port=9000,
            )
            recovery.write_text(changed, encoding="utf-8")
            recovery.chmod(0o600)
            snapshot = {
                "owned": True,
                "installed": False,
                "enabled": True,
                "running": False,
                "recoveries": {str(recovery): prior},
            }
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            result = AutostartStatus(
                False, False, False, False, True, definition, "recovery", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=result),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "recovery-only.*changed"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

    def test_stopped_install_restore_fails_safe_if_launchd_autoloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / f"{LABEL}.plist"
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            loaded = LaunchdSnapshot(True, 733, command, True, False)
            unloaded = LaunchdSnapshot(False, None, None, False, True)
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": False,
                "command": command,
                "mutation_succeeded": False,
            }
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[loaded, loaded, unloaded],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait") as bootout,
            ):
                with self.assertRaisesRegex(AutostartError, "remains disabled.*auto-loaded"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            self.assertEqual(disabled.call_args_list, [mock.call(False), mock.call(True)])
            bootout.assert_called_once_with(733)
            self.assertTrue(definition.exists())
            self.assertFalse(any(path.exists() for path in recovery_paths(definition)))

    def test_running_install_restore_rejects_unhealthy_final_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / f"{LABEL}.plist"
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            unloaded = LaunchdSnapshot(False, None, None, False, False)
            loaded = LaunchdSnapshot(True, 744, command, True, False)
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": True,
                "command": command,
                "mutation_succeeded": False,
            }
            unhealthy = AutostartStatus(
                True, True, True, False, True, definition, "connection refused", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl"),
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready"),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=unhealthy),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[unloaded, loaded, loaded, loaded],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_and_wait") as bootout,
            ):
                with self.assertRaisesRegex(AutostartError, "remains disabled.*did not remain"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            self.assertEqual(disabled.call_args_list, [mock.call(False), mock.call(True)])
            bootout.assert_called_once_with(744)

    def test_invalid_recovery_collision_is_reported_instead_of_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            recovery = recovery_paths(definition)[0]
            recovery.write_text("not a plist", encoding="utf-8")
            recovery.chmod(0o600)
            with mock.patch(
                "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                return_value=LaunchdSnapshot(False, None, None, False, False),
            ):
                identity = launchd_inspect(
                    paths=paths,
                    definition_path=definition,
                    interpreter=root / "python",
                )

            self.assertFalse(identity.owned)
            self.assertIn("preserved unowned", identity.detail)
            self.assertIn(str(recovery), identity.detail)

    def test_canonical_definition_does_not_hide_invalid_recovery_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            recovery = recovery_paths(definition)[0]
            recovery.write_text("not a plist", encoding="utf-8")
            recovery.chmod(0o600)
            with mock.patch(
                "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                return_value=LaunchdSnapshot(False, None, None, False, False),
            ):
                identity = launchd_inspect(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertTrue(identity.owned)
            self.assertIn("preserved unowned", identity.detail)
            self.assertIn(str(recovery), identity.detail)

    def test_takeover_disable_preserves_unrelated_foreign_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            foreign_paths = GlobalDataPaths.resolve(
                env={"AGENT_COLLAB_HOME": str(root / "foreign")}
            )
            foreign = render_launchd_plist(
                paths=foreign_paths,
                interpreter=root / "foreign-venv" / "bin" / "python",
                env={"PATH": "/usr/bin"},
            )
            primary, _fallback = recovery_paths(definition)
            primary.write_text(foreign, encoding="utf-8")
            primary.chmod(0o600)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=LaunchdSnapshot(False, None, None, False, False),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
            ):
                result = disable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                    takeover=True,
                )

            self.assertEqual(primary.read_text(encoding="utf-8"), foreign)
            self.assertIn("preserved foreign LaunchAgent recovery", result.detail)

    def test_quarantine_uses_fallback_when_primary_recovery_is_unowned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            content = render_launchd_plist(
                paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
            )
            definition.write_text(content, encoding="utf-8")
            primary, fallback = recovery_paths(definition)
            primary.write_text("unowned collision", encoding="utf-8")

            selected = _quarantine_definition(
                definition, paths, command.interpreter, authorized_recoveries={}
            )

            self.assertEqual(selected, fallback)
            self.assertEqual(primary.read_text(encoding="utf-8"), "unowned collision")
            self.assertEqual(fallback.read_text(encoding="utf-8"), content)

    def test_quarantine_preserves_a_primary_collision_created_at_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            content = render_launchd_plist(
                paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
            )
            definition.write_text(content, encoding="utf-8")
            primary, fallback = recovery_paths(definition)

            def race_primary(source, destination):
                if destination == primary:
                    primary.write_text("concurrent collision", encoding="utf-8")
                    raise FileExistsError(primary)
                atomic_rename_noreplace(source, destination)

            with mock.patch(
                "agent_collab.daemon_autostart_launchd.atomic_rename_noreplace",
                side_effect=race_primary,
            ):
                selected = _quarantine_definition(
                    definition,
                    paths,
                    command.interpreter,
                    authorized_recoveries={},
                )

            self.assertEqual(selected, fallback)
            self.assertEqual(primary.read_text(encoding="utf-8"), "concurrent collision")
            self.assertEqual(fallback.read_text(encoding="utf-8"), content)

    def test_disable_finishes_teardown_after_last_resort_definition_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            definition = root / f"{LABEL}.plist"
            command = self._command(root)
            definition.write_text(
                render_launchd_plist(
                    paths=paths, interpreter=command.interpreter, env={"PATH": "/usr/bin"}
                ),
                encoding="utf-8",
            )
            for index, recovery in enumerate(recovery_paths(definition)):
                foreign_paths = GlobalDataPaths.resolve(
                    env={"AGENT_COLLAB_HOME": str(root / f"foreign-{index}")}
                )
                recovery.write_text(
                    render_launchd_plist(
                        paths=foreign_paths,
                        interpreter=root / f"foreign-{index}-venv" / "bin" / "python",
                        env={"PATH": "/usr/bin"},
                    ),
                    encoding="utf-8",
                )
                recovery.chmod(0o600)
            native = LaunchdSnapshot(True, 919, command, True, False, effective_home=paths.home)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_launchd_available"),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=native,
                ),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.daemon_status",
                    return_value=DaemonStatus(False, {}, "stopped"),
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled"),
                mock.patch("agent_collab.daemon_autostart_launchd._bootout_matching") as bootout,
            ):
                result = disable_locked(
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                    takeover=False,
                )

            self.assertFalse(definition.exists())
            bootout.assert_called_once_with(command, 919)
            self.assertIn("critical", result.detail)
            self.assertTrue(all(path.exists() for path in recovery_paths(definition)))

    def test_ambiguous_loaded_identity_does_not_recommend_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            identity = ManagedServiceIdentity(
                "launchd", "loaded", True, False, False, True, True, command, None
            )
            with mock.patch("agent_collab.daemon_autostart_launchd.inspect", return_value=identity):
                with self.assertRaisesRegex(AutostartError, "indeterminate") as raised:
                    start_locked(
                        paths=paths,
                        definition_path=root / f"{LABEL}.plist",
                        interpreter=command.interpreter,
                    )

            self.assertNotIn("--takeover", str(raised.exception))

    def test_failed_install_restores_previously_running_launchd_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / f"{LABEL}.plist"
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": True,
                "running": True,
                "command": command,
                "mutation_succeeded": False,
            }
            expected = AutostartStatus(
                True, True, True, True, True, definition, "healthy", "launchd"
            )
            with (
                mock.patch("agent_collab.daemon_autostart_launchd._ensure_durable_install"),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
                mock.patch("agent_collab.daemon_autostart_launchd._launchctl") as launchctl,
                mock.patch("agent_collab.daemon_autostart_launchd._wait_for_ready") as ready,
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    side_effect=[
                        LaunchdSnapshot(False, None, None, False, False),
                        LaunchdSnapshot(True, 777, command, True, False),
                    ],
                ),
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=expected),
            ):
                result = restore_after_install_locked(
                    snapshot,
                    paths=paths,
                    definition_path=definition,
                    interpreter=command.interpreter,
                )

            self.assertEqual(result, expected)
            disabled.assert_called_once_with(False)
            launchctl.assert_called_once_with("bootstrap", mock.ANY, str(definition))
            ready.assert_called_once_with(
                paths,
                command.host,
                command.port,
                5.0,
                expected_command=command,
                observed_pids=mock.ANY,
            )

    def test_install_restore_rejects_stale_launchd_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            command = self._command(root)
            definition = root / f"{LABEL}.plist"
            snapshot = {
                "owned": True,
                "installed": True,
                "enabled": False,
                "running": False,
                "command": command,
            }
            stale = AutostartStatus(
                True, False, False, False, False, definition, "stale", "launchd"
            )
            unloaded = LaunchdSnapshot(False, None, None, False, True)
            with (
                mock.patch("agent_collab.daemon_autostart_launchd.status", return_value=stale),
                mock.patch(
                    "agent_collab.daemon_autostart_launchd.launchd_snapshot",
                    return_value=unloaded,
                ),
                mock.patch("agent_collab.daemon_autostart_launchd._set_disabled") as disabled,
            ):
                with self.assertRaisesRegex(AutostartError, "autostart remains disabled"):
                    restore_after_install_locked(
                        snapshot,
                        paths=paths,
                        definition_path=definition,
                        interpreter=command.interpreter,
                    )

            disabled.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
