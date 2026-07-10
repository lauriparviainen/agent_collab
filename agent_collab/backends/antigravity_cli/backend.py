"""Standalone Antigravity CLI subprocess backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ...backend_contract import OptionSpec, load_option_schema, normalize_declared_options
from ...config import AgentConfig, ConfigError
from ...runners import AgentRunner, SubprocessRunner
from ..base import BackendCapabilities, BackendHealth
from ..common.cli import flag_value, has_flag, insert_before_print_prompt, set_flag_value_before_print_prompt
from ..common.health import antigravity_credentials, default_version_runner, probe_cli_backend
from .parser import parse_antigravity_line

OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))


class AntigravityCliBackend:
    id = "cli"
    agent_type = "antigravity"
    brand_color = "#4285F4"
    event_fidelity = "message_only"
    provider_session_id_kind = None
    capabilities = BackendCapabilities()
    checks_credentials = True
    block_on_unavailable = True

    def probe(self) -> BackendHealth:
        return probe_cli_backend(
            "agy", run_version=default_version_runner, credentials=antigravity_credentials
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(OPTION_SCHEMA)

    def normalize_options(self, agent: AgentConfig, requested: Mapping[str, Any]) -> Mapping[str, Any]:
        inferred: Dict[str, Any] = {}
        for field, flag in (("model", "--model"), ("mode", "--mode")):
            value = flag_value(agent.args, flag)
            if value is not None:
                inferred[field] = value
        return normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=agent.options_for(self.id),
            inferred=inferred,
        )

    def build_command(
        self, agent: AgentConfig, options: Mapping[str, Any], run_dir: Optional[Path] = None
    ) -> list[str]:
        command = [agent.command or agent.id, *agent.args]
        for key, flag in (("model", "--model"), ("mode", "--mode")):
            if key in options:
                command = set_flag_value_before_print_prompt(command, flag, str(options[key]))
        if run_dir is not None and not has_flag(command, "--add-dir"):
            command = insert_before_print_prompt(command, ["--add-dir", str(run_dir.resolve())])
        return command

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        if not agent.command:
            return None
        run_dir = None
        if workdir is not None:
            from ..common.cli import resolve_run_dir

            run_dir = resolve_run_dir(workdir, agent.cwd)
        return self.build_command(agent, options, run_dir)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"backend": "cli", "options": dict(options)}

    def create_runner(self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]) -> AgentRunner:
        if not agent.command:
            raise ConfigError(f"agents.{agent.id}.command is required for backend 'cli'")
        return SubprocessRunner(
            agent.id,
            self.build_command(agent, options),
            parse_antigravity_line,
            verbose,
            env=dict(agent.env),
            cwd=agent.cwd,
            command_builder=lambda run_dir: self.build_command(agent, options, run_dir),
        )
