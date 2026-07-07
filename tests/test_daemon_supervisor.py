import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.daemon_supervisor import DaemonSupervisorError, daemon_status, start_daemon, stop_daemon, tail_daemon_log
from agent_collab.paths import GlobalDataPaths


class FakeProcess:
    pid = 4242


class FailedProcess:
    pid = 4243

    def poll(self):
        return 2

    def terminate(self):
        raise AssertionError("terminated process should not be terminated again")


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
            self.assertEqual(paths.pid_path.read_text(encoding="utf-8").strip(), "4242")
            self.assertEqual(json.loads(paths.state_path.read_text(encoding="utf-8"))["pid"], 4242)
            argv = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertIn("serve", argv)
            self.assertIn("--session-log-dir", argv)
            self.assertNotIn("--workdir", argv)
            self.assertEqual(popen.call_args.kwargs["cwd"], str(paths.home))
            self.assertIn("agent_collab", env["PYTHONPATH"])
            self.assertTrue(paths.daemon_log_path.exists())
            self.assertTrue(paths.daemon_stderr_path.exists())

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

    def test_start_refuses_live_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            paths.ensure_dirs()
            paths.pid_path.write_text("4242\n", encoding="utf-8")
            paths.state_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")

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
