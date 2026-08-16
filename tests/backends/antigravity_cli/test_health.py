import unittest

from agent_collab.backends.antigravity_cli.backend import (
    REQUIRED_AGY_VERSION,
    AntigravityCliBackend,
)
from agent_collab.backends.base import HEALTH_OK, HEALTH_UNAVAILABLE
from agent_collab.backends.common.health import parse_cli_version, probe_cli_backend
from agent_collab.config import AgentConfig


class AntigravityCliVersionFloorTests(unittest.TestCase):
    def test_required_floor_is_1_1_8(self):
        self.assertEqual(REQUIRED_AGY_VERSION, "1.1.8")
        self.assertEqual(parse_cli_version("1.1.8"), (1, 1, 8))
        self.assertEqual(parse_cli_version("1.1.13"), (1, 1, 13))
        self.assertEqual(parse_cli_version("1.1.7"), (1, 1, 7))
        self.assertEqual(parse_cli_version("agy 1.1.13"), (1, 1, 13))
        self.assertEqual(parse_cli_version("v1.1.8"), (1, 1, 8))
        self.assertIsNone(parse_cli_version("not-a-version"))
        self.assertIsNone(parse_cli_version("exit status 2"))
        self.assertIsNone(parse_cli_version("go1.22.5"))
        self.assertIsNone(parse_cli_version("dev"))
        self.assertIsNone(parse_cli_version(None))

    def test_probe_accepts_required_and_newer(self):
        for version in ("1.1.8", "1.1.13"):
            with self.subTest(version=version):
                health = probe_cli_backend(
                    "agy",
                    which=lambda _binary: "/usr/bin/agy",
                    run_version=lambda _binary, _path: version,
                    credentials=None,
                    min_version=REQUIRED_AGY_VERSION,
                )
                self.assertEqual(health.status, HEALTH_OK)
                self.assertEqual(health.version, version)
                self.assertEqual(health.checks["cli_version"]["status"], "compatible")
                self.assertEqual(health.checks["cli_version"]["required"], "agy >= 1.1.8")
                self.assertEqual(health.checks["cli_version"]["observed"], version)

    def test_probe_rejects_older_and_unparseable(self):
        cases = (
            ("1.1.7", "1.1.7"),
            ("1.0.0", "1.0.0"),
            ("exit status 2", "exit status 2"),
            ("go1.22.5", "go1.22.5"),
            ("dev", "dev"),
            (None, "missing"),
        )
        for version, observed in cases:
            with self.subTest(version=version):
                health = probe_cli_backend(
                    "agy",
                    which=lambda _binary: "/usr/bin/agy",
                    run_version=lambda _binary, _path: version,
                    credentials=None,
                    min_version=REQUIRED_AGY_VERSION,
                )
                self.assertEqual(health.status, HEALTH_UNAVAILABLE)
                self.assertEqual(health.reason_codes, ("cli_version_incompatible",))
                self.assertIn("1.1.8", health.reason)
                self.assertIn(observed, health.reason)
                self.assertEqual(health.checks["cli_version"]["required"], "agy >= 1.1.8")
                self.assertEqual(health.checks["cli_version"]["observed"], observed)
                self.assertEqual(health.remediation[0]["code"], "upgrade_cli")

    def test_backend_probe_uses_the_version_floor(self):
        backend = AntigravityCliBackend()
        calls = {}

        def fake_probe(binary, **kwargs):
            calls["binary"] = binary
            calls["min_version"] = kwargs.get("min_version")
            return probe_cli_backend(
                binary,
                which=lambda _name: f"/usr/bin/{_name}",
                run_version=lambda _binary, _path: "1.1.7",
                min_version=kwargs.get("min_version"),
            )

        from unittest import mock

        with mock.patch(
            "agent_collab.backends.antigravity_cli.backend.probe_cli_backend",
            side_effect=fake_probe,
        ):
            health = backend.probe_for_agent(
                AgentConfig(id="ag", type="antigravity", command="agy-wrapper")
            )
        self.assertEqual(calls["binary"], "agy-wrapper")
        self.assertEqual(calls["min_version"], REQUIRED_AGY_VERSION)
        self.assertEqual(health.status, HEALTH_UNAVAILABLE)
        self.assertEqual(health.reason_codes, ("cli_version_incompatible",))
        self.assertIn("agy-wrapper", health.reason)
        self.assertIn("1.1.8", health.reason)
        self.assertIn("1.1.7", health.reason)
        self.assertEqual(backend.clean_eof_fallback, False)
        self.assertEqual(backend.event_fidelity, "typed")
        self.assertEqual(backend.provider_session_id_kind, "conversation")
        self.assertEqual(
            backend.capabilities.to_dict(),
            {"resume": False, "interrupt": False, "tool_gate": False, "continuity": False},
        )
