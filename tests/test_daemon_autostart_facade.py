import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_collab.daemon_autostart import (
    AutostartError,
    _reject_reserved_endpoint,
    autostart_status,
    enable_autostart,
    managed_service_identity,
    selected_manager,
    service_transaction,
    service_manager_owns_daemon,
)
from agent_collab.daemon_service import ManagedCommand, ManagedServiceIdentity, endpoints_overlap
from agent_collab.paths import GlobalDataPaths


class DaemonAutostartFacadeTests(unittest.TestCase):
    def test_platform_selection_is_closed_and_actionable(self):
        self.assertEqual(selected_manager("linux"), "systemd")
        self.assertEqual(selected_manager("linux2"), "systemd")
        self.assertEqual(selected_manager("darwin"), "launchd")
        with self.assertRaisesRegex(AutostartError, "Linux.*macOS"):
            selected_manager("win32")

    def test_unsupported_platform_does_not_block_detached_lifecycle(self):
        with mock.patch("sys.platform", "win32"):
            self.assertFalse(service_manager_owns_daemon())

    def test_live_cross_platform_manager_is_not_routed_to_native_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(Path(tmp) / "home")})
            with (
                mock.patch("sys.platform", "linux"),
                mock.patch(
                    "agent_collab.daemon_autostart.managed_service_identity",
                    side_effect=AutostartError(
                        "runtime state is owned by launchd on this Linux host; recover manually"
                    ),
                ),
            ):
                with self.assertRaisesRegex(AutostartError, "owned by launchd"):
                    service_manager_owns_daemon(paths=paths)

    def test_current_home_runtime_only_identity_routes_to_native_backend(self):
        identity = ManagedServiceIdentity(
            "systemd",
            "runtime",
            True,
            True,
            False,
            False,
            False,
            ManagedCommand(Path("/venv/bin/python"), "systemd", "127.0.0.1", 8765, None),
            Path("/home/current"),
            4242,
        )
        with mock.patch(
            "agent_collab.daemon_autostart.managed_service_identity", return_value=identity
        ):
            self.assertTrue(service_manager_owns_daemon())

    def test_foreign_registration_reserves_overlapping_endpoint_only(self):
        command = ManagedCommand(Path("/other/venv/bin/python"), "launchd", "0.0.0.0", 8765, None)
        identity = ManagedServiceIdentity(
            "launchd",
            "definition",
            True,
            False,
            True,
            False,
            True,
            command,
            Path("/other/home"),
        )
        with self.assertRaisesRegex(AutostartError, "reserved"):
            _reject_reserved_endpoint(identity, "127.0.0.1", 8765)
        _reject_reserved_endpoint(identity, "127.0.0.1", 9876)

    def test_recovery_only_identity_does_not_reserve_endpoint(self):
        command = ManagedCommand(Path("/venv/bin/python"), "launchd", "127.0.0.1", 8765, None)
        recovery = ManagedServiceIdentity(
            "launchd",
            "recovery",
            True,
            True,
            False,
            False,
            False,
            command,
            Path("/home/current"),
        )

        _reject_reserved_endpoint(recovery, "127.0.0.1", 8765)

    def test_indeterminate_owned_registration_refuses_detached_lifecycle(self):
        identity = ManagedServiceIdentity(
            "systemd", "definition+runtime", True, False, True, True, True, None, None
        )

        with self.assertRaisesRegex(AutostartError, "indeterminate command identity"):
            _reject_reserved_endpoint(identity, "127.0.0.1", 8765)

    def test_read_only_discovery_does_not_take_mutation_locks(self):
        backend = mock.Mock(MANAGER="launchd")
        definition = Path("/tmp/agent-collab.plist")
        identity = ManagedServiceIdentity(
            "launchd", "none", False, False, False, False, False, None, None
        )
        status = mock.sentinel.status
        backend.inspect.return_value = identity
        backend.status.return_value = status
        with (
            mock.patch("agent_collab.daemon_autostart._backend", return_value=backend),
            mock.patch("agent_collab.daemon_autostart._definition_path", return_value=definition),
            mock.patch("agent_collab.daemon_autostart._reject_cross_platform_runtime"),
            mock.patch(
                "agent_collab.daemon_autostart.lifecycle_transaction",
                side_effect=AssertionError("read-only discovery acquired lifecycle lock"),
            ),
            mock.patch(
                "agent_collab.daemon_autostart.registration_transaction",
                side_effect=AssertionError("read-only discovery acquired registration lock"),
            ),
        ):
            self.assertIs(managed_service_identity(), identity)
            self.assertIs(autostart_status(), status)

    def test_ipv4_and_ipv6_wildcards_overlap_only_their_socket_family(self):
        self.assertTrue(endpoints_overlap("0.0.0.0", 8765, "127.0.0.1", 8765))
        self.assertTrue(endpoints_overlap("::", 8765, "::1", 8765))
        self.assertTrue(endpoints_overlap("0:0:0:0:0:0:0:0", 8765, "::1", 8765))
        self.assertFalse(endpoints_overlap("::", 8765, "127.0.0.1", 8765))

    def test_service_transaction_uses_global_manager_lock_for_explicit_home(self):
        backend = mock.Mock(MANAGER="launchd")
        with tempfile.TemporaryDirectory() as tmp:
            paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(Path(tmp) / "home")})
            definition = Path(tmp) / "Library" / "LaunchAgents" / "agent-collab.plist"
            with (
                mock.patch("agent_collab.daemon_autostart._backend", return_value=backend),
                mock.patch(
                    "agent_collab.daemon_autostart._definition_path", return_value=definition
                ),
                mock.patch("agent_collab.daemon_autostart.lifecycle_transaction") as lifecycle,
                mock.patch(
                    "agent_collab.daemon_autostart.registration_transaction"
                ) as registration,
            ):
                lifecycle.return_value.__enter__.return_value = None
                registration.return_value.__enter__.return_value = None
                with service_transaction("install", paths=paths) as values:
                    self.assertEqual(values[1], definition)

            registration.assert_called_once_with("launchd", "install")

    def test_service_transaction_allows_detached_only_work_on_unsupported_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(Path(tmp) / "home")})
            with mock.patch("sys.platform", "freebsd"):
                with service_transaction("install", paths=paths) as (backend, path, *_rest):
                    self.assertIsNone(backend)
                    self.assertIsNone(path)

    def test_enable_validates_durable_interpreter_inside_both_locks(self):
        events = []

        @contextmanager
        def locked(name):
            events.append(f"enter {name}")
            try:
                yield
            finally:
                events.append(f"exit {name}")

        backend = mock.Mock(MANAGER="launchd")
        expected = mock.sentinel.status
        backend.enable_locked.return_value = expected
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "home")})
            interpreter = root / "venv" / "bin" / "python"
            with (
                mock.patch("agent_collab.daemon_autostart._backend", return_value=backend),
                mock.patch(
                    "agent_collab.daemon_autostart.lifecycle_transaction",
                    side_effect=lambda *_args: locked("lifecycle"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart.registration_transaction",
                    side_effect=lambda *_args: locked("registration"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart._ensure_durable_install",
                    side_effect=lambda *_args: events.append("validate"),
                ),
                mock.patch(
                    "agent_collab.daemon_autostart._definition_path",
                    return_value=root / "agent-collab.plist",
                ),
            ):
                result = enable_autostart(paths=paths, interpreter=interpreter)

        self.assertIs(result, expected)
        self.assertEqual(
            events,
            [
                "enter lifecycle",
                "enter registration",
                "validate",
                "exit registration",
                "exit lifecycle",
            ],
        )


if __name__ == "__main__":
    unittest.main()
