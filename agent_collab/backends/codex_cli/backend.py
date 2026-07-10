"""Standalone Codex CLI subprocess backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ...backend_contract import OptionSpec, load_option_schema, normalize_declared_options
from ...config import AgentConfig, ConfigError
from ...runners import AgentRunner, SubprocessRunner
from ..base import BackendCapabilities, BackendHealth
from ..common.cli import config_value, flag_value, remove_flag, set_config_value, set_flag_value
from ..common.health import default_version_runner, probe_cli_backend
from ..common.options import configured_choices, resolve_codex_effort
from .parser import parse_codex_line

OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))


class CodexCliBackend:
    id = "cli"
    agent_type = "codex"
    brand_color = "#10A37F"
    event_fidelity = "typed"
    provider_session_id_kind = "thread"
    capabilities = BackendCapabilities()
    checks_credentials = False
    block_on_unavailable = False

    def probe(self) -> BackendHealth:
        return probe_cli_backend("codex", run_version=default_version_runner)

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(OPTION_SCHEMA)

    def normalize_options(self, agent: AgentConfig, requested: Mapping[str, Any]) -> Mapping[str, Any]:
        inferred: Dict[str, Any] = {}
        for field, flag in (
            ("model", "--model"), ("profile", "--profile"), ("sandbox", "--sandbox"),
            ("approval_policy", "--approval-policy"),
        ):
            value = flag_value(agent.args, flag)
            if value is not None:
                inferred[field] = value
        effort = config_value(agent.args, "model_reasoning_effort") or flag_value(agent.args, "--reasoning-effort")
        if effort is not None:
            inferred.update({"thinking_level": effort, "reasoning_effort": effort})
        if "--search" in agent.args:
            inferred["search"] = True
        configured = agent.options_for(self.id)
        normalized = normalize_declared_options(
            requested, self.option_schema(agent), configured=configured, inferred=inferred
        )
        return resolve_codex_effort(normalized, configured_choices(configured, requested))

    def build_command(self, agent: AgentConfig, options: Mapping[str, Any]) -> list[str]:
        command = [agent.command or agent.id, *agent.args]
        effort = options.get("reasoning_effort", options.get("thinking_level"))
        if effort is not None:
            command = remove_flag(command, "--reasoning-effort", has_value=True)
            command = set_config_value(command, "model_reasoning_effort", str(effort))
        for key, flag in (
            ("model", "--model"), ("profile", "--profile"), ("sandbox", "--sandbox"),
            ("approval_policy", "--approval-policy"),
        ):
            if key in options:
                command = set_flag_value(command, flag, str(options[key]))
        if "search" in options:
            command = remove_flag(command, "--search", has_value=False)
            if options["search"]:
                command.append("--search")
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
        return SubprocessRunner(
            agent.id,
            self.build_command(agent, options),
            parse_codex_line,
            verbose,
            env=dict(agent.env),
            cwd=agent.cwd,
            command_builder=lambda _run_dir: self.build_command(agent, options),
        )
