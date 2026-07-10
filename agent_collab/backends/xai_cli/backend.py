"""Grok Build CLI backend using headless ``streaming-json`` output."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ...backend_contract import (
    BackendOptionError,
    OptionSpec,
    load_option_schema,
    normalize_declared_options,
)
from ...config import AgentConfig, ConfigError
from ...runners import AgentRunner, SubprocessRunner
from ..base import BackendCapabilities, BackendHealth
from ..common.cli import (
    flag_value,
    remove_flag,
    set_flag_value_before_print_prompt,
)
from ..common.health import default_version_runner, probe_cli_backend, xai_cli_credentials
from .parser import XaiStreamingParser

OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))


def _canonical_reasoning(options: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(options)
    thinking = result.pop("thinking_level", None)
    native = result.pop("reasoning_effort", None)
    if thinking is not None and native is not None and thinking != native:
        raise BackendOptionError(
            "reasoning_effort",
            "conflicts with thinking_level; use one reasoning field or provide matching values",
        )
    effective = thinking if thinking is not None else native
    if effective is not None:
        result["thinking_level"] = effective
    return result


class XaiCliBackend:
    id = "cli"
    agent_type = "xai"
    # xAI's brand is monochrome rather than a single signature hue. A mid-light
    # neutral remains legible on both dark and light terminal backgrounds.
    brand_color = "#A0A0A0"
    event_fidelity = "message_first"
    provider_session_id_kind = "session"
    capabilities = BackendCapabilities()
    checks_credentials = True
    block_on_unavailable = True

    def probe(self) -> BackendHealth:
        return probe_cli_backend(
            "grok", run_version=default_version_runner, credentials=xai_cli_credentials
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(OPTION_SCHEMA)

    def normalize_options(self, agent: AgentConfig, requested: Mapping[str, Any]) -> Mapping[str, Any]:
        inferred: Dict[str, Any] = {}
        for field, flag in (
            ("model", "--model"),
            ("permission_mode", "--permission-mode"),
            ("sandbox", "--sandbox"),
            ("thinking_level", "--effort"),
            ("reasoning_effort", "--reasoning-effort"),
        ):
            value = flag_value(agent.args, flag)
            if value is not None:
                inferred[field] = value
        normalized = normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=agent.options_for(self.id),
            inferred=inferred,
        )
        return _canonical_reasoning(normalized)

    def build_command(self, agent: AgentConfig, options: Mapping[str, Any]) -> list[str]:
        command = [agent.command or agent.id, *agent.args]
        for key, flag in (
            ("model", "--model"),
            ("permission_mode", "--permission-mode"),
            ("sandbox", "--sandbox"),
        ):
            if key in options:
                command = set_flag_value_before_print_prompt(command, flag, str(options[key]))
        effort = options.get("thinking_level")
        if effort is not None:
            command = remove_flag(command, "--effort", has_value=True)
            command = set_flag_value_before_print_prompt(
                command, "--reasoning-effort", str(effort)
            )
        return command

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        return self.build_command(agent, options) if agent.command else None

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"backend": "cli", "options": dict(options)}

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]) -> AgentRunner:
        if not agent.command:
            raise ConfigError(f"agents.{agent.id}.command is required for backend 'cli'")
        parser = XaiStreamingParser(agent.id)
        return SubprocessRunner(
            agent.id,
            self.build_command(agent, options),
            parser,
            verbose,
            env=dict(agent.env),
            cwd=agent.cwd,
            command_builder=lambda _run_dir: self.build_command(agent, options),
        )
