import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.user_install import (
    UserInstallError,
    install_user_command,
    main,
    uninstall_user_command,
)


class UserInstallTests(unittest.TestCase):
    def test_install_creates_venv_installs_package_and_links_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            venv = root / "venv"
            bin_dir = root / "bin"
            calls = []

            def fake_checked(argv):
                calls.append(argv)
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").touch()

            def fake_logged(argv, log_path, *, action):
                calls.append(argv)
                (venv / "bin" / "agent-collab").touch()

            with (
                mock.patch("agent_collab.user_install._run_checked", side_effect=fake_checked),
                mock.patch("agent_collab.user_install._run_logged", side_effect=fake_logged),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                link = install_user_command(
                    repo_root=repo,
                    venv=venv,
                    bin_dir=bin_dir,
                    bootstrap_python=root / "python",
                    log_path=root / "install.log",
                )

            self.assertEqual(calls[0], [str(root / "python"), "-m", "venv", str(venv)])
            self.assertEqual(
                calls[1],
                [str(venv / "bin" / "python"), "-m", "pip", "install", f"{repo}[all]"],
            )
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (venv / "bin" / "agent-collab").resolve())
            output = stdout.getvalue()
            self.assertIn("▶ Preparing durable environment", output)
            self.assertIn("▶ Installing agent-collab", output)
            self.assertIn("✓ Command available:", output)

    def test_editable_reinstall_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").touch()
            (venv / "bin" / "agent-collab").touch()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            link = bin_dir / "agent-collab"
            link.symlink_to(venv / "bin" / "agent-collab")

            with (
                mock.patch("agent_collab.user_install._run_logged") as run,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                result = install_user_command(
                    repo_root=repo,
                    venv=venv,
                    bin_dir=bin_dir,
                    editable=True,
                    log_path=root / "install.log",
                )

            self.assertEqual(result, link)
            self.assertEqual(
                run.call_args.args[0],
                [
                    str(venv / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--editable",
                    f"{repo}[all]",
                ],
            )

    def test_existing_unmanaged_command_fails_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").touch()
            (venv / "bin" / "agent-collab").touch()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            command = bin_dir / "agent-collab"
            command.write_text("unrelated\n", encoding="utf-8")

            with (
                mock.patch("agent_collab.user_install._run_logged"),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                with self.assertRaisesRegex(UserInstallError, "remove it and re-run"):
                    install_user_command(
                        repo_root=repo,
                        venv=venv,
                        bin_dir=bin_dir,
                        log_path=root / "install.log",
                    )

            self.assertEqual(command.read_text(encoding="utf-8"), "unrelated\n")

    def test_foreign_command_fails_before_any_install_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "agent-collab").write_text("unrelated\n", encoding="utf-8")

            with (
                mock.patch("agent_collab.user_install._run_checked") as checked,
                mock.patch("agent_collab.user_install._run_logged") as logged,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                with self.assertRaisesRegex(UserInstallError, "remove it and re-run"):
                    install_user_command(
                        repo_root=repo,
                        venv=root / "venv",
                        bin_dir=bin_dir,
                        log_path=root / "install.log",
                    )

            checked.assert_not_called()
            logged.assert_not_called()

    def test_failed_pip_install_reports_log_path_and_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text(
                "#!/usr/bin/env bash\necho 'boom from pip'\nexit 3\n", encoding="utf-8"
            )
            (venv / "bin" / "python").chmod(0o755)
            log_path = root / "install.log"

            with (
                mock.patch("sys.stdout", new_callable=io.StringIO),
                self.assertRaisesRegex(UserInstallError, "full log:") as caught,
            ):
                install_user_command(
                    repo_root=repo,
                    venv=venv,
                    bin_dir=root / "bin",
                    log_path=log_path,
                )

            self.assertIn("boom from pip", str(caught.exception))
            self.assertIn("boom from pip", log_path.read_text(encoding="utf-8"))

    def test_main_install_warns_when_bin_directory_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "bin" / "agent-collab"
            environ = {
                "PATH": "/usr/bin",
                "AGENT_COLLAB_BIN_DIR": str(root / "bin"),
                "AGENT_COLLAB_HOME": str(root / "home"),
            }
            with (
                mock.patch(
                    "agent_collab.user_install.install_user_command", return_value=link
                ) as install,
                mock.patch(
                    "agent_collab.user_install._probe_daemon",
                    return_value={
                        "running": False,
                        "systemd": False,
                        "sessions": None,
                        "state": {},
                    },
                ),
                mock.patch.dict(os.environ, environ, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                code = main(
                    ["install", "--repo-root", str(root / "repo"), "--venv", str(root / "venv")]
                )

            self.assertEqual(code, 0)
            self.assertEqual(install.call_args.kwargs["bin_dir"], root / "bin")
            output = stdout.getvalue()
            self.assertIn("✓ Install complete", output)
            self.assertIn("ⓘ Info: Daemon not running", output)

    def test_main_install_restarts_previously_running_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = {
                "running": True,
                "systemd": False,
                "sessions": 2,
                "state": {"host": "127.0.0.1", "port": 8765, "default_workdir": None},
            }
            with (
                mock.patch(
                    "agent_collab.user_install.install_user_command",
                    return_value=root / "bin" / "agent-collab",
                ),
                mock.patch("agent_collab.user_install._probe_daemon", return_value=probe),
                mock.patch("agent_collab.user_install._migrate_user_config"),
                mock.patch("agent_collab.daemon_supervisor.stop_daemon") as stop,
                mock.patch("agent_collab.daemon_supervisor.start_daemon") as start,
                mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root)}, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                code = main(
                    ["install", "--repo-root", str(root / "repo"), "--venv", str(root / "venv")]
                )

            self.assertEqual(code, 0)
            stop.assert_called_once()
            start.assert_called_once_with(
                host="127.0.0.1",
                port=8765,
                default_workdir=None,
                interpreter=(root / "venv" / "bin" / "python"),
            )
            output = stdout.getvalue()
            self.assertIn("interrupting 2 active sessions", output)
            self.assertIn("✓ Daemon restarted", output)

    def test_main_reports_fatal_errors_with_error_prefix(self):
        with (
            mock.patch(
                "agent_collab.user_install._probe_daemon",
                return_value={"running": False, "systemd": False, "sessions": None, "state": {}},
            ),
            mock.patch(
                "agent_collab.user_install.install_user_command",
                side_effect=UserInstallError("pip exploded"),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = main(["install", "--repo-root", "/tmp/repo", "--venv", "/tmp/venv"])

        self.assertEqual(code, 1)
        self.assertIn("Error: pip exploded", stderr.getvalue())


class UserUninstallTests(unittest.TestCase):
    def test_uninstall_removes_venv_and_owned_link_keeps_home_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            entrypoint = venv / "bin" / "agent-collab"
            entrypoint.touch()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            link = bin_dir / "agent-collab"
            link.symlink_to(entrypoint)
            home = root / "home"
            (home / "data").mkdir(parents=True)
            (home / "config.toml").write_text("schema_version = 6\n", encoding="utf-8")

            with (
                mock.patch("agent_collab.user_install._teardown_daemon") as teardown,
                mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                uninstall_user_command(venv=venv, bin_dir=bin_dir)

            teardown.assert_called_once()
            self.assertFalse(venv.exists())
            self.assertFalse(os.path.lexists(link))
            self.assertTrue((home / "config.toml").exists())
            self.assertTrue((home / "data").exists())
            output = stdout.getvalue()
            self.assertIn("✓ Environment removed", output)
            self.assertIn("Config and session data kept", output)
            self.assertIn("✓ Uninstall complete", output)

    def test_uninstall_leaves_foreign_command_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / "venv"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            command = bin_dir / "agent-collab"
            command.write_text("unrelated\n", encoding="utf-8")

            with (
                mock.patch("agent_collab.user_install._teardown_daemon"),
                mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                uninstall_user_command(venv=venv, bin_dir=bin_dir)

            self.assertTrue(command.exists())
            output = stdout.getvalue()
            self.assertIn("! Warning: left", output)
            self.assertIn("ⓘ Info: Environment not present", output)

    def test_uninstall_aborts_before_removing_venv_when_teardown_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)

            with (
                mock.patch(
                    "agent_collab.user_install._teardown_daemon",
                    side_effect=UserInstallError("systemd said no"),
                ),
                mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                with self.assertRaisesRegex(UserInstallError, "systemd said no"):
                    uninstall_user_command(venv=venv, bin_dir=root / "bin")

            self.assertTrue(venv.exists())

    def test_uninstall_is_safe_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch("agent_collab.user_install._teardown_daemon"),
                mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}, clear=False),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                uninstall_user_command(venv=root / "venv", bin_dir=root / "bin")

            self.assertIn("✓ Uninstall complete", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
