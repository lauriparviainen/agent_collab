import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest

from agent_collab.daemon_lifecycle import (
    LifecycleBusyError,
    lifecycle_transaction,
    registration_lock_path,
    registration_transaction,
)
from agent_collab.paths import GlobalDataPaths


class DaemonLifecycleLockTests(unittest.TestCase):
    def _paths(self, root: Path) -> GlobalDataPaths:
        return GlobalDataPaths.resolve(env={"AGENT_COLLAB_HOME": str(root / "home")})

    def test_busy_lifecycle_lock_fails_fast_and_names_holder_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            entered = threading.Event()
            release = threading.Event()

            def holder():
                with lifecycle_transaction("install", paths):
                    entered.set()
                    release.wait(2)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaisesRegex(LifecycleBusyError, "install is in progress"):
                    with lifecycle_transaction("daemon start", paths):
                        self.fail("busy lock unexpectedly acquired")
            finally:
                release.set()
                thread.join(2)

            with lifecycle_transaction("daemon start", paths):
                self.assertTrue(paths.lifecycle_lock_path.exists())

    def test_registration_lock_requires_lifecycle_first_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            lock = root / "account" / "app" / "launchd-registration.lock"
            with self.assertRaisesRegex(RuntimeError, "lifecycle lock first"):
                with registration_transaction("launchd", "enable", lock_path=lock):
                    pass
            with lifecycle_transaction("enable", paths):
                with registration_transaction("launchd", "enable", lock_path=lock):
                    self.assertTrue(lock.exists())
                    self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(lock.parent.stat().st_mode), 0o700)
            self.assertTrue(lock.exists())
            self.assertFalse(lock.with_name(lock.name + ".owner.json").exists())

    def test_registration_lock_rejects_symlink_and_permissive_directory(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with lifecycle_transaction("enable", paths):
                with self.assertRaisesRegex(LifecycleBusyError, "not a real directory"):
                    with registration_transaction(
                        "launchd", "enable", lock_path=linked / "registration.lock"
                    ):
                        pass

            app = root / "permissive"
            app.mkdir(mode=0o755)
            with lifecycle_transaction("enable", paths):
                with self.assertRaisesRegex(LifecycleBusyError, "owner-only"):
                    with registration_transaction(
                        "launchd", "enable", lock_path=app / "registration.lock"
                    ):
                        pass

    def test_registration_lock_rejects_permissive_file_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            app = root / "account" / "app"
            app.mkdir(parents=True, mode=0o700)
            lock = app / "launchd-registration.lock"
            lock.write_text("", encoding="utf-8")
            lock.chmod(0o666)

            with lifecycle_transaction("enable", paths):
                with self.assertRaisesRegex(LifecycleBusyError, r"owner-only \(0600\)"):
                    with registration_transaction("launchd", "enable", lock_path=lock):
                        pass

            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o666)

    def test_registration_paths_use_account_home_not_environment(self):
        account = Path("/Users/example")
        self.assertEqual(
            registration_lock_path("launchd", home=account),
            account
            / "Library"
            / "Application Support"
            / "io.github.lauriparviainen.agent-collab"
            / "launchd-registration.lock",
        )
        self.assertEqual(
            registration_lock_path("systemd", home=account),
            account
            / ".local"
            / "state"
            / "io.github.lauriparviainen.agent-collab"
            / "systemd-registration.lock",
        )


if __name__ == "__main__":
    unittest.main()
