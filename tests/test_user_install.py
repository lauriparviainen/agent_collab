import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.user_install import UserInstallError, install_user_command, main


class UserInstallTests(unittest.TestCase):
    def test_install_creates_venv_installs_package_and_links_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            venv = root / "venv"
            bin_dir = root / "bin"
            calls = []

            def fake_run(argv):
                calls.append(argv)
                if argv[1:3] == ["-m", "venv"]:
                    (venv / "bin").mkdir(parents=True)
                    (venv / "bin" / "python").touch()
                elif argv[1:4] == ["-m", "pip", "install"]:
                    (venv / "bin" / "agent-collab").touch()

            with mock.patch("agent_collab.user_install._run_checked", side_effect=fake_run):
                link = install_user_command(
                    repo_root=repo,
                    venv=venv,
                    bin_dir=bin_dir,
                    bootstrap_python=root / "python",
                )

            self.assertEqual(calls[0], [str(root / "python"), "-m", "venv", str(venv)])
            self.assertEqual(
                calls[1],
                [str(venv / "bin" / "python"), "-m", "pip", "install", f"{repo}[all]"],
            )
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (venv / "bin" / "agent-collab").resolve())

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

            with mock.patch("agent_collab.user_install._run_checked") as run:
                result = install_user_command(
                    repo_root=repo, venv=venv, bin_dir=bin_dir, editable=True
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

    def test_existing_unmanaged_command_requires_force(self):
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

            with mock.patch("agent_collab.user_install._run_checked"):
                with self.assertRaisesRegex(UserInstallError, "refusing to replace"):
                    install_user_command(repo_root=repo, venv=venv, bin_dir=bin_dir)
                result = install_user_command(
                    repo_root=repo, venv=venv, bin_dir=bin_dir, force=True
                )

            self.assertEqual(result.resolve(), (venv / "bin" / "agent-collab").resolve())

    def test_main_warns_when_bin_directory_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "bin" / "agent-collab"
            with (
                mock.patch("agent_collab.user_install.install_user_command", return_value=link),
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False),
                mock.patch("sys.stdout") as stdout,
                mock.patch("sys.stderr") as stderr,
            ):
                code = main(
                    [
                        "--repo-root",
                        str(root / "repo"),
                        "--venv",
                        str(root / "venv"),
                        "--bin-dir",
                        str(root / "bin"),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertTrue(
                any("installed agent-collab" in str(call) for call in stdout.write.call_args_list)
            )
            self.assertTrue(any("not on PATH" in str(call) for call in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
