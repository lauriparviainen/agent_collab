"""Live SDK smoke tests — SKIPPED BY DEFAULT (never run in `unittest discover`).

Stage 5.1 implementation step 12 (a live one-turn smoke per SDK on a credentialed
machine) cannot run in CI or a normal local unit run: it needs the real SDK wheels
installed plus provider credentials. Each test here is gated behind an env var and
skips otherwise, so the default `python3 -m unittest discover -s tests` stays
hermetic and fake-module based.

To run import and no-model constructor smokes in an installed-SDK environment:

    AGENT_COLLAB_LIVE_SDK_SMOKE=1 \\
        python3 -m unittest tests.test_sdk_live_smoke -v

The constructor smokes import real public types and start a Codex thread, but do
not run a model turn. One-turn tests require a separate provider-specific opt-in
so local sign-in works without copying credentials into environment variables:

  - ``AGENT_COLLAB_LIVE_CLAUDE_SDK_SMOKE=1``
  - ``AGENT_COLLAB_LIVE_CODEX_SDK_SMOKE=1``
  - ``AGENT_COLLAB_LIVE_ANTIGRAVITY_SDK_SMOKE=1``

The older combination of ``AGENT_COLLAB_LIVE_SDK_SMOKE=1`` plus the matching
API-key environment variable remains supported. A provider-specific flag means
"attempt the turn using whatever auth the SDK normally discovers"; it does not
make agent-collab manage or validate credentials.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

LIVE = bool(os.environ.get("AGENT_COLLAB_LIVE_SDK_SMOKE"))
CLAUDE_LIVE = bool(
    os.environ.get("AGENT_COLLAB_LIVE_CLAUDE_SDK_SMOKE")
    or (LIVE and os.environ.get("ANTHROPIC_API_KEY"))
)
CODEX_LIVE = bool(
    os.environ.get("AGENT_COLLAB_LIVE_CODEX_SDK_SMOKE")
    or (LIVE and os.environ.get("OPENAI_API_KEY"))
)
ANTIGRAVITY_LIVE = bool(
    os.environ.get("AGENT_COLLAB_LIVE_ANTIGRAVITY_SDK_SMOKE")
    or (LIVE and os.environ.get("GEMINI_API_KEY"))
)

_PROMPT = "Reply with the single word: ready."


def _skip_reason(flag: str) -> str:
    return f"SDK smoke disabled; set {flag}=1 in an installed-SDK environment"


async def _one_turn(runner, workdir: Path, prompt: str = _PROMPT) -> list:
    return [event async for event in runner.run(prompt, workdir)]


def _run_in_empty_workspace(runner, prompt: str = _PROMPT) -> list:
    # Never expose the checkout to a live-smoke agent. The model sees only the
    # explicit prompt and a fresh disposable workspace.
    with tempfile.TemporaryDirectory(prefix="agent-collab-sdk-smoke-") as tmp:
        return asyncio.run(_one_turn(runner, Path(tmp), prompt))


class SdkImportSmokeTests(unittest.TestCase):
    @unittest.skipUnless(LIVE, _skip_reason("AGENT_COLLAB_LIVE_SDK_SMOKE"))
    def test_all_three_sdks_import(self):
        import importlib.util

        for module in ("claude_agent_sdk", "openai_codex", "google.antigravity"):
            self.assertIsNotNone(
                importlib.util.find_spec(module), f"{module} is not importable in this environment"
            )


class SdkNoModelSmokeTests(unittest.TestCase):
    @unittest.skipUnless(LIVE, _skip_reason("AGENT_COLLAB_LIVE_SDK_SMOKE"))
    def test_claude_options_construct_with_verified_coding_presets(self):
        from claude_agent_sdk import ClaudeAgentOptions

        from agent_collab.backends.claude_sdk import build_claude_agent_options

        options = build_claude_agent_options(
            ClaudeAgentOptions,
            {"thinking_level": "high", "thinking_budget_tokens": 1024},
            Path.cwd(),
        )
        self.assertEqual(options.cwd, str(Path.cwd()))
        self.assertEqual(options.setting_sources, [])
        self.assertEqual(options.system_prompt, {"type": "preset", "preset": "claude_code"})
        self.assertEqual(options.tools, {"type": "preset", "preset": "claude_code"})

    @unittest.skipUnless(LIVE, _skip_reason("AGENT_COLLAB_LIVE_SDK_SMOKE"))
    def test_antigravity_agent_constructs_with_verified_workspace_config(self):
        from google.antigravity import Agent, LocalAgentConfig

        config = LocalAgentConfig(workspaces=[str(Path.cwd())], model="gemini-2.5-pro")
        agent = Agent(config)
        self.assertIsInstance(agent, Agent)

    @unittest.skipUnless(LIVE, _skip_reason("AGENT_COLLAB_LIVE_SDK_SMOKE"))
    def test_codex_client_starts_ephemeral_thread_without_model_turn(self):
        from openai_codex import AsyncCodex, Sandbox

        async def construct():
            async with AsyncCodex() as client:
                thread = await client.thread_start(
                    cwd=str(Path.cwd()),
                    sandbox=Sandbox.read_only,
                    ephemeral=True,
                )
                return thread.id

        self.assertTrue(asyncio.run(construct()))


class ClaudeLiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(
        CLAUDE_LIVE,
        _skip_reason("AGENT_COLLAB_LIVE_CLAUDE_SDK_SMOKE"),
    )
    def test_claude_sdk_one_turn(self):
        from agent_collab.backends.claude_sdk import ClaudeSdkBackend
        from agent_collab.config import AgentConfig

        runner = ClaudeSdkBackend().create_runner(
            AgentConfig(id="claude", type="claude", backend="sdk"), verbose=True, options={}
        )
        events = _run_in_empty_workspace(runner)
        self.assertTrue(any(e.source == "claude" and e.type == "message" for e in events), events)
        self.assertTrue(any((e.raw or {}).get("provider_session_kind") == "session" for e in events), events)


class CodexLiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(
        CODEX_LIVE,
        _skip_reason("AGENT_COLLAB_LIVE_CODEX_SDK_SMOKE"),
    )
    def test_codex_sdk_one_turn(self):
        from agent_collab.backends.codex_sdk import CodexSdkBackend
        from agent_collab.config import AgentConfig

        runner = CodexSdkBackend().create_runner(
            AgentConfig(id="codex", type="codex", command="codex", backend="sdk"),
            verbose=True,
            options={"model": os.environ.get("AGENT_COLLAB_CODEX_SDK_SMOKE_MODEL", "gpt-5.6-sol")},
        )
        events = _run_in_empty_workspace(runner)
        self.assertTrue(any(e.source == "codex" and e.type == "message" for e in events), events)
        self.assertTrue(any((e.raw or {}).get("provider_session_kind") == "thread" for e in events), events)


class AntigravityLiveSmokeTests(unittest.TestCase):
    @unittest.skipUnless(
        ANTIGRAVITY_LIVE,
        _skip_reason("AGENT_COLLAB_LIVE_ANTIGRAVITY_SDK_SMOKE"),
    )
    def test_antigravity_sdk_one_turn(self):
        from agent_collab.backends.antigravity_sdk import AntigravitySdkBackend
        from agent_collab.config import AgentConfig

        runner = AntigravitySdkBackend().create_runner(
            AgentConfig(id="antigravity", type="antigravity", backend="sdk"), verbose=True, options={}
        )
        events = _run_in_empty_workspace(
            runner,
            "Create sdk-smoke.txt containing the single word ready, read it back, then reply ready.",
        )
        self.assertTrue(any(e.source == "antigravity" and e.type == "message" for e in events), events)
        self.assertTrue(
            any(e.source == "tool" and e.type in {"tool_call", "file_change"} for e in events),
            events,
        )
        self.assertTrue(
            any((e.raw or {}).get("provider_session_kind") == "conversation" for e in events),
            events,
        )


if __name__ == "__main__":
    unittest.main()
