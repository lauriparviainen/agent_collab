import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from agent_collab.daemon_supervisor import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    READY_TIMEOUT_ENV,
    DaemonSupervisorError,
    IDENTITY_UNKNOWN,
    _daemon_identity_matches,
    _daemon_identity_status,
    _daemon_start_lock,
    _ready_timeout_seconds,
    _wait_for_ready,
    daemon_status,
    run_managed_daemon,
    start_daemon,
    stop_daemon,
    tail_daemon_log,
)
from agent_collab.paths import GlobalDataPaths


class ReadyTimeoutConfigTests(unittest.TestCase):
    def test_default_when_env_is_unset_or_blank(self):
        import os

        with mock.patch.dict("os.environ"):
            os.environ.pop(READY_TIMEOUT_ENV, None)
            self.assertEqual(_ready_timeout_seconds(), DEFAULT_READY_TIMEOUT_SECONDS)
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                with mock.patch.dict("os.environ", {READY_TIMEOUT_ENV: blank}):
                    self.assertEqual(_ready_timeout_seconds(), DEFAULT_READY_TIMEOUT_SECONDS)

    def test_env_override_parses_positive_seconds(self):
        with mock.patch.dict("os.environ", {READY_TIMEOUT_ENV: "12.5"}):
            self.assertEqual(_ready_timeout_seconds(), 12.5)

    def test_invalid_or_nonpositive_values_fail_loudly(self):
        for raw in ("zero", "0", "-1", "nan"):
            with self.subTest(raw=raw):
                with mock.patch.dict("os.environ", {READY_TIMEOUT_ENV: raw}):
                    with self.assertRaises(DaemonSupervisorError):
                        _ready_timeout_seconds()


class FakeProcess:
    pid = 4242


class FailedProcess:
    pid = 4243

    def poll(self):
        return 2

    def terminate(self):
        raise AssertionError("terminated process should not be terminated again")


class ReadyProcess:
    pid = 4244

    def poll(self):
        return None


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return b'{"sessions":[]}'


