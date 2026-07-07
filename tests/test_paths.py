import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.paths import (
    AgentCollabHome,
    GlobalDataPaths,
    default_session_log_dirs,
    legacy_project_session_dirs,
    project_config_path,
    user_config_path,
)


class AgentCollabHomeTests(unittest.TestCase):
    def test_default_home_is_under_user_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_COLLAB_HOME", None)
            home = AgentCollabHome.resolve()
        self.assertEqual(home.root, Path.home().resolve() / ".agent-collab")
        self.assertEqual(home.config_path, home.root / "config.toml")

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": tmp}):
                home = AgentCollabHome.resolve()
            self.assertEqual(home.root, Path(tmp).resolve())

    def test_explicit_env_mapping(self):
        home = AgentCollabHome.resolve(env={"AGENT_COLLAB_HOME": "/tmp/ac-explicit"})
        self.assertEqual(home.root, Path("/tmp/ac-explicit").resolve())


class GlobalDataPathsTests(unittest.TestCase):
    def test_derived_paths(self):
        paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": "/tmp/ac-home"})
        root = Path("/tmp/ac-home").resolve()
        self.assertEqual(paths.home, root)
        self.assertEqual(paths.data_dir, root / "data")
        self.assertEqual(paths.daemon_dir, root / "data" / "daemon")
        self.assertEqual(paths.session_dir, root / "data" / "sessions")
        self.assertEqual(paths.tmp_dir, root / "data" / "tmp")
        self.assertEqual(paths.session_index_path, root / "data" / "session-index.json")
        self.assertEqual(paths.pid_path, root / "data" / "daemon" / "pid")
        self.assertEqual(paths.state_path, root / "data" / "daemon" / "state.json")
        self.assertEqual(paths.daemon_log_path, root / "data" / "daemon" / "daemon.log")
        self.assertEqual(paths.daemon_stderr_path, root / "data" / "daemon" / "daemon.stderr.log")

    def test_ensure_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": tmp})
            paths.ensure_dirs()
            self.assertTrue(paths.daemon_dir.is_dir())
            self.assertTrue(paths.session_dir.is_dir())
            self.assertTrue(paths.tmp_dir.is_dir())


class ConfigPathTests(unittest.TestCase):
    def test_project_config_path(self):
        self.assertEqual(
            project_config_path(Path("/tmp/proj")),
            Path("/tmp/proj").resolve() / ".agent-collab" / "config.toml",
        )

    def test_user_config_path(self):
        home = AgentCollabHome.resolve(env={"AGENT_COLLAB_HOME": "/tmp/ac-home"})
        self.assertEqual(user_config_path(home), Path("/tmp/ac-home").resolve() / "config.toml")


class SessionLogDirTests(unittest.TestCase):
    def test_legacy_project_session_dirs(self):
        root = Path("/tmp/proj").resolve()
        self.assertEqual(
            legacy_project_session_dirs(root),
            [root / ".agent-collab" / "data" / "sessions", root / ".agent-collab" / "sessions"],
        )

    def test_default_session_log_dirs_global_first(self):
        root = Path("/tmp/proj").resolve()
        dirs = default_session_log_dirs(root, env={"AGENT_COLLAB_HOME": "/tmp/ac-home"})
        self.assertEqual(dirs[0], Path("/tmp/ac-home").resolve() / "data" / "sessions")
        self.assertEqual(dirs[1:], legacy_project_session_dirs(root))


if __name__ == "__main__":
    unittest.main()
