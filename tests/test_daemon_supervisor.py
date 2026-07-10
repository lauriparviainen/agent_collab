import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from agent_collab.daemon_supervisor import (
    DaemonSupervisorError,
    _wait_for_ready,
    daemon_status,
    start_daemon,
    stop_daemon,
    tail_daemon_log,
)
from agent_collab.paths import GlobalDataPaths
from agent_collab.server_http import mint_auth_token


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
    def _paths(self, tmp: str) -> GlobalDataPaths:
        return GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": tmp})

    def test_start_writes_pid_state_and_log_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)

            with mock.patch("agent_collab.daemon_supervisor.subprocess.Popen", return_value=FakeProcess()) as popen:
                state = start_daemon(paths, host="127.0.0.1", port=8765)

            self.assertEqual(state["pid"], 4242)
            self.assertEqual(state["home"], str(paths.home))
            self.assertIsNone(state["default_workdir"])
            self.assertEqual(state["data_dir"], str(paths.data_dir))
            self.assertEqual(state["session_dir"], str(paths.session_dir))
            self.assertEqual(state["token_path"], str(paths.token_path))
            self.assertEqual(paths.pid_path.read_text(encoding="utf-8").strip(), "4242")
            self.assertEqual(json.loads(paths.state_path.read_text(encoding="utf-8"))["pid"], 4242)
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertIn("serve", argv)
            self.assertIn("--session-log-dir", argv)
            self.assertIn("--token-path", argv)
            self.assertIn(str(paths.token_path), argv)
            self.assertNotIn("--workdir", argv)
            self.assertEqual(popen.call_args.kwargs["cwd"], str(paths.home))
            self.assertIn("agent_collab", env["PYTHONPATH"])
            self.assertTrue(paths.daemon_log_path.exists())
            self.assertTrue(paths.daemon_stderr_path.exists())
            self.assertEqual(paths.pid_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.daemon_dir.stat().st_mode & 0o777, 0o700)

    def test_start_passes_default_workdir_to_serve_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            workdir = Path(tmp) / "project"
            workdir.mkdir()

            with mock.patch("agent_collab.daemon_supervisor.subprocess.Popen", return_value=FakeProcess()) as popen:
                state = start_daemon(paths, default_workdir=workdir)

            argv = popen.call_args.args[0]
            self.assertIn("--workdir", argv)
            self.assertIn(str(workdir.resolve()), argv)
            self.assertEqual(state["default_workdir"], str(workdir.resolve()))
            self.assertEqual(popen.call_args.kwargs["cwd"], str(paths.home))

    def test_start_failure_does_not_write_live_pid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)

            with mock.patch("agent_collab.daemon_supervisor.subprocess.Popen", return_value=FailedProcess()):
                with self.assertRaises(DaemonSupervisorError):
                    start_daemon(paths, host="127.0.0.1", port=8765)

            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

    def test_token_is_private_and_daemon_dir_permissions_are_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.daemon_dir.mkdir(parents=True)
            paths.daemon_dir.chmod(0o755)
            paths.ensure_dirs()
            first = mint_auth_token(paths.token_path)
            second = mint_auth_token(paths.token_path)

            self.assertNotEqual(first, second)
            self.assertEqual(paths.token_path.read_text(encoding="utf-8").strip(), second)
            self.assertEqual(paths.token_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.daemon_dir.stat().st_mode & 0o777, 0o700)

    def test_readiness_rejects_stale_token_then_accepts_fresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.token_path.write_text("stale\n", encoding="utf-8")
            seen = []

            def open_request(request, timeout):
                authorization = request.get_header("Authorization")
                seen.append(authorization)
                if authorization == "Bearer stale":
                    paths.token_path.write_text("fresh\n", encoding="utf-8")
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

    def test_start_refuses_live_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
            paths.token_path.write_text("stale", encoding="utf-8")

            with mock.patch("agent_collab.daemon_supervisor.os.kill", return_value=None):
                with self.assertRaises(DaemonSupervisorError):
                    start_daemon(paths)

    def test_status_cleans_stale_pid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")

            with mock.patch("agent_collab.daemon_supervisor.os.kill", side_effect=ProcessLookupError):
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

            with mock.patch("agent_collab.daemon_supervisor.os.kill", side_effect=fake_kill):
                status = stop_daemon(paths, grace_seconds=0.1)

            self.assertFalse(status.running)
            self.assertIn(signal.SIGTERM, signals)
            self.assertFalse(paths.pid_path.exists())
            self.assertFalse(paths.state_path.exists())

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
