from __future__ import annotations

from dataclasses import dataclass, field
import ast
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class ConfigError(ValueError):
    """Raised when agent-collab configuration is invalid."""


@dataclass
class AgentConfig:
    id: str
    type: str
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    enabled: bool = True
    name: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    timeout: Optional[int] = None
    options: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class ModeConfig:
    id: str
    sequence: List[str] = field(default_factory=list)


@dataclass
class CollaborationConfig:
    agents: Dict[str, AgentConfig] = field(default_factory=dict)
    modes: Dict[str, ModeConfig] = field(default_factory=dict)
    loaded_paths: List[Path] = field(default_factory=list)


BUILTIN_CONFIG: Dict[str, Any] = {
    "agents": {
        "claude": {
            "type": "claude",
            "command": "claude",
            "args": ["-p", "--output-format", "stream-json", "--verbose"],
            "enabled": True,
        },
        "codex": {
            "type": "codex",
            "command": "codex",
            "args": ["exec", "--json"],
            "enabled": True,
        },
    },
    "modes": {
        "claude-leads": {"sequence": ["claude", "codex", "claude"]},
        "codex-leads": {"sequence": ["codex", "claude", "codex"]},
        "debate": {"sequence": ["claude", "codex", "claude", "codex"]},
    },
}


SUBPROCESS_AGENT_TYPES = {"claude", "codex"}
AGENT_TYPES = SUBPROCESS_AGENT_TYPES | {"mock"}


def builtin_config() -> CollaborationConfig:
    config = CollaborationConfig()
    merge_config_data(config, BUILTIN_CONFIG)
    return config


def config_search_paths(workdir: Path, home: Optional[Path] = None) -> List[Path]:
    root = workdir.expanduser().resolve()
    user_home = (home or Path.home()).expanduser()
    return [
        root / ".agent-collab" / "config.toml",
        user_home / ".agent-collab" / "config.toml",
    ]


def load_config(
    workdir: Path,
    home: Optional[Path] = None,
) -> CollaborationConfig:
    """Load built-ins, then user config, then project config.

    The public lookup precedence is project config, user config, then built-ins.
    Applying the files in reverse order lets project values override user values.
    """

    config = builtin_config()
    project_path, user_path = config_search_paths(workdir, home)
    for path in (user_path, project_path):
        if path.exists():
            merge_config_data(config, _load_toml_file(path))
            config.loaded_paths.append(path)

    validate_config(config)
    return config


def merge_config_data(config: CollaborationConfig, data: Mapping[str, Any]) -> None:
    agents = data.get("agents", {})
    if agents is not None:
        if not isinstance(agents, Mapping):
            raise ConfigError("[agents] must be a table")
        for agent_id, values in agents.items():
            if not isinstance(values, Mapping):
                raise ConfigError(f"[agents.{agent_id}] must be a table")
            config.agents[str(agent_id)] = _merge_agent(config.agents.get(str(agent_id)), str(agent_id), values)

    modes = data.get("modes", {})
    if modes is not None:
        if not isinstance(modes, Mapping):
            raise ConfigError("[modes] must be a table")
        for mode_id, values in modes.items():
            if not isinstance(values, Mapping):
                raise ConfigError(f"[modes.{mode_id}] must be a table")
            config.modes[str(mode_id)] = _merge_mode(config.modes.get(str(mode_id)), str(mode_id), values)


def _merge_agent(existing: Optional[AgentConfig], agent_id: str, values: Mapping[str, Any]) -> AgentConfig:
    agent = AgentConfig(id=agent_id, type="") if existing is None else AgentConfig(
        id=existing.id,
        type=existing.type,
        command=existing.command,
        args=list(existing.args),
        enabled=existing.enabled,
        name=existing.name,
        env=dict(existing.env),
        cwd=existing.cwd,
        timeout=existing.timeout,
        options={key: dict(value) for key, value in existing.options.items()},
    )
    for key, value in values.items():
        if key == "type":
            agent.type = _expect_str(value, f"agents.{agent_id}.type")
        elif key == "command":
            agent.command = _expect_str(value, f"agents.{agent_id}.command")
        elif key == "args":
            agent.args = _expect_str_list(value, f"agents.{agent_id}.args")
        elif key == "enabled":
            agent.enabled = _expect_bool(value, f"agents.{agent_id}.enabled")
        elif key == "name":
            agent.name = _expect_str(value, f"agents.{agent_id}.name")
        elif key == "env":
            agent.env = _expect_str_dict(value, f"agents.{agent_id}.env")
        elif key == "cwd":
            agent.cwd = _expect_str(value, f"agents.{agent_id}.cwd")
        elif key == "timeout":
            agent.timeout = _expect_int(value, f"agents.{agent_id}.timeout")
        elif key == "options":
            agent.options = _expect_option_config(value, f"agents.{agent_id}.options")
        else:
            raise ConfigError(f"unknown field agents.{agent_id}.{key}")
    return agent


def _merge_mode(existing: Optional[ModeConfig], mode_id: str, values: Mapping[str, Any]) -> ModeConfig:
    mode = ModeConfig(id=mode_id) if existing is None else ModeConfig(id=existing.id, sequence=list(existing.sequence))
    for key, value in values.items():
        if key == "sequence":
            mode.sequence = _expect_str_list(value, f"modes.{mode_id}.sequence")
        else:
            raise ConfigError(f"unknown field modes.{mode_id}.{key}")
    return mode


