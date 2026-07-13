from __future__ import annotations

from dataclasses import dataclass, field
import ast
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .config_migrations import migrate_config_data
from .paths import AgentCollabHome, atomic_write_private_text, project_config_path, user_config_path

_logger = logging.getLogger("agent_collab.config")


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
    backend_config: Dict[str, Any] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)
    backend: Optional[str] = None

    def options_for(self, backend_id: str) -> Dict[str, Any]:
        """Return config defaults only for the agent's configured backend."""

        return self.options if (self.backend or "cli") == backend_id else {}


@dataclass
class WorkflowConfig:
    id: str
    sequence: List[str] = field(default_factory=list)


@dataclass
class BackendPolicyConfig:
    canonical_backend: str
    enabled: bool = True
    source: str = "user_config"


@dataclass
class SessionsConfig:
    """Daemon-global session retention policy; user config only.

    The defaults live here rather than in ``default_config.toml`` — the same
    shape as ``daemon_token``, which also has no TOML built-in. A TOML
    ``[sessions]`` section only overrides. ``retention_days = 0`` disables
    automatic pruning.
    """

    retention_days: int = 30
    cleanup_interval_hours: int = 24


@dataclass
class WorkdirConfig:
    """Daemon-global workdir confinement policy; user config only."""

    restrict_workdir_roots: List[Path] = field(default_factory=list)


@dataclass
class CollaborationConfig:
    agents: Dict[str, AgentConfig] = field(default_factory=dict)
    workflows: Dict[str, WorkflowConfig] = field(default_factory=dict)
    backends: Dict[str, BackendPolicyConfig] = field(default_factory=dict)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    workdir: WorkdirConfig = field(default_factory=WorkdirConfig)
    daemon_token: Optional[str] = None
    loaded_paths: List[Path] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)


DEFAULT_WORKFLOW = "cross-review"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.toml")


SUBPROCESS_AGENT_TYPES = {"claude", "codex", "antigravity", "xai"}
AGENT_TYPES = SUBPROCESS_AGENT_TYPES | {"mock"}


def builtin_config() -> CollaborationConfig:
    config = CollaborationConfig()
    data = load_toml_file(DEFAULT_CONFIG_PATH)
    merge_config_data(
        config,
        migrate_config_data(data, source=str(DEFAULT_CONFIG_PATH), scope="built_in"),
    )
    return config


def config_search_paths(
    workdir: Path,
    home: Optional[AgentCollabHome] = None,
    env: Optional[Mapping[str, str]] = None,
) -> List[Path]:
    resolved_home = home or AgentCollabHome.resolve(env)
    return [
        project_config_path(workdir),
        user_config_path(resolved_home),
    ]


def load_config(
    workdir: Path,
    home: Optional[AgentCollabHome] = None,
    env: Optional[Mapping[str, str]] = None,
) -> CollaborationConfig:
    """Load built-ins, user config, then the safe subset of project config.

    Project config comes from ``workdir`` (the session workdir), never the
    caller's current shell directory. It may rename globally known agents and
    compose workflows from globally enabled agents, but execution-relevant
    agent fields and daemon-global policy remain user-config-only.
    """

    config = load_user_config(home, env)
    resolved_workdir = workdir.expanduser().resolve()
    project_path = project_config_path(resolved_workdir)
    validate_workdir_allowed(config, resolved_workdir)
    user_path = user_config_path(home or AgentCollabHome.resolve(env))
    if project_path.expanduser().resolve() == user_path.expanduser().resolve():
        # A workdir at (or resolving to) the agent-collab home would re-read
        # the user config as project config and strip its own user-only
        # sections with confusing warnings. It was already loaded; skip it.
        return config
    if project_path.exists():
        project_warnings: List[Dict[str, str]] = []
        merge_config_data(
            config,
            migrate_config_data(
                load_toml_file(project_path),
                source=str(project_path),
                scope="project",
                global_agent_ids=config.agents,
                enabled_global_agent_ids=(
                    agent_id for agent_id, agent in config.agents.items() if agent.enabled
                ),
                warnings=project_warnings,
            ),
        )
        config.warnings.extend(project_warnings)
        config.loaded_paths.append(project_path)

    validate_config(config)
    return config


def load_user_config(
    home: Optional[AgentCollabHome] = None,
    env: Optional[Mapping[str, str]] = None,
) -> CollaborationConfig:
    """Load built-ins plus global user config without selecting a project."""

    config = builtin_config()
    resolved_home = home or AgentCollabHome.resolve(env)
    path = user_config_path(resolved_home)
    if path.exists():
        merge_config_data(
            config,
            migrate_config_data(load_toml_file(path), source=str(path), scope="user"),
        )
        config.loaded_paths.append(path)
    validate_config(config)
    return config


