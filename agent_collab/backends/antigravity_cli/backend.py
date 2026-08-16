"""Standalone Antigravity CLI subprocess backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ...backend_contract import OptionSpec, load_option_schema, normalize_declared_options
from ...config import AgentConfig
from ...runners import AgentRunner
from ..base import BackendCapabilities, BackendHealth
from ..common.cli import (
    cli_settings_summary,
    create_cli_runner,
    flag_value,
    has_flag,
    insert_before_print_prompt,
    remove_flag,
    resolve_run_dir,
    set_flag_value_before_print_prompt,
)
from ..common.health import antigravity_credentials, default_version_runner, probe_cli_backend
from .invocation import CLI_OWNERSHIP_FLAGS, finalize_antigravity_cli_invocation
from .parser import AntigravityStreamingParser
from .sandbox import AntigravityCliSandboxAdapter

OPTION_SCHEMA = load_option_schema(Path(__file__).with_name("options.toml"))
REQUIRED_AGY_VERSION = "1.1.8"

_TRUE_BOOLEAN_VALUES = frozenset({"1", "t", "true"})
_FALSE_BOOLEAN_VALUES = frozenset({"0", "f", "false"})


def _sandbox_flag_value(args: list[str]) -> Optional[bool]:
    """Return the last valid Go-style boolean value supplied for --sandbox."""

    result: Optional[bool] = None
    prefix = "--sandbox="
    for item in args:
        if item == "--sandbox":
            result = True
        elif item.startswith(prefix):
            value = item[len(prefix) :].lower()
            if value in _TRUE_BOOLEAN_VALUES:
                result = True
            elif value in _FALSE_BOOLEAN_VALUES:
                result = False
            else:
                return None
    return result


class AntigravityCliBackend:
    id = "cli"
    agent_type = "antigravity"
    brand_color = "#4285F4"
    event_fidelity = "typed"
    provider_session_id_kind = "conversation"
    capabilities = BackendCapabilities()
    cli_ownership_flags = CLI_OWNERSHIP_FLAGS
    finalize_cli_invocation = staticmethod(finalize_antigravity_cli_invocation)
    sandbox_adapter = AntigravityCliSandboxAdapter()
    checks_credentials = True
    block_on_unavailable = True
    clean_eof_fallback = False

    def probe(self) -> BackendHealth:
        return probe_cli_backend(
            "agy",
            run_version=default_version_runner,
            credentials=antigravity_credentials,
            min_version=REQUIRED_AGY_VERSION,
        )

    def probe_for_agent(self, agent: AgentConfig) -> BackendHealth:
        return probe_cli_backend(
            agent.command or agent.id,
            run_version=default_version_runner,
            credentials=antigravity_credentials,
            min_version=REQUIRED_AGY_VERSION,
        )

    def option_schema(self, agent: AgentConfig) -> Mapping[str, OptionSpec]:
        return dict(OPTION_SCHEMA)

    def normalize_options(
        self, agent: AgentConfig, requested: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        inferred: Dict[str, Any] = {}
        for field, flag in (("model", "--model"), ("mode", "--mode")):
            value = flag_value(agent.args, flag)
            if value is not None:
                inferred[field] = value
        sandbox = _sandbox_flag_value(agent.args)
        if sandbox is not None:
            inferred["sandbox"] = sandbox
        return normalize_declared_options(
            requested,
            self.option_schema(agent),
            configured=agent.options_for(self.id),
            inferred=inferred,
            configured_defaults=agent.default_options_for(self.id),
        )

    def build_command(
        self, agent: AgentConfig, options: Mapping[str, Any], run_dir: Optional[Path] = None
    ) -> list[str]:
        command = [agent.command or agent.id, *agent.args]
        command = set_flag_value_before_print_prompt(command, "--output-format", "stream-json")
        for key, flag in (("model", "--model"), ("mode", "--mode")):
            if key in options:
                command = set_flag_value_before_print_prompt(command, flag, str(options[key]))
        if "sandbox" in options:
            command = remove_flag(command, "--sandbox", has_value=False)
            if options["sandbox"]:
                command = insert_before_print_prompt(command, ["--sandbox"])
        if agent.timeout is not None and not has_flag(command, "--print-timeout"):
            command = insert_before_print_prompt(
                command, ["--print-timeout", f"{max(0, int(agent.timeout))}s"]
            )
        if run_dir is not None and not has_flag(command, "--add-dir"):
            command = insert_before_print_prompt(command, ["--add-dir", str(run_dir.resolve())])
        return command

    def command_preview(
        self, agent: AgentConfig, options: Mapping[str, Any], workdir: Optional[Path] = None
    ) -> Optional[list[str]]:
        # Unlike the other CLI backends, the command depends on the run
        # directory (--add-dir), so the shared run-dir-independent preview
        # helper does not apply.
        if not agent.command:
            return None
        run_dir = None
        if workdir is not None:
            run_dir = resolve_run_dir(workdir, agent.cwd)
        return self.build_command(agent, options, run_dir)

    def settings_summary(self, agent: AgentConfig, options: Mapping[str, Any]) -> Mapping[str, Any]:
        return cli_settings_summary(options)

    def create_runner(
        self, agent: AgentConfig, verbose: bool, options: Mapping[str, Any]
    ) -> AgentRunner:
        return create_cli_runner(
            self,
            agent,
            verbose,
            options,
            AntigravityStreamingParser(agent.id),
            command_builder=lambda run_dir: self.build_command(agent, options, run_dir),
        )