class DaemonSupervisorTests(unittest.TestCase):
    PROCESS_IDENTITY = {
        "source": "procfs",
        "start_time": "123456",
        "argv": ["python", "-m", "agent_collab.cli", "serve"],
    }

    def _paths(self, tmp: str) -> GlobalDataPaths:
        return GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": tmp})

    def test_start_writes_pid_state_and_log_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)

            with (
                mock.patch(
                    "agent_collab.daemon_supervisor.subprocess.Popen", return_value=FakeProcess()
                ) as popen,
                mock.patch(
                    "agent_collab.daemon_supervisor._read_process_identity",
                    return_value=self.PROCESS_IDENTITY,
                ),
            ):
                state = start_daemon(paths, host="127.0.0.1", port=8765)

            self.assertEqual(state["pid"], 4242)
            self.assertEqual(state["home"], str(paths.home))
            self.assertIsNone(state["default_workdir"])
            self.assertEqual(state["data_dir"], str(paths.data_dir))
            self.assertEqual(state["session_dir"], str(paths.session_dir))
            self.assertNotIn("token_path", state)
            self.assertEqual(state["process_identity"], self.PROCESS_IDENTITY)
            self.assertEqual(paths.pid_path.read_text(encoding="utf-8").strip(), "4242")
            self.assertEqual(json.loads(paths.state_path.read_text(encoding="utf-8"))["pid"], 4242)
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertIn("serve", argv)
            self.assertIn("--session-log-dir", argv)
            self.assertNotIn("--token-path", argv)
            self.assertNotIn("--workdir", argv)
            self.assertEqual(popen.call_args.kwargs["cwd"], str(paths.home))
            self.assertIn("agent_collab", env["PYTHONPATH"])
            self.assertTrue(paths.daemon_log_path.exists())
            self.assertTrue(paths.daemon_stderr_path.exists())
            self.assertEqual(paths.pid_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.daemon_dir.stat().st_mode & 0o777, 0o700)

    def test_managed_foreground_daemon_writes_systemd_state_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            observed = {}

            def fake_run_server(*_args, **_kwargs):
                observed.update(json.loads(paths.state_path.read_text(encoding="utf-8")))

            with (
                mock.patch("agent_collab.daemon_supervisor.os.getpid", return_value=4242),
                mock.patch(
                    "agent_collab.daemon_supervisor._read_process_identity",
                    return_value=self.PROCESS_IDENTITY,
                ),
                mock.patch("agent_collab.daemon_supervisor.signal.signal"),
                mock.patch("agent_collab.server_http.run_server", side_effect=fake_run_server),
            ):
                run_managed_daemon(paths, redirect_logs=False)

            self.assertEqual(observed["pid"], 4242)
            self.assertEqual(observed["manager"], "systemd")
            self.assertEqual(observed["session_dir"], str(paths.session_dir))
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_managed_foreground_daemon_refuses_existing_live_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.state_path.write_text(
                json.dumps({"pid": 9999, "process_identity": self.PROCESS_IDENTITY}),
                encoding="utf-8",
            )
            with (
                mock.patch("agent_collab.daemon_supervisor.os.getpid", return_value=4242),
                mock.patch("agent_collab.daemon_supervisor._is_running", return_value=True),
                mock.patch(
                    "agent_collab.daemon_supervisor._daemon_identity_status",
                    return_value="match",
                ),
            ):
                with self.assertRaisesRegex(DaemonSupervisorError, "already running"):
                    run_managed_daemon(paths, redirect_logs=False)

    def test_start_passes_default_workdir_to_serve_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            workdir = Path(tmp) / "project"
            workdir.mkdir()

            with (
                mock.patch(
                    "agent_collab.daemon_supervisor.subprocess.Popen", return_value=FakeProcess()
                ) as popen,
                mock.patch(
                    "agent_collab.daemon_supervisor._read_process_identity",
                    return_value=self.PROCESS_IDENTITY,
                ),
            ):
                state = start_daemon(paths, default_workdir=workdir)

            argv = popen.call_args.args[0]
            self.assertIn("--workdir", argv)
            self.assertIn(str(workdir.resolve()), argv)
            self.assertEqual(state["default_workdir"], str(workdir.resolve()))
            self.assertEqual(popen.call_args.kwargs["cwd"], str(paths.home))

    def test_start_failure_does_not_write_live_pid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)

            with mock.patch(
                "agent_collab.daemon_supervisor.subprocess.Popen", return_value=FailedProcess()
            ):
                with self.assertRaises(DaemonSupervisorError):
                    start_daemon(paths, host="127.0.0.1", port=8765)

            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_daemon_dir_permissions_are_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.daemon_dir.mkdir(parents=True)
            paths.daemon_dir.chmod(0o755)
            paths.ensure_dirs()

            self.assertEqual(paths.daemon_dir.stat().st_mode & 0o777, 0o700)

    def _write_config_token(self, paths: GlobalDataPaths, token: str) -> None:
        config_path = paths.home / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f'[daemon]\ntoken = "{token}"\n', encoding="utf-8")
        config_path.chmod(0o600)

    def test_readiness_waits_for_config_token_then_probes_with_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            self._write_config_token(paths, "stale")
            seen = []

            def open_request(request, timeout):
                authorization = request.get_header("Authorization")
                seen.append(authorization)
                if authorization == "Bearer stale":
                    self._write_config_token(paths, "fresh")
                    raise HTTPError(
                        request.full_url,
                        401,
                        "Unauthorized",
                        {},
                        io.BytesIO(b'{"error":"unauthorized"}'),
                    )
                self.assertEqual(authorization, "Bearer fresh")
                return _Response()

            with mock.patch("agent_collab.daemon_supervisor.urlopen", side_effect=open_request):
                _wait_for_ready(ReadyProcess(), "127.0.0.1", 8765, paths, timeout=0.5)

            self.assertEqual(seen, ["Bearer stale", "Bearer fresh"])

    def test_readiness_fails_without_config_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()

            with mock.patch("agent_collab.daemon_supervisor.urlopen") as urlopen:
                with self.assertRaisesRegex(DaemonSupervisorError, "token is not ready"):
                    _wait_for_ready(ReadyProcess(), "127.0.0.1", 8765, paths, timeout=0.3)

            urlopen.assert_not_called()

    def test_start_refuses_live_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
            paths.token_path.write_text("stale", encoding="utf-8")

            with (
                mock.patch("agent_collab.daemon_supervisor.os.kill", return_value=None),
                mock.patch(
                    "agent_collab.daemon_supervisor._daemon_identity_status",
                    return_value="match",
                ),
            ):
                with self.assertRaises(DaemonSupervisorError):
                    start_daemon(paths)

    def test_start_refuses_while_start_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()

            with (
                _daemon_start_lock(paths),
                mock.patch("agent_collab.daemon_supervisor.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(DaemonSupervisorError, "start already in progress"):
                    start_daemon(paths)

            popen.assert_not_called()
            self.assertEqual(paths.daemon_start_lock_path.stat().st_mode & 0o777, 0o600)

    def test_status_cleans_stale_pid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")

            with mock.patch(
                "agent_collab.daemon_supervisor.os.kill", side_effect=ProcessLookupError
            ):
                status = daemon_status(paths)

            self.assertFalse(status.running)
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())
            self.assertFalse(paths.token_path.exists())

    def test_status_cleans_zombie_pid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")

            with mock.patch("agent_collab.daemon_supervisor.os.kill", return_value=None):
                with mock.patch("agent_collab.daemon_supervisor._is_zombie", return_value=True):
                    status = daemon_status(paths)

            self.assertFalse(status.running)
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_stop_sends_sigterm_and_removes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
            signals = []

            def fake_kill(_pid, sig):
                signals.append(sig)
                if sig == 0 and signal.SIGTERM in signals:
                    raise ProcessLookupError()
                return None

            with (
                mock.patch("agent_collab.daemon_supervisor.os.kill", side_effect=fake_kill),
                mock.patch(
                    "agent_collab.daemon_supervisor._daemon_identity_status",
                    return_value="match",
                ),
            ):
                status = stop_daemon(paths, grace_seconds=0.1)

            self.assertFalse(status.running)
            self.assertIn(signal.SIGTERM, signals)
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_stop_tolerates_transient_unknown_identity_during_shutdown(self):
        # Regression for #34: a pid we already attributed and SIGTERM'd can
        # momentarily report IDENTITY_UNKNOWN while it tears down (its procfs
        # cmdline empties before it is reaped). The post-signal wait must keep
        # polling for exit instead of raising, which previously aborted the
        # stop and left the daemon running during autostart handoff.
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "process_identity": self.PROCESS_IDENTITY}),
                encoding="utf-8",
            )
            # Alive for the pre-signal check and the first post-SIGTERM poll,
            # then the process exits.
            running = iter([True, True, False])
            # Attributed before signaling, then transiently unreadable.
            identities = iter(["match", "unknown"])
            signals = []

            def fake_running(_pid):
                return next(running, False)

            def fake_identity(_pid, _state):
                return next(identities, "unknown")

            with (
                mock.patch(
                    "agent_collab.daemon_supervisor.os.kill",
                    side_effect=lambda _pid, sig: signals.append(sig),
                ),
                mock.patch(
                    "agent_collab.daemon_supervisor._is_running",
                    side_effect=fake_running,
                ),
                mock.patch(
                    "agent_collab.daemon_supervisor._daemon_identity_status",
                    side_effect=fake_identity,
                ),
            ):
                status = stop_daemon(paths, grace_seconds=1.0)

            self.assertFalse(status.running)
            self.assertIn(signal.SIGTERM, signals)
            self.assertNotIn(signal.SIGKILL, signals)
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_stop_preserves_state_when_pid_survives_sigkill_with_unknown_identity(self):
        # Regression for #34 review: tolerating a transient IDENTITY_UNKNOWN in
        # the post-SIGKILL wait must not let a pid that stays alive (e.g. stuck
        # in uninterruptible sleep) with an unreadable identity be reported as a
        # clean kill. That would discard state for a live daemon and let a later
        # start spawn a second one on the same port.
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "process_identity": self.PROCESS_IDENTITY}),
                encoding="utf-8",
            )
            signals = []
            # Attributed at the pre-SIGTERM and pre-SIGKILL guards, then its
            # identity becomes and stays unreadable while the pid never dies.
            identities = iter(["match", "match"])

            with (
                mock.patch(
                    "agent_collab.daemon_supervisor.os.kill",
                    side_effect=lambda _pid, sig: signals.append(sig),
                ),
                mock.patch(
                    "agent_collab.daemon_supervisor._is_running",
                    return_value=True,
                ),
                mock.patch(
                    "agent_collab.daemon_supervisor._daemon_identity_status",
                    side_effect=lambda _pid, _state: next(identities, "unknown"),
                ),
            ):
                with self.assertRaisesRegex(DaemonSupervisorError, "failed to stop"):
                    stop_daemon(paths, grace_seconds=0)

            self.assertIn(signal.SIGTERM, signals)
            self.assertIn(signal.SIGKILL, signals)
            # State must be preserved: the daemon is still alive.
            self.assertTrue(paths.state_path.exists())

    def test_stop_refuses_to_signal_systemd_owned_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "manager": "systemd"}), encoding="utf-8"
            )
            with mock.patch("agent_collab.daemon_supervisor.os.kill") as kill:
                with self.assertRaisesRegex(DaemonSupervisorError, "owned by systemd"):
                    stop_daemon(paths)

            self.assertEqual(kill.call_args_list, [mock.call(4242, 0)])

    def test_stop_cleans_stale_systemd_owned_state_without_signaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "manager": "systemd"}), encoding="utf-8"
            )
            with (
                mock.patch("agent_collab.daemon_supervisor._is_running", return_value=False),
                mock.patch("agent_collab.daemon_supervisor.os.kill") as kill,
            ):
                status = stop_daemon(paths)

            self.assertFalse(status.running)
            self.assertFalse(paths.state_path.exists())
            kill.assert_not_called()

    def test_stop_refuses_to_signal_recycled_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "process_identity": self.PROCESS_IDENTITY}),
                encoding="utf-8",
            )
            signals = []

            def fake_kill(_pid, sig):
                signals.append(sig)

            replacement = dict(self.PROCESS_IDENTITY, start_time="999999")
            with (
                mock.patch("agent_collab.daemon_supervisor.os.kill", side_effect=fake_kill),
                mock.patch(
                    "agent_collab.daemon_supervisor._read_process_identity",
                    return_value=replacement,
                ),
            ):
                status = stop_daemon(paths)

            self.assertFalse(status.running)
            self.assertIn("refused to signal", status.message)
            self.assertEqual(signals, [0])
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_stop_preserves_state_when_live_pid_cannot_be_attributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "process_identity": self.PROCESS_IDENTITY}),
                encoding="utf-8",
            )
            signals = []

            def fake_kill(_pid, sig):
                signals.append(sig)

            with (
                mock.patch("agent_collab.daemon_supervisor.os.kill", side_effect=fake_kill),
                mock.patch(
                    "agent_collab.daemon_supervisor._read_process_identity",
                    return_value=None,
                ),
            ):
                with self.assertRaisesRegex(DaemonSupervisorError, "refusing to signal"):
                    stop_daemon(paths)

            self.assertEqual(signals, [0])
            self.assertTrue(paths.pid_path.exists())
            self.assertTrue(paths.state_path.exists())

    def test_stop_rechecks_identity_before_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(
                json.dumps({"pid": 4242, "process_identity": self.PROCESS_IDENTITY}),
                encoding="utf-8",
            )
            signals = []

            def fake_kill(_pid, sig):
                signals.append(sig)

            with (
                mock.patch("agent_collab.daemon_supervisor.os.kill", side_effect=fake_kill),
                mock.patch(
                    "agent_collab.daemon_supervisor._daemon_identity_status",
                    side_effect=["match", "mismatch"],
                ),
            ):
                status = stop_daemon(paths, grace_seconds=0)

            self.assertFalse(status.running)
            self.assertIn("recycled pid", status.message)
            self.assertIn(signal.SIGTERM, signals)
            self.assertNotIn(signal.SIGKILL, signals)

    def test_process_identity_requires_exact_match_and_supports_legacy_argv(self):
        actual = self.PROCESS_IDENTITY
        with mock.patch(
            "agent_collab.daemon_supervisor._read_process_identity",
            return_value=actual,
        ):
            self.assertTrue(_daemon_identity_matches(4242, {"process_identity": dict(actual)}))
            self.assertFalse(
                _daemon_identity_matches(
                    4242,
                    {"process_identity": dict(actual, start_time="different")},
                )
            )
            self.assertTrue(_daemon_identity_matches(4242, {"argv": actual["argv"]}))

        ps_identity = {
            "source": "ps",
            "start_time": "Thu Jul 10 20:00:00 2026",
            "command": "python -m agent_collab.cli serve",
        }
        with mock.patch(
            "agent_collab.daemon_supervisor._read_process_identity",
            return_value=ps_identity,
        ):
            self.assertEqual(
                _daemon_identity_status(4242, {"process_identity": actual}),
                IDENTITY_UNKNOWN,
            )

    def test_tail_daemon_log_reads_last_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.daemon_log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")

            self.assertEqual(tail_daemon_log(paths, tail=2), "two\nthree")

    def test_paths_resolve_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"AGENT_COLLAB_HOME": tmp}):
                paths = GlobalDataPaths.resolve()
            self.assertEqual(paths.home, Path(tmp).resolve())


if __name__ == "__main__":
    unittest.main()
