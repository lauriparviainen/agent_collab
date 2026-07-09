"""Selection, workspace isolation, and assertions for live backend tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from agent_collab import backends
from agent_collab.backends.base import CREDENTIALS_MISSING, HEALTH_UNAVAILABLE
from agent_collab.config import AgentConfig, builtin_config

PROVIDERS = {"claude", "codex", "antigravity"}
BACKENDS = {"cli", "sdk"}
_selected_providers: Set[str] = set(PROVIDERS)
_selected_backends: Set[str] = set(BACKENDS)
_explicit_providers: Set[str] = set()
STRICT = False
REPO_ROOT = Path(__file__).resolve().parents[1]

# Live tests verify transport/event fidelity, not frontier-model quality. Keep
# their paid calls on each provider's fast, economical tier by default.
DEFAULT_LIVE_OPTIONS: Dict[str, Dict[str, Any]] = {
    "claude": {"model": "sonnet", "thinking_level": "low"},
    "codex": {"model": "gpt-5.6-luna", "thinking_level": "low"},
    "antigravity": {"model": "Gemini 3.5 Flash (Low)"},
}


def configure(
    providers: Optional[Iterable[str]] = None,
    backend_ids: Optional[Iterable[str]] = None,
    *,
    strict: bool = False,
    explicit_providers: Optional[Iterable[str]] = None,
) -> None:
    global _selected_providers, _selected_backends, _explicit_providers, STRICT
    _selected_providers = set(providers or PROVIDERS)
    _selected_backends = set(backend_ids or BACKENDS)
    _explicit_providers = set(explicit_providers or ())
    STRICT = strict


def selected(provider: str, backend_id: str) -> bool:
    return provider in _selected_providers and backend_id in _selected_backends


def missing_reason(provider: str, backend_id: str, reason: str) -> str:
    strict = STRICT and provider in _explicit_providers
    return f"[strict-missing] {provider}_{backend_id}: {reason}" if strict else f"[missing] {provider}_{backend_id}: {reason}"


async def _collect(runner: Any, prompt: str, workdir: Path) -> list:
    return [event async for event in runner.run(prompt, workdir)]


class LiveBackendTestCase(unittest.TestCase):
    provider = ""
    backend_id = ""
    prompt = "Reply with the single word: ready."

    def setUp(self) -> None:
        if not selected(self.provider, self.backend_id):
            self.skipTest("[unselected] backend not selected")
        backend = backends.get_backend(self.provider, self.backend_id)
        health = backend.probe()
        if health.status == HEALTH_UNAVAILABLE:
            self.skipTest(missing_reason(self.provider, self.backend_id, health.reason or "unavailable"))
        if health.credentials == CREDENTIALS_MISSING:
            self.skipTest(missing_reason(self.provider, self.backend_id, health.reason or "credentials missing"))

    def requested_options(self) -> Dict[str, Any]:
        options = dict(DEFAULT_LIVE_OPTIONS[self.provider])
        prefix = f"AGENT_COLLAB_IT_{self.provider.upper()}"
        if os.environ.get(f"{prefix}_MODEL"):
            options["model"] = os.environ[f"{prefix}_MODEL"]
        if "thinking_level" in options and os.environ.get(f"{prefix}_THINKING_LEVEL"):
            options["thinking_level"] = os.environ[f"{prefix}_THINKING_LEVEL"]
        return options

    def run_live(self, prompt: Optional[str] = None) -> list:
        config = builtin_config()
        source = config.agents[self.provider]
        agent = AgentConfig(
            id=source.id,
            type=source.type,
            command=source.command,
            args=list(source.args),
            enabled=True,
            env=dict(source.env),
            cwd=source.cwd,
            backend=self.backend_id,
        )
        backend = backends.get_backend(self.provider, self.backend_id)
        options = backend.normalize_options(agent, self.requested_options())
        runner = backend.create_runner(agent, True, options)
        with tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp, tempfile.TemporaryDirectory(
            prefix="agent-collab-it-home-"
        ) as home:
            workdir = Path(tmp).resolve()
            self.assertNotEqual(workdir, REPO_ROOT)
            self.assertNotIn(REPO_ROOT, workdir.parents)
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = home
            try:
                return asyncio.run(_collect(runner, prompt or self.prompt, workdir))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_COLLAB_HOME", None)
                else:
                    os.environ["AGENT_COLLAB_HOME"] = previous

    def assert_message(self, events: list) -> None:
        errors = [event.text for event in events if event.type == "error"]
        self.assertFalse(errors, f"backend emitted {len(errors)} error event(s)")
        self.assertTrue(
            any(event.source == self.provider and event.type == "message" for event in events),
            f"no provider message; event kinds={[(event.source, event.type) for event in events]}",
        )

    def assert_session_kind(self, events: list, kind: str) -> None:
        self.assertTrue(
            any((event.raw or {}).get("provider_session_kind") == kind for event in events),
            f"no {kind} identity; event kinds={[(event.source, event.type) for event in events]}",
        )