KNOWN_TOP_LEVEL_KEYS = {
    "schema_version",
    "agents",
    "workflows",
    "backends",
    "daemon",
    "sessions",
    "workdir",
}


def merge_config_data(config: CollaborationConfig, data: Mapping[str, Any]) -> None:
    for key in data:
        if key not in KNOWN_TOP_LEVEL_KEYS:
            if key == "modes":
                raise ConfigError("unknown config section 'modes'; use [workflows.*] instead")
            raise ConfigError(f"unknown config section {key!r}")

    agents = data.get("agents", {})
    if agents is not None:
        if not isinstance(agents, Mapping):
            raise ConfigError("[agents] must be a table")
        for agent_id, values in agents.items():
            if not isinstance(values, Mapping):
                raise ConfigError(f"[agents.{agent_id}] must be a table")
            config.agents[str(agent_id)] = _merge_agent(
                config.agents.get(str(agent_id)), str(agent_id), values
            )

    workflows = data.get("workflows", {})
    if workflows is not None:
        if not isinstance(workflows, Mapping):
            raise ConfigError("[workflows] must be a table")
        for workflow_id, values in workflows.items():
            if not isinstance(values, Mapping):
                raise ConfigError(f"[workflows.{workflow_id}] must be a table")
            config.workflows[str(workflow_id)] = _merge_workflow(
                config.workflows.get(str(workflow_id)), str(workflow_id), values
            )

    backend_policies = data.get("backends", {})
    if backend_policies is not None:
        if not isinstance(backend_policies, Mapping):
            raise ConfigError("[backends] must be a table")
        for canonical_name, values in backend_policies.items():
            name = str(canonical_name)
            if not isinstance(values, Mapping):
                raise ConfigError(f"[backends.{name}] must be a table")
            unknown = sorted(set(values) - {"enabled"})
            if unknown:
                raise ConfigError(f"unknown field backends.{name}.{unknown[0]}")
            enabled = _expect_bool(values.get("enabled", True), f"backends.{name}.enabled")
            config.backends[name] = BackendPolicyConfig(name, enabled, "user_config")

    sessions = data.get("sessions", {})
    if sessions is not None and sessions != {}:
        if not isinstance(sessions, Mapping):
            raise ConfigError("[sessions] must be a table")
        unknown = sorted(set(sessions) - {"retention_days", "cleanup_interval_hours"})
        if unknown:
            raise ConfigError(f"unknown field sessions.{unknown[0]}")
        if "retention_days" in sessions:
            retention_days = _expect_int(sessions["retention_days"], "sessions.retention_days")
            if retention_days < 0:
                raise ConfigError("sessions.retention_days must be >= 0 (0 disables pruning)")
            config.sessions.retention_days = retention_days
        if "cleanup_interval_hours" in sessions:
            interval = _expect_int(
                sessions["cleanup_interval_hours"], "sessions.cleanup_interval_hours"
            )
            if interval < 1:
                raise ConfigError("sessions.cleanup_interval_hours must be >= 1")
            config.sessions.cleanup_interval_hours = interval

    workdir = data.get("workdir", {})
    if workdir is not None and workdir != {}:
        if not isinstance(workdir, Mapping):
            raise ConfigError("[workdir] must be a table")
        unknown = sorted(set(workdir) - {"restrict_workdir_roots"})
        if unknown:
            raise ConfigError(f"unknown field workdir.{unknown[0]}")
        if "restrict_workdir_roots" in workdir:
            config.workdir.restrict_workdir_roots = _expect_absolute_path_list(
                workdir["restrict_workdir_roots"], "workdir.restrict_workdir_roots"
            )

    daemon = data.get("daemon", {})
    if daemon is not None and daemon != {}:
        if not isinstance(daemon, Mapping):
            raise ConfigError("[daemon] must be a table")
        unknown = sorted(set(daemon) - {"token"})
        if unknown:
            raise ConfigError(f"unknown field daemon.{unknown[0]}")
        token = _expect_str(daemon.get("token", ""), "daemon.token").strip()
        if not token:
            raise ConfigError("daemon.token must be a non-empty string")
        config.daemon_token = token


