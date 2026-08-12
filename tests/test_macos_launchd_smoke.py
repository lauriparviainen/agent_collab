import io
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import macos_launchd_smoke as smoke


class MacOSLaunchdSmokeTests(unittest.TestCase):
    def _completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_unavailable_gui_domain_is_a_skip_eligible_preflight(self):
        with (
            mock.patch.object(smoke, "_registration_paths", return_value=()),
            mock.patch.object(
                smoke,
                "_run",
                side_effect=[
                    self._completed(1, stderr="not found"),
                    self._completed(1, stderr="domain unavailable"),
                ],
            ),
        ):
            clean, detail = smoke._preflight({})
        self.assertFalse(clean)
        self.assertIn("domain is unavailable", detail)

    def test_malformed_or_duplicate_disabled_output_is_a_hard_failure(self):
        outputs = (
            "unexpected output\n",
            f'disabled services = {{\n    "{smoke.LABEL}" malformed\n}}\n',
            (
                "disabled services = {\n"
                f'    "{smoke.LABEL}" => false\n'
                f'    "{smoke.LABEL}" => true\n'
                "}\n"
            ),
        )
        for output in outputs:
            with self.subTest(output=output):
                with (
                    mock.patch.object(smoke, "_registration_paths", return_value=()),
                    mock.patch.object(
                        smoke,
                        "_run",
                        side_effect=[self._completed(1), self._completed(stdout=output)],
                    ),
                ):
                    with self.assertRaises(smoke.PreflightContractError):
                        smoke._preflight({})

    def test_malformed_unrelated_disabled_entry_is_ignored(self):
        output = "disabled services = {\n    malformed unrelated entry\n}\n"
        with (
            mock.patch.object(smoke, "_registration_paths", return_value=()),
            mock.patch.object(
                smoke,
                "_run",
                side_effect=[self._completed(1), self._completed(stdout=output)],
            ),
        ):
            clean, detail = smoke._preflight({})
        self.assertTrue(clean)
        self.assertEqual(detail, "")

    def test_named_disabled_and_enabled_states_are_accepted(self):
        for state, expected_clean in (("disabled", False), ("enabled", True)):
            output = f'disabled services = {{\n    "{smoke.LABEL}" => {state}\n}}\n'
            with (
                self.subTest(state=state),
                mock.patch.object(smoke, "_registration_paths", return_value=()),
                mock.patch.object(
                    smoke,
                    "_run",
                    side_effect=[self._completed(1), self._completed(stdout=output)],
                ),
            ):
                clean, detail = smoke._preflight({})

            self.assertEqual(clean, expected_clean)
            if expected_clean:
                self.assertEqual(detail, "")
            else:
                self.assertIn("persisted disabled override", detail)

    def test_existing_production_registration_remains_a_safe_skip(self):
        collision = Path("/nonexistent/agent-collab.plist")
        disabled = "disabled services = {\n}\n"
        with (
            mock.patch.object(smoke, "_registration_paths", return_value=(collision,)),
            mock.patch("os.path.lexists", return_value=True),
            mock.patch.object(
                smoke,
                "_run",
                side_effect=[self._completed(1), self._completed(stdout=disabled)],
            ),
        ):
            clean, detail = smoke._preflight({})
        self.assertFalse(clean)
        self.assertIn("production registration is not empty", detail)

    def test_main_returns_nonzero_for_available_but_unparseable_contract(self):
        with (
            mock.patch.object(smoke.sys, "platform", "darwin"),
            mock.patch.object(
                smoke,
                "_preflight",
                side_effect=smoke.PreflightContractError("malformed native output"),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = smoke.main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL: malformed native output", stderr.getvalue())

    def test_override_reset_refuses_a_loaded_dormant_target(self):
        with (
            mock.patch.object(smoke, "_registration_paths", return_value=()),
            mock.patch.object(smoke, "_target_loaded", return_value=True),
            mock.patch.object(smoke, "_run") as run,
            mock.patch.object(smoke, "lifecycle_transaction"),
            mock.patch.object(smoke, "registration_transaction"),
            mock.patch.object(smoke.GlobalDataPaths, "resolve"),
        ):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                smoke._reset_disabled_override({})
        run.assert_not_called()

    def test_target_loaded_accepts_only_recognized_absence(self):
        with mock.patch.object(
            smoke, "_run", return_value=self._completed(1, stderr="Could not find service")
        ):
            self.assertFalse(smoke._target_loaded({}))
        with mock.patch.object(
            smoke, "_run", return_value=self._completed(1, stderr="Operation not permitted")
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot prove"):
                smoke._target_loaded({})


if __name__ == "__main__":
    unittest.main()
