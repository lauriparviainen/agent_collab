"""The ``cli`` backend: run a turn as a subprocess and parse its stdout.

This is the default backend and stays standard-library only. It owns the
subprocess *construction* that ``runners.configured_runner`` used to hard-wire:
pick the provider's line parser, apply typed options to the argv, and build a
:class:`~agent_collab.runners.SubprocessRunner`. Each provider (``claude``,
``codex`` today) is a separate :class:`CliBackend` instance so the registry can
key them independently.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..config import AgentConfig, ConfigError
from ..events import Event, parse_antigravity_line, parse_claude_line, parse_codex_line
from ..options import build_cli_command
from ..runners import AgentRunner, SubprocessRunner
from .base import BackendCapabilities, BackendHealth
from .health import antigravity_credentials, default_version_runner, probe_cli_backend

Parser = Callable[[str, bool], Optional[Event]]


class CliBackend:
    """Subprocess execution for one provider. Registered as ``(agent_type, "cli")``."""

    id = "cli"

    def __init__(
        self,
        agent_type: str,
        parser: Parser,
        *,
        probe_binary: str,
        capabilities: Optional[BackendCapabilities] = None,
        credentials: Optional[Callable[[], str]] = None,
        block_on_unavailable: bool = False,
    ) -> None:
        self.agent_type = agent_type
        self.parser = parser
        # The provider's conventional binary, used only for presence probing;
        # a session's actual argv still comes from the agent's configured command.
        self.probe_binary = probe_binary
        self.capabilities = capabilities or BackendCapabilities()
        self._credentials = credentials
        # Whether this backend attempts a credential check (drives "unknown"
        # warnings on start); never inferred from provider brand.
        self.checks_credentials = credentials is not None
        # Default providers (claude/codex) keep their legacy contract: a missing
        # binary surfaces as a per-turn error, not a start rejection. Opt-in
        # backends (antigravity) set this True so an unusable setup fails fast.
        self.block_on_unavailable = block_on_unavailable

    def probe(self) -> BackendHealth:
        return probe_cli_backend(
            self.probe_binary,
            run_version=default_version_runner,
            credentials=self._credentials,
        )

    def create_runner(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Dict[str, Any],
    ) -> AgentRunner:
        if not agent.command:
            raise ConfigError(f"agents.{agent.id}.command is required for backend 'cli'")
        command = build_cli_command(agent, options or {})
        return SubprocessRunner(
            agent.id,
            command,
            self.parser,
            verbose,
            env=dict(agent.env),
            cwd=agent.cwd,
            agent=agent,
        )


def build_cli_backends() -> List[CliBackend]:
    """Built-in ``cli`` backends. The registry registers each at import time."""

    return [
        CliBackend("claude", parse_claude_line, probe_binary="claude"),
        CliBackend("codex", parse_codex_line, probe_binary="codex"),
        # Antigravity opts into start-time gating: a missing `agy` or a definite
        # sign-out fails the start fast rather than burning the first turn.
        CliBackend(
            "antigravity",
            parse_antigravity_line,
            probe_binary="agy",
            credentials=antigravity_credentials,
            block_on_unavailable=True,
        ),
    ]