def _merge_agent(
    existing: Optional[AgentConfig], agent_id: str, values: Mapping[str, Any]
) -> AgentConfig:
    agent = (
        AgentConfig(id=agent_id, type="")
        if existing is None
        else AgentConfig(
            id=existing.id,
            type=existing.type,
            command=existing.command,
            args=list(existing.args),
            enabled=existing.enabled,
            name=existing.name,
            env=dict(existing.env),
            cwd=existing.cwd,
            timeout=existing.timeout,
            backend_config=dict(existing.backend_config),
            options=dict(existing.options),
            backend=existing.backend,
        )
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
            agent.options = _expect_backend_options(value, f"agents.{agent_id}.options")
        elif key == "backend":
            agent.backend = _expect_str(value, f"agents.{agent_id}.backend")
        else:
            config_name = str(key)
            agent.backend_config[config_name] = _expect_backend_value(
                value, f"agents.{agent_id}.{config_name}"
            )
    return agent


def _merge_workflow(
    existing: Optional[WorkflowConfig], workflow_id: str, values: Mapping[str, Any]
) -> WorkflowConfig:
    workflow = (
        WorkflowConfig(id=workflow_id)
        if existing is None
        else WorkflowConfig(id=existing.id, sequence=list(existing.sequence))
    )
    for key, value in values.items():
        if key == "sequence":
            workflow.sequence = _expect_str_list(value, f"workflows.{workflow_id}.sequence")
        else:
            raise ConfigError(f"unknown field workflows.{workflow_id}.{key}")
    return workflow


def validate_config(config: CollaborationConfig) -> None:
    from .backends import registered_backend_names

    registered = set(registered_backend_names())
    for name in config.backends:
        if name not in registered:
            raise ConfigError(
                f"backends.{name} is not a registered canonical backend; expected one of: "
                + ", ".join(sorted(registered))
            )
    for agent in config.agents.values():
        validate_agent(agent)
    for workflow in config.workflows.values():
        validate_workflow(config, workflow.id)


def backend_policy(config: CollaborationConfig, canonical_backend: str) -> BackendPolicyConfig:
    """Return explicit user policy or the backward-compatible enabled default."""

    return config.backends.get(
        canonical_backend,
        BackendPolicyConfig(canonical_backend, True, "default"),
    )


def validate_workdir_allowed(config: CollaborationConfig, workdir: Path) -> None:
    """Reject a resolved workdir outside the optional user-global allowlist."""

    resolved = workdir.expanduser().resolve()
    roots = config.workdir.restrict_workdir_roots
    if not roots or any(resolved == root or root in resolved.parents for root in roots):
        return
    raise ConfigError(
        f"workdir {resolved} is outside [workdir].restrict_workdir_roots; add that directory "
        "to the user config to allow it"
    )


def resolve_existing_workdir(workdir: Path) -> Path:
    """Resolve and validate a path intended to be used as a process cwd."""

    resolved = workdir.expanduser().resolve()
    if not resolved.exists():
        raise ConfigError(f"workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ConfigError(f"workdir is not a directory: {resolved}")
    return resolved


def render_user_config(token: Optional[str] = None) -> str:
    """Generate a user config with explicit policy for every registered backend."""

    from .backends import registered_backend_names
    from .config_migrations import CURRENT_CONFIG_SCHEMA

    lines = [f"schema_version = {CURRENT_CONFIG_SCHEMA}", ""]
    lines.extend(
        (
            "# Optional workdir confinement. Missing or empty keeps the daemon unrestricted;",
            "# add a broad root or one specific exceptional directory to restrict it.",
            "[workdir]",
            "restrict_workdir_roots = []",
            "",
        )
    )
    for name in registered_backend_names():
        lines.extend((f"[backends.{name}]", "enabled = true", ""))
    if token is not None:
        lines.extend(
            (
                "# The daemon bearer token. This file holds a credential: keep it",
                "# owner-only (chmod 600) and never commit or share it.",
                "[daemon]",
                f'token = "{token}"',
                "",
            )
        )
    return "\n".join(lines)


def _config_is_permissive(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def load_daemon_token(
    home: Optional[AgentCollabHome] = None, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Read ``[daemon].token`` from the user config, or None when unset."""

    config_path = (home or AgentCollabHome.resolve(env)).config_path
    if not config_path.exists():
        return None
    daemon = load_toml_file(config_path).get("daemon")
    if not isinstance(daemon, Mapping):
        return None
    token = daemon.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    if _config_is_permissive(config_path):
        _logger.warning(
            "%s holds the daemon token but is group/world-readable; run: chmod 600 %s",
            config_path,
            config_path,
        )
    return token.strip()


def ensure_daemon_token(
    home: Optional[AgentCollabHome] = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """Return the permanent daemon token, generating and persisting it once.

    The token lives in the user config only. A missing config file is created
    owner-only; an existing file gets a ``[daemon]`` section appended without
    rewriting its content. Generation refuses a group/world-readable file.
    """

    resolved_home = home or AgentCollabHome.resolve(env)
    config_path = resolved_home.config_path
    existing = load_daemon_token(resolved_home)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    if not config_path.exists():
        atomic_write_private_text(config_path, render_user_config(token=token))
        return token
    if _config_is_permissive(config_path):
        raise ConfigError(
            f"refusing to write the daemon token into group/world-readable {config_path}; "
            f"run: chmod 600 {config_path}"
        )
    if "daemon" in load_toml_file(config_path):
        raise ConfigError(
            f"{config_path} has a [daemon] section without a usable token; "
            "set daemon.token to a non-empty string or remove the section"
        )
    text = config_path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    text += f'\n[daemon]\ntoken = "{token}"\n'
    atomic_write_private_text(config_path, text)
    return token


def validate_agent(agent: AgentConfig) -> None:
    if not agent.type:
        raise ConfigError(f"agents.{agent.id}.type is required")
    if agent.type not in AGENT_TYPES:
        raise ConfigError(f"agents.{agent.id}.type must be one of {sorted(AGENT_TYPES)}")

    if agent.type == "mock":
        if agent.backend is not None:
            raise ConfigError(f"agents.{agent.id}.backend is not supported for type 'mock'")
        return

    # A backend must be registered for the agent's type; the command requirement
    # keys off the effective backend (only `cli` runs a subprocess), not the type.
    from .backend_contract import BackendOptionError
    from .backends import DEFAULT_BACKEND, get_backend, registered_backends

    registered = registered_backends(agent.type)
    if agent.backend is not None and agent.backend not in registered:
        raise ConfigError(
            f"agents.{agent.id}.backend {agent.backend!r} is not registered for type "
            f"{agent.type!r}; registered backends: {registered}"
        )
    backend_id = agent.backend or DEFAULT_BACKEND
    backend_impl = get_backend(agent.type, backend_id)
    config_normalizer = getattr(backend_impl, "normalize_config", None)
    if agent.backend_config and not callable(config_normalizer):
        field = sorted(agent.backend_config)[0]
        raise ConfigError(
            f"agents.{agent.id}.{field} is not a configuration field declared by backend {backend_id!r}"
        )
    try:
        if callable(config_normalizer):
            config_normalizer(agent)
        backend_impl.normalize_options(agent, {})
    except BackendOptionError as exc:
        section = "options" if exc.field in agent.options else ""
        path = f"agents.{agent.id}.{section + '.' if section else ''}{exc.field}".rstrip(".")
        raise ConfigError(f"{path}: {exc.message}") from exc
    if agent.enabled and backend_id == "cli" and not agent.command:
        raise ConfigError(f"agents.{agent.id}.command is required for backend 'cli'")


def validate_workflow(config: CollaborationConfig, workflow_id: str) -> None:
    workflow = config.workflows.get(workflow_id)
    if workflow is None:
        raise ConfigError(f"unknown workflow {workflow_id!r}")
    if not workflow.sequence:
        raise ConfigError(f"workflows.{workflow_id}.sequence must not be empty")
    for agent_id in workflow.sequence:
        agent = config.agents.get(agent_id)
        if agent is None:
            raise ConfigError(
                f"workflows.{workflow_id}.sequence references unknown agent {agent_id!r}"
            )
        if not agent.enabled:
            raise ConfigError(
                f"workflows.{workflow_id}.sequence references disabled agent {agent_id!r}"
            )


def load_toml_file(path: Path) -> Dict[str, Any]:
    return load_toml_text(path.read_text(encoding="utf-8"), source=str(path))


def load_toml_text(text: str, source: str = "config") -> Dict[str, Any]:
    try:
        import tomllib  # type: ignore
    except ModuleNotFoundError:
        return _parse_toml_subset(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # type: ignore[name-defined]
        raise ConfigError(f"{source}: {exc}") from exc


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
                    raise ConfigError(
                        f"line {line_number}: section conflicts with value {section!r}"
                    )
                current = child
            continue
        if "=" not in line:
            raise ConfigError(f"line {line_number}: expected key = value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"line {line_number}: empty TOML key")
        _set_dotted_key(
            current, key, _parse_toml_value(raw_value.strip(), line_number), line_number
        )
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


def _expect_absolute_path_list(value: Any, label: str) -> List[Path]:
    paths = _expect_str_list(value, label)
    resolved: List[Path] = []
    for index, raw_path in enumerate(paths):
        if not raw_path.strip():
            raise ConfigError(f"{label}[{index}] must be a non-empty path")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"{label}[{index}] must be absolute (or start with ~)")
        normalized = path.resolve()
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved


def _expect_str_dict(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(val, str) for key, val in value.items()
    ):
        raise ConfigError(f"{label} must be a table of string values")
    return dict(value)


def _expect_backend_value(value: Any, label: str) -> Any:
    if not isinstance(value, (str, bool, int)):
        raise ConfigError(f"{label} must be a string, boolean, or integer")
    return value


def _expect_backend_options(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a table")
    return {
        str(name): _expect_backend_value(option, f"{label}.{name}")
        for name, option in value.items()
    }
