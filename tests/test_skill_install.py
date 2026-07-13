import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_collab.skill_install import (
    SKILL_NAMES,
    SkillInstallError,
    install_skills,
    main,
    uninstall_skills,
)


def _make_sources(repo: Path, version: str = "one") -> None:
    for name in SKILL_NAMES:
        skill = repo / "skills" / name
        (skill / "agents").mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n{version}\n", encoding="utf-8")
        (skill / "agents" / "openai.yaml").write_text(version + "\n", encoding="utf-8")


class SkillInstallTests(unittest.TestCase):
    def test_install_upgrade_and_uninstall_codex_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            state = root / "state.json"
            _make_sources(repo)

            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                install_skills(
                    repo_root=repo,
                    clients=["codex"],
                    user_home=home,
                    state_path=state,
                )

            for name in SKILL_NAMES:
                self.assertEqual(
                    (home / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8"),
                    f"---\nname: {name}\n---\none\n",
                )
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertIn("✓ Review skill installation complete", stdout.getvalue())

            _make_sources(repo, version="two")
            install_skills(
                repo_root=repo,
                clients=["codex"],
                user_home=home,
                state_path=state,
            )
            for name in SKILL_NAMES:
                self.assertIn(
                    "two",
                    (home / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8"),
                )

            uninstall_skills(clients=["codex"], user_home=home, state_path=state)
            for name in SKILL_NAMES:
                self.assertFalse(home.joinpath(".agents", "skills", name).exists())
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["installs"], {})

    def test_install_all_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            _make_sources(repo)

            install_skills(
                repo_root=repo,
                clients=[],
                user_home=home,
                state_path=root / "state.json",
            )

            expected_roots = (
                home / ".claude" / "skills",
                home / ".agents" / "skills",
                home / ".gemini" / "config" / "skills",
                home / ".grok" / "skills",
            )
            for skill_root in expected_roots:
                for name in SKILL_NAMES:
                    self.assertTrue((skill_root / name / "SKILL.md").is_file())

    def test_identical_manual_copy_is_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            _make_sources(repo)
            destination = home / ".claude" / "skills"
            destination.mkdir(parents=True)
            for name in SKILL_NAMES:
                shutil.copytree(repo / "skills" / name, destination / name)

            install_skills(
                repo_root=repo,
                clients=["claude"],
                user_home=home,
                state_path=root / "state.json",
            )

            self.assertEqual(
                len(json.loads((root / "state.json").read_text(encoding="utf-8"))["installs"]),
                2,
            )

    def test_conflict_prevents_partial_all_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            _make_sources(repo)
            conflict = home / ".agents" / "skills" / SKILL_NAMES[0]
            conflict.mkdir(parents=True)
            (conflict / "SKILL.md").write_text("local\n", encoding="utf-8")

            with self.assertRaisesRegex(SkillInstallError, "unmanaged or different"):
                install_skills(
                    repo_root=repo,
                    clients=[],
                    user_home=home,
                    state_path=root / "state.json",
                )

            self.assertFalse((home / ".claude" / "skills").exists())
            self.assertEqual((conflict / "SKILL.md").read_text(encoding="utf-8"), "local\n")

    def test_modified_managed_skill_blocks_upgrade_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            state = root / "state.json"
            _make_sources(repo)
            install_skills(
                repo_root=repo,
                clients=["grok"],
                user_home=home,
                state_path=state,
            )
            changed = home / ".grok" / "skills" / SKILL_NAMES[0] / "SKILL.md"
            changed.write_text("local edit\n", encoding="utf-8")

            with self.assertRaisesRegex(SkillInstallError, "local changes"):
                install_skills(
                    repo_root=repo,
                    clients=["grok"],
                    user_home=home,
                    state_path=state,
                )
            with self.assertRaisesRegex(SkillInstallError, "local changes"):
                uninstall_skills(clients=["grok"], user_home=home, state_path=state)

            for name in SKILL_NAMES:
                self.assertTrue((home / ".grok" / "skills" / name).exists())

    def test_completed_upgrade_remains_managed_when_later_client_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            state = root / "state.json"
            _make_sources(repo)
            install_skills(
                repo_root=repo,
                clients=["claude", "codex"],
                user_home=home,
                state_path=state,
            )
            _make_sources(repo, version="two")

            from agent_collab import skill_install

            replace = skill_install._replace_directory
            calls = 0

            def fail_on_codex(source, destination):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise SkillInstallError("simulated Codex permission error")
                replace(source, destination)

            with mock.patch(
                "agent_collab.skill_install._replace_directory",
                side_effect=fail_on_codex,
            ):
                with self.assertRaisesRegex(SkillInstallError, "simulated Codex"):
                    install_skills(
                        repo_root=repo,
                        clients=["claude", "codex"],
                        user_home=home,
                        state_path=state,
                    )

            # Both completed Claude replacements were persisted before the
            # Codex failure, so the next managed operation accepts them.
            install_skills(
                repo_root=repo,
                clients=["claude"],
                user_home=home,
                state_path=state,
            )
            for name in SKILL_NAMES:
                self.assertIn(
                    "two",
                    (home / ".claude" / "skills" / name / "SKILL.md").read_text(encoding="utf-8"),
                )

    def test_source_matching_destination_recovers_stale_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "user"
            state = root / "state.json"
            _make_sources(repo)
            install_skills(
                repo_root=repo,
                clients=["claude"],
                user_home=home,
                state_path=state,
            )
            _make_sources(repo, version="two")

            # Simulate a process exit after replacing one destination but
            # before its new fingerprint reached the state file.
            destination = home / ".claude" / "skills" / SKILL_NAMES[0]
            shutil.rmtree(destination)
            shutil.copytree(repo / "skills" / SKILL_NAMES[0], destination)

            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                install_skills(
                    repo_root=repo,
                    clients=["claude"],
                    user_home=home,
                    state_path=state,
                )

            self.assertIn("✓ Recovered managed state:", stdout.getvalue())
            install_skills(
                repo_root=repo,
                clients=["claude"],
                user_home=home,
                state_path=state,
            )

    def test_uninstall_leaves_unmanaged_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user"
            skill = home / ".agents" / "skills" / SKILL_NAMES[0]
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("manual\n", encoding="utf-8")

            uninstall_skills(clients=["codex"], user_home=home, state_path=root / "state.json")

            self.assertTrue(skill.exists())

    def test_main_reports_errors_with_cli_prefix(self):
        with (
            mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": "/tmp/test-skill-home"}),
            mock.patch(
                "agent_collab.skill_install.install_skills",
                side_effect=SkillInstallError("nope"),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = main(["install", "codex", "--repo-root", "/tmp/repo"])

        self.assertEqual(code, 1)
        self.assertIn("Error: nope", stderr.getvalue())

    def test_main_install_without_client_selects_all(self):
        with mock.patch("agent_collab.skill_install.install_skills") as install:
            code = main(["install", "--repo-root", "/tmp/repo"])

        self.assertEqual(code, 0)
        self.assertEqual(install.call_args.kwargs["clients"], [])

    def test_main_rejects_unknown_client_as_cli_error(self):
        with (
            mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": "/tmp/test-skill-home"}),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = main(["install", "cursor", "--repo-root", "/tmp/repo"])

        self.assertEqual(code, 1)
        self.assertIn("unknown skill client: cursor", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