def validate_config(config: CollaborationConfig) -> None:
    for agent in config.agents.values():
        validate_agent(agent)
    for mode in config.modes.values():
        validate_mode(config, mode.id)


def validate_agent(agent: AgentConfig) -> None:
    if not agent.type:
        raise ConfigError(f"agents.{agent.id}.type is required")
    if agent.type not in AGENT_TYPES:
        raise ConfigError(f"agents.{agent.id}.type must be one of {sorted(AGENT_TYPES)}")
    if agent.enabled and agent.type in SUBPROCESS_AGENT_TYPES and not agent.command:
        raise ConfigError(f"agents.{agent.id}.command is required for type {agent.type!r}")


def validate_mode(config: CollaborationConfig, mode_id: str) -> None:
    mode = config.modes.get(mode_id)
    if mode is None:
        raise ConfigError(f"unknown mode {mode_id!r}")
    if not mode.sequence:
        raise ConfigError(f"modes.{mode_id}.sequence must not be empty")
    for agent_id in mode.sequence:
        agent = config.agents.get(agent_id)
        if agent is None:
            raise ConfigError(f"modes.{mode_id}.sequence references unknown agent {agent_id!r}")
        if not agent.enabled:
            raise ConfigError(f"modes.{mode_id}.sequence references disabled agent {agent_id!r}")


def _load_toml_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # type: ignore
    except ModuleNotFoundError:
        return _parse_toml_subset(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # type: ignore[name-defined]
        raise ConfigError(f"{path}: {exc}") from exc


def _parse_toml_subset(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    current: Dict[str, Any] = root
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                raise ConfigError(f"line {line_number}: empty TOML section")
            current = root
            for part in section.split("."):
                key = part.strip()
                if not key:
                    raise ConfigError(f"line {line_number}: invalid TOML section {section!r}")
                child = current.setdefault(key, {})
                if not isinstance(child, dict):
                    raise ConfigError(f"line {line_number}: section conflicts with value {section!r}")
                current = child
            continue
        if "=" not in line:
            raise ConfigError(f"line {line_number}: expected key = value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"line {line_number}: empty TOML key")
        _set_dotted_key(current, key, _parse_toml_value(raw_value.strip(), line_number), line_number)
    return root


def _set_dotted_key(current: Dict[str, Any], key: str, value: Any, line_number: int) -> None:
    parts = [part.strip() for part in key.split(".")]
    if any(not part for part in parts):
        raise ConfigError(f"line {line_number}: invalid dotted key {key!r}")
    target = current
    for part in parts[:-1]:
        child = target.setdefault(part, {})
        if not isinstance(child, dict):
            raise ConfigError(f"line {line_number}: key conflicts with table {key!r}")
        target = child
    target[parts[-1]] = value


def _strip_comment(line: str) -> str:
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _parse_toml_value(value: str, line_number: int) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(item, line_number) for item in _split_top_level(inner, ",")]
    if value.startswith("{") and value.endswith("}"):
        return _parse_inline_table(value[1:-1], line_number)
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"line {line_number}: invalid TOML string") from exc
        if not isinstance(parsed, str):
            raise ConfigError(f"line {line_number}: invalid TOML string")
        return parsed
    if value.lstrip("-").isdigit():
        return int(value)
    raise ConfigError(f"line {line_number}: unsupported TOML value {value!r}")


def _parse_inline_table(value: str, line_number: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if not value.strip():
        return result
    for item in _split_top_level(value, ","):
        if "=" not in item:
            raise ConfigError(f"line {line_number}: invalid inline table item")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"line {line_number}: empty inline table key")
        result[key] = _parse_toml_value(raw_value.strip(), line_number)
    return result


def _split_top_level(value: str, delimiter: str) -> List[str]:
    items: List[str] = []
    quote: Optional[str] = None
    escaped = False
    start = 0
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == delimiter:
            items.append(value[start:index].strip())
            start = index + 1
    items.append(value[start:].strip())
    return [item for item in items if item]


def _expect_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    return value


def _expect_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _expect_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer")
    return value


def _expect_str_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be an array of strings")
    return list(value)


def _expect_str_dict(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) and isinstance(val, str) for key, val in value.items()):
        raise ConfigError(f"{label} must be a table of string values")
    return dict(value)


def _expect_option_config(value: Any, label: str) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a table")
    result: Dict[str, Dict[str, Any]] = {}
    for option_name, settings in value.items():
        option_label = f"{label}.{option_name}"
        if not isinstance(settings, Mapping):
            raise ConfigError(f"{option_label} must be a table")
        parsed: Dict[str, Any] = {}
        for key, setting in settings.items():
            setting_label = f"{option_label}.{key}"
            if key == "allowed":
                if not isinstance(setting, list) or not all(isinstance(item, (str, bool, int)) for item in setting):
                    raise ConfigError(f"{setting_label} must be an array of strings, booleans, or integers")
                parsed[key] = list(setting)
            elif key in {"min", "max"}:
                parsed[key] = _expect_int(setting, setting_label)
            elif key == "default":
                if not isinstance(setting, (str, bool, int)):
                    raise ConfigError(f"{setting_label} must be a string, boolean, or integer")
                parsed[key] = setting
            else:
                raise ConfigError(f"unknown field {setting_label}")
        result[str(option_name)] = parsed
    return result
