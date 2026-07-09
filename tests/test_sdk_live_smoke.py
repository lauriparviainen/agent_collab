"""Live SDK smoke tests — SKIPPED BY DEFAULT (never run in `unittest discover`).

Stage 5.1 implementation step 12 (a live one-turn smoke per SDK on a credentialed
machine) cannot run in CI or a normal local unit run: it needs the real SDK wheels
installed plus provider credentials. Each test here is gated behind an env var and
skips otherwise, so the default `python3 -m unittest discover -s tests` stays
hermetic and fake-module based.

To run a live smoke on a credentialed dev machine:

    AGENT_COLLAB_LIVE_SDK_SMOKE=1 ANTHROPIC_API_KEY=... \\
        python3 -m unittest tests.test_sdk_live_smoke -v

Set the matching auth for each provider you want to exercise:
  - claude:      ANTHROPIC_API_KEY (or Claude Code local sign-in)
  - codex:       OPENAI_API_KEY (or Codex local sign-in)
  - antigravity: GEMINI_API_KEY (or Vertex/ADC)
"""

import asyncio
import os
import unittest
from pathlib import Path

LIVE = os.environ.get("AGENT_COLLAB_LIVE_SDK_SMOKE")

_PROMPT = "Reply with the single word: ready."


def _skip_reason(auth_env: str) -> str:
    return (
        f"live SDK smoke disabled; set AGENT_COLLAB_LIVE_SDK_SMOKE=1 and {auth_env} "
        "on a credentialed machine with the SDK installed"
    )


async def _one_turn(runner) -> list:
    return [event async for event in runner.run(_PROMPT, Path("."))]


class SdkImportSmokeTests(unittest.TestCase):
    @unittest.skipUnless(LIVE, _skip_reason("(no auth needed)"))
    def test_all_three_sdks_import(self):
        import importlib.util

        for module in ("claude_agent_sdk", "openai_codex", "google.antigravity"):
            self.assertIsNotNone(
                importlib.util.find_spec(module), f"{module} is not importable in this environment"
            )


class ClaudeLiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(LIVE and os.environ.get("ANTHROPIC_API_KEY"), _skip_reason("ANTHROPIC_API_KEY"))
    def test_claude_sdk_one_turn(self):
        from agent_collab.backends.claude_sdk import ClaudeSdkBackend
        from agent_collab.config import AgentConfig

        runner = ClaudeSdkBackend().create_runner(
            AgentConfig(id="claude", type="claude", backend="sdk"), verbose=True, options={}
        )
        events = asyncio.run(_one_turn(runner))
        self.assertTrue(any(e.source == "claude" and e.type == "message" for e in events), events)


class CodexLiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(LIVE and os.environ.get("OPENAI_API_KEY"), _skip_reason("OPENAI_API_KEY"))
    def test_codex_sdk_one_turn(self):
        from agent_collab.backends.codex_sdk import CodexSdkBackend
        from agent_collab.config import AgentConfig

        runner = CodexSdkBackend().create_runner(
            AgentConfig(id="codex", type="codex", backend="sdk"), verbose=True, options={}
        )
        events = asyncio.run(_one_turn(runner))
        self.assertTrue(any(e.source == "codex" and e.type == "message" for e in events), events)


class AntigravityLiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(LIVE and os.environ.get("GEMINI_API_KEY"), _skip_reason("GEMINI_API_KEY"))
    def test_antigravity_sdk_one_turn(self):
        from agent_collab.backends.antigravity_sdk import AntigravitySdkBackend
        from agent_collab.config import AgentConfig

        runner = AntigravitySdkBackend().create_runner(
            AgentConfig(id="antigravity", type="antigravity", backend="sdk"), verbose=True, options={}
        )
        events = asyncio.run(_one_turn(runner))
        self.assertTrue(any(e.source == "antigravity" and e.type == "message" for e in events), events)


if __name__ == "__main__":
    unittest.main()
