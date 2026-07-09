"""The ``cli`` backend: run a turn as a subprocess and parse its stdout.

This is the default backend and stays standard-library only. It owns the
subprocess *construction* that ``runners.configured_runner`` used to hard-wire:
pick the provider's line parser, apply typed options to the argv, and build a
:class:`~agent_collab.runners.SubprocessRunner`. Each provider (``claude``,
``codex`` today) is a separate :class:`CliBackend` instance so the registry can
key them independently.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..config import AgentConfig, ConfigError
from ..events import Event, parse_antigravity_line, parse_claude_line, parse_codex_line
from ..options import build_cli_command
from ..runners import AgentRunner, SubprocessRunner
from .base import (
    BackendCapabilities,
    BackendHealth,
    BackendOptionError,
    OptionSpec,
    normalize_declared_options,
)
from .health import antigravity_credentials, default_version_runner, probe_cli_backend

Parser = Callable[[str, bool], Optional[Event]]

_CODEX_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
_CLAUDE_LEVELS = ("low", "medium", "high", "xhigh", "max")

CLI_OPTION_SCHEMAS: Dict[str, Dict[str, OptionSpec]] = {
    "claude": {
        "model": OptionSpec("string", inferred=True),
        "permission_mode": OptionSpec(
            "string",
            allowed=("default", "acceptEdits", "bypassPermissions"),
            inferred=True,
        ),
        "thinking_level": OptionSpec("string", allowed=_CLAUDE_LEVELS, inferred=True),
        "thinking_budget_tokens": OptionSpec("integer", minimum=0, inferred=True),
    },
    "codex": {
        "model": OptionSpec("string", inferred=True),
        "profile": OptionSpec("string", inferred=True),
        "thinking_level": OptionSpec("string", allowed=_CODEX_LEVELS, inferred=True),
        "reasoning_effort": OptionSpec("string", allowed=_CODEX_LEVELS, inferred=True),
        "sandbox": OptionSpec(
            "string",
            allowed=("read-only", "workspace-write", "danger-full-access"),
            inferred=True,
        ),
        "approval_policy": OptionSpec(
            "string",
            allowed=("untrusted", "on-failure", "on-request", "never"),
            inferred=True,
        ),
        "search": OptionSpec("boolean", allowed=(True, False), inferred=True),
    },
    "antigravity": {
        "model": OptionSpec("string", inferred=True),
        "mode": OptionSpec(
            "string",
            allowed=("default", "accept-edits", "plan"),
            inferred=True,
        ),
    },
}


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

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(CLI_OPTION_SCHEMAS[self.agent_type])

    def normalize_options(
        self,
        agent: AgentConfig,
        requested: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        schema = self.option_schema(agent)
        inferred = {
            field: value
            for field in schema
            for value in [_infer_cli_option(agent, field)]
            if value is not None
        }
        normalized = normalize_declared_options(agent, requested, schema, inferred=inferred)
        return _normalize_provider_aliases(self.agent_type, normalized, set(requested))

    def settings_summary(
        self,
        agent: AgentConfig,
        options: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"backend": "cli", "options": dict(options)}

    def create_runner(
        self,
        agent: AgentConfig,
        verbose: bool,
        options: Mapping[str, Any],
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


def _normalize_provider_aliases(
    agent_type: str,
    options: Dict[str, Any],
    explicit: set,
) -> Dict[str, Any]:
    if agent_type == "codex":
        if {"thinking_level", "reasoning_effort"}.issubset(explicit):
            if options.get("thinking_level") != options.get("reasoning_effort"):
                raise BackendOptionError(
                    "thinking_level",
                    "conflicts with reasoning_effort; use one thinking level field or provide matching values",
                )
        if "thinking_level" in explicit:
            options["reasoning_effort"] = options["thinking_level"]
        elif "reasoning_effort" in explicit:
            options["thinking_level"] = options["reasoning_effort"]
        elif "reasoning_effort" in options:
            options["thinking_level"] = options["reasoning_effort"]
        elif "thinking_level" in options:
            options["reasoning_effort"] = options["thinking_level"]
    elif agent_type == "claude":
        if {"thinking_level", "thinking_budget_tokens"}.issubset(explicit):
            raise BackendOptionError(
                "thinking_level",
                "conflicts with thinking_budget_tokens; use thinking_level or a raw token budget, not both",
            )
        if "thinking_budget_tokens" in explicit:
            options.pop("thinking_level", None)
        elif "thinking_level" in explicit:
            options.pop("thinking_budget_tokens", None)
    return options


def _infer_cli_option(agent: AgentConfig, field: str) -> Optional[Any]:
    args = list(agent.args)
    if field == "model":
        return _flag_value(args, "--model")
    if field == "profile":
        return _flag_value(args, "--profile")
    if field == "sandbox":
        return _flag_value(args, "--sandbox")
    if field == "approval_policy":
        return _flag_value(args, "--approval-policy")
    if field == "thinking_level":
        if agent.type == "codex":
            return _config_value(args, "model_reasoning_effort") or _flag_value(args, "--reasoning-effort")
        if agent.type == "claude":
            return _flag_value(args, "--effort")
    if field == "reasoning_effort":
        return _config_value(args, "model_reasoning_effort") or _flag_value(args, "--reasoning-effort")
    if field == "permission_mode":
        return _flag_value(args, "--permission-mode")
    if field == "mode":
        return _flag_value(args, "--mode")
    if field == "thinking_budget_tokens":
        value = _flag_value(args, "--thinking-budget-tokens")
        return int(value) if value is not None and value.isdigit() else value
    if field == "search":
        return True if "--search" in args else None
    return None


def _flag_value(args: Sequence[str], flag: str) -> Optional[str]:
    prefix = f"{flag}="
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def _config_value(args: Sequence[str], key: str) -> Optional[str]:
    for index, item in enumerate(args):
        value: Optional[str] = None
        if item in {"-c", "--config"} and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith("--config="):
            value = item[len("--config=") :]
        if value is not None and value.split("=", 1)[0].strip() == key and "=" in value:
            return value.split("=", 1)[1].strip("\"'")
    return None


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
