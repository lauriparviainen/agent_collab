"""Stateless helpers shared by subprocess backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ...config import AgentConfig, ConfigError
from ...runners import AgentRunner, CommandBuilder, Parser, SubprocessRunner


def flag_value(args: Sequence[str], flag: str) -> Optional[str]:
    prefix = f"{flag}="
    result = None
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            result = args[index + 1]
        elif item.startswith(prefix):
            result = item[len(prefix) :]
    return result


def config_value(args: Sequence[str], key: str) -> Optional[str]:
    result = None
    for index, item in enumerate(args):
        value: Optional[str] = None
        if item in {"-c", "--config"} and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith("--config="):
            value = item[len("--config=") :]
        if value is not None and "=" in value and value.split("=", 1)[0].strip() == key:
            result = value.split("=", 1)[1].strip("\"'")
    return result


def set_flag_value(command: Sequence[str], flag: str, value: str) -> List[str]:
    result = remove_flag(command, flag, has_value=True)
    result.extend([flag, value])
    return result


def set_flag_value_before_print_prompt(command: Sequence[str], flag: str, value: str) -> List[str]:
    return insert_before_print_prompt(remove_flag(command, flag, has_value=True), [flag, value])


def insert_before_print_prompt(command: Sequence[str], items: Sequence[str]) -> List[str]:
    result = list(command)
    for index, item in enumerate(result):
        if item in {"-p", "--print", "--prompt", "--single"}:
            return result[:index] + list(items) + result[index:]
    return result + list(items)


def has_flag(command: Sequence[str], flag: str) -> bool:
    prefix = f"{flag}="
    return any(item == flag or item.startswith(prefix) for item in command)


def set_config_value(command: Sequence[str], key: str, value: str) -> List[str]:
    result = remove_config_value(command, key)
    result.extend(["-c", f'{key}="{value}"'])
    return result


def remove_flag(command: Sequence[str], flag: str, *, has_value: bool) -> List[str]:
    result: List[str] = []
    skip_next = False
    prefix = f"{flag}="
    for item in command:
        if skip_next:
            skip_next = False
            continue
        if item == flag:
            skip_next = has_value
            continue
        if item.startswith(prefix):
            continue
        result.append(item)
    return result


def remove_config_value(command: Sequence[str], key: str) -> List[str]:
    result: List[str] = []
    skip_next = False
    values = list(command)
    for index, item in enumerate(values):
        if skip_next:
            skip_next = False
            continue
        if item in {"-c", "--config"} and index + 1 < len(values):
            if config_item_key(values[index + 1]) == key:
                skip_next = True
                continue
        if item.startswith("--config=") and config_item_key(item[len("--config=") :]) == key:
            continue
        result.append(item)
    return result


def config_item_key(value: str) -> str:
    return value.split("=", 1)[0].strip()


def cli_settings_summary(options: Mapping[str, Any]) -> Dict[str, Any]:
    """The uniform CLI ``settings_summary``: the effective options verbatim."""

    return {"backend": "cli", "options": dict(options)}


def cli_command_preview(
    backend: Any, agent: AgentConfig, options: Mapping[str, Any]
) -> Optional[List[str]]:
    """The uniform run-dir-independent ``command_preview``.

    Backends whose command depends on the run directory (antigravity) keep
    their own preview instead.
    """

    return backend.build_command(agent, options) if agent.command else None


def create_cli_runner(
    backend: Any,
    agent: AgentConfig,
    verbose: bool,
    options: Mapping[str, Any],
    parser: Parser,
    command_builder: Optional[CommandBuilder] = None,
) -> AgentRunner:
    """Construct the ``SubprocessRunner`` every CLI backend shares.

    Requires ``agent.command``, threads the backend's provider type so stderr
    attribution never follows the display id, and defaults the rebuild hook to
    a run-dir-independent ``build_command``.
    """

    if not agent.command:
        raise ConfigError(f"agents.{agent.id}.command is required for backend 'cli'")
    if command_builder is None:

        def command_builder(_run_dir: Path) -> List[str]:
            return backend.build_command(agent, options)

    return SubprocessRunner(
        agent.id,
        backend.build_command(agent, options),
        parser,
        verbose,
        env=dict(agent.env),
        cwd=agent.cwd,
        command_builder=command_builder,
        source=backend.agent_type,
        clean_eof_fallback=bool(getattr(backend, "clean_eof_fallback", False)),
    )


def resolve_run_dir(workdir: Path, cwd: Optional[str]) -> Path:
    base = workdir.expanduser().resolve()
    if not cwd:
        return base
    path = Path(cwd).expanduser()
    return path if path.is_absolute() else (base / path).resolve()
