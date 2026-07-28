import os
import unittest
from unittest import mock

from integration_tests import harness
from integration_tests.harness import LiveBackendTestCase


class IntegrationHarnessOptionTests(unittest.TestCase):
    def tearDown(self):
        harness.configure()

    def _options(self, provider):
        case = LiveBackendTestCase(methodName="runTest")
        case.provider = provider
        return case.requested_options()

    def test_live_tests_default_to_fast_economical_models(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self._options("claude"), {"model": "sonnet", "thinking_level": "low"})
            self.assertEqual(
                self._options("codex"),
                {"model": "gpt-5.6-luna", "thinking_level": "low"},
            )
            self.assertEqual(self._options("antigravity"), {"model": "gemini-3.5-flash-low"})
            self.assertEqual(
                self._options("xai"),
                {"model": "grok-4.5", "thinking_level": "low"},
            )

    def test_environment_can_override_model_and_thinking_level(self):
        with mock.patch.dict(
            os.environ,
            {
                "AGENT_COLLAB_IT_CODEX_MODEL": "custom-codex",
                "AGENT_COLLAB_IT_CODEX_THINKING_LEVEL": "medium",
            },
            clear=True,
        ):
            self.assertEqual(
                self._options("codex"),
                {"model": "custom-codex", "thinking_level": "medium"},
            )

    def test_selection_uses_canonical_backend_names(self):
        harness.configure(["claude_sdk", "codex_cli"], strict=True)
        self.assertTrue(harness.selected("claude", "sdk"))
        self.assertTrue(harness.selected("codex", "cli"))
        self.assertFalse(harness.selected("claude", "cli"))
        self.assertTrue(
            harness.missing_reason("claude", "sdk", "missing").startswith("[strict-missing]")
        )

    def test_live_agent_resolves_enabled_and_disabled_canonical_backend_sources(self):
        for provider in ("claude", "codex", "antigravity", "xai"):
            for backend_id in ("cli", "sdk"):
                with self.subTest(provider=provider, backend_id=backend_id):
                    case = LiveBackendTestCase(methodName="runTest")
                    case.provider = provider
                    case.backend_id = backend_id
                    agent = case.live_agent()
                    canonical = f"{provider}_{backend_id}"
                    section = harness.builtin_config().backends[canonical]
                    self.assertEqual(agent.id, canonical)
                    self.assertEqual(agent.type, provider)
                    self.assertEqual(agent.backend, backend_id)
                    self.assertEqual(agent.command, section.command)
                    self.assertEqual(agent.args, section.args)
                    self.assertEqual(agent.env, section.env)
                    self.assertEqual(agent.cwd, section.cwd)
                    self.assertEqual(agent.timeout, section.timeout)
                    self.assertEqual(agent.backend_config, section.backend_config)
                    self.assertEqual(agent.options, section.options)
                    self.assertEqual(agent.default_options, section.default_options)


if __name__ == "__main__":
    unittest.main()
