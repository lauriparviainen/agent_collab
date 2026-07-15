from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import time, timedelta
import ast
import logging
import os
import secrets
from pathlib import Path
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config_migrations import ConfigError, migrate_config_data
from .paths import AgentCollabHome, atomic_write_private_text, project_config_path, user_config_path

_logger = logging.getLogger("agent_collab.config")

# ConfigError is defined in config_migrations (so ConfigMigrationError can
# subclass it without a circular import) and re-exported here as the public name.


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
    default_options: Dict[str, Any] = field(default_factory=dict)
    backend: Optional[str] = None

    def options_for(self, backend_id: str) -> Dict[str, Any]:
        """Return config defaults only for the agent's configured backend."""

        return self.options if (self.backend or "cli") == backend_id else {}

    def default_options_for(self, backend_id: str) -> Dict[str, Any]:
        """Return built-in option defaults only for the configured backend.

        These rank below values inferred from ``args`` — see
        ``normalize_declared_options``.
        """

        return self.default_options if (self.backend or "cli") == backend_id else {}


@dataclass
class WorkflowConfig:
    id: str
    sequence: List[str] = field(default_factory=list)
    parallel: Optional[List[str]] = None


def workflow_members(workflow: WorkflowConfig) -> List[str]:
    """Return the workflow's ordered agents for execution and discovery."""

    return list(workflow.parallel if workflow.parallel is not None else workflow.sequence)


def workflow_member_slots(workflow: WorkflowConfig) -> List[str]:
    """Return the workflow's member slots in first-appearance order.

    A slot is named by its configured member id; duplicate sequence positions
    collapse into one slot, so ``[a, b, a]`` keeps slot identity ``a`` (lead,
    reprising) plus ``b`` (reviewer) rather than three free positions.
    """

    return list(dict.fromkeys(workflow_members(workflow)))


@dataclass
class PersonaConfig:
    """A named options-only agent nested under a backend section."""

    id: str
    name: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendPolicyConfig:
    """One `[backends.<canonical>]` section: enablement plus execution config.

    Every enabled backend implicitly defines its default agent (same id as the
    canonical backend name); `agents` holds nested options-only personae.
    """

    canonical_backend: str
    enabled: bool = True
    source: str = "user_config"
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    name: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    timeout: Optional[int] = None
    backend_config: Dict[str, Any] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)
    # Shipped option defaults from the built-in config; kept separate from
    # user `options` so a user options table never silently drops them.
    default_options: Dict[str, Any] = field(default_factory=dict)
    agents: Dict[str, PersonaConfig] = field(default_factory=dict)


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
class WorkTimeConfig:
    start: time = time(9, 0)
    end: time = time(17, 0)


@dataclass
class SystemConfig:
    timezone: str = "local"


@dataclass
class UsageWindowTargetConfig:
    id: str
    enabled: bool = False
    backend: str = ""
    model: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    days: Optional[List[str]] = None
    work_time: Optional[WorkTimeConfig] = None
    interval: Optional[timedelta] = None
    jitter: Optional[timedelta] = None


@dataclass
class UsageWindowsConfig:
    days: List[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    work_time: WorkTimeConfig = field(default_factory=WorkTimeConfig)
    interval: timedelta = timedelta(hours=5)
    jitter: timedelta = timedelta(minutes=5)
    targets: Dict[str, UsageWindowTargetConfig] = field(default_factory=dict)


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
    system: SystemConfig = field(default_factory=SystemConfig)
    usage_windows: UsageWindowsConfig = field(default_factory=UsageWindowsConfig)
    workdir: WorkdirConfig = field(default_factory=WorkdirConfig)
    daemon_token: Optional[str] = None
    loaded_paths: List[Path] = field(default_factory=list)
    warnings: List[Dict[str, str]] = field(default_factory=list)
    # Display-name overrides for derived agents (project config may rename).
    agent_names: Dict[str, str] = field(default_factory=dict)


DEFAULT_WORKFLOW = "cross-review"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.toml")


SUBPROCESS_AGENT_TYPES = {"claude", "codex", "antigravity", "xai"}
AGENT_TYPES = SUBPROCESS_AGENT_TYPES | {"mock"}
MAX_PARALLEL_WORKFLOW_WIDTH = 4


def builtin_config() -> CollaborationConfig:
    config = CollaborationConfig()
    data = load_toml_file(DEFAULT_CONFIG_PATH)
    merge_config_data(
        config,
        migrate_config_data(data, source=str(DEFAULT_CONFIG_PATH), scope="built_in"),
        scope="built_in",
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
    "system",
    "usage_windows",
}


def merge_config_data(
    config: CollaborationConfig, data: Mapping[str, Any], *, scope: str = "user_config"
) -> None:
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
            # Display names are the one surviving agent-level override; the
            # execution schema is backend-first (`[backends.<canonical>]`).
            extra = sorted(set(str(key) for key in values) - {"name"})
            if extra:
                raise ConfigError(
                    f"agents.{agent_id}.{extra[0]}: top-level [agents.*] sections are no "
                    "longer supported; configure [backends.<canonical>] instead and re-run "
                    "./agent_collab.sh install to migrate an old config"
                )
            if "name" in values:
                config.agent_names[str(agent_id)] = _expect_str(
                    values["name"], f"agents.{agent_id}.name"
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

    backend_sections = data.get("backends", {})
    if backend_sections is not None:
        if not isinstance(backend_sections, Mapping):
            raise ConfigError("[backends] must be a table")
        for canonical_name, values in backend_sections.items():
            name = str(canonical_name)
            if not isinstance(values, Mapping):
                raise ConfigError(f"[backends.{name}] must be a table")
            config.backends[name] = _merge_backend_section(
                config.backends.get(name), name, values, scope=scope
            )

    # Rebuilding only when backend sections or display names changed keeps
    # programmatically constructed agent dicts (tests, tooling) intact across
    # workflow-only merges.
    if "backends" in data or "agents" in data:
        _rebuild_derived_agents(config)

    system = data.get("system", {})
    if system is not None and system != {}:
        if not isinstance(system, Mapping):
            raise ConfigError("[system] must be a table")
        unknown = sorted(set(system) - {"timezone"})
        if unknown:
            raise ConfigError(f"unknown field system.{unknown[0]}")
        if "timezone" in system:
            config.system.timezone = _expect_str(system["timezone"], "system.timezone")

    usage_windows = data.get("usage_windows", {})
    if usage_windows is not None and usage_windows != {}:
        _merge_usage_windows(config.usage_windows, usage_windows)

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


def _merge_backend_section(
    existing: Optional[BackendPolicyConfig],
    canonical: str,
    values: Mapping[str, Any],
    *,
    scope: str = "user_config",
) -> BackendPolicyConfig:
    section = (
        BackendPolicyConfig(canonical_backend=canonical)
        if existing is None
        else BackendPolicyConfig(
            canonical_backend=existing.canonical_backend,
            enabled=existing.enabled,
            source=existing.source,
            command=existing.command,
            args=list(existing.args),
            name=existing.name,
            env=dict(existing.env),
            cwd=existing.cwd,
            timeout=existing.timeout,
            backend_config=dict(existing.backend_config),
            options=dict(existing.options),
            default_options=dict(existing.default_options),
            agents=dict(existing.agents),
        )
    )
    for key, value in values.items():
        label = f"backends.{canonical}.{key}"
        if key == "enabled":
            section.enabled = _expect_bool(value, label)
        elif key == "command":
            section.command = _expect_str(value, label)
        elif key == "args":
            section.args = _expect_str_list(value, label)
        elif key == "name":
            section.name = _expect_str(value, label)
        elif key == "env":
            section.env = _expect_str_dict(value, label)
        elif key == "cwd":
            section.cwd = _expect_str(value, label)
        elif key == "timeout":
            section.timeout = _expect_int(value, label)
        elif key == "options" and scope == "built_in":
            section.default_options = _expect_backend_options(value, label)
        elif key == "options":
            section.options = _expect_backend_options(value, label)
        elif key == "agents":
            if not isinstance(value, Mapping):
                raise ConfigError(f"{label} must be a table")
            for persona_name, persona_values in value.items():
                section.agents[str(persona_name)] = _merge_persona(
                    section.agents.get(str(persona_name)),
                    canonical,
                    str(persona_name),
                    persona_values,
                )
        else:
            section.backend_config[str(key)] = _expect_backend_value(value, label)
    return section


_TARGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLOCK = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_usage_duration(value: Any, label: str, *, jitter: bool) -> timedelta:
    from .durations import parse_whole_duration

    units = {"m": timedelta(minutes=1)}
    if not jitter:
        units["h"] = timedelta(hours=1)
    try:
        return parse_whole_duration(
            value,
            units=units,
            allow_zero=jitter,
            example="0m" if jitter else "5h",
        )
    except ValueError as exc:
        raise ConfigError(f"{label}: {exc}") from exc


def _parse_clock(value: Any, label: str) -> time:
    text = _expect_str(value, label)
    if _CLOCK.fullmatch(text) is None:
        raise ConfigError(f"{label} must use zero-padded HH:MM")
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _parse_work_time(value: Any, label: str) -> WorkTimeConfig:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a table with start and end")
    unknown = sorted(set(value) - {"start", "end"})
    if unknown:
        raise ConfigError(f"unknown field {label}.{unknown[0]}")
    if "start" not in value or "end" not in value:
        missing = "start" if "start" not in value else "end"
        raise ConfigError(f"{label}.{missing} is required")
    result = WorkTimeConfig(
        start=_parse_clock(value["start"], f"{label}.start"),
        end=_parse_clock(value["end"], f"{label}.end"),
    )
    if result.start == result.end:
        raise ConfigError(f"{label}.start and {label}.end must differ")
    return result


def _parse_days(value: Any, label: str) -> List[str]:
    days = _expect_str_list(value, label)
    if not days:
        raise ConfigError(f"{label} must be a non-empty array")
    if len(set(days)) != len(days):
        raise ConfigError(f"{label} must contain unique day names")
    invalid = next((item for item in days if item not in _DAYS), None)
    if invalid is not None:
        raise ConfigError(f"{label} contains invalid day {invalid!r}; expected mon through sun")
    return days


def _merge_usage_windows(config: UsageWindowsConfig, values: Any) -> None:
    if not isinstance(values, Mapping):
        raise ConfigError("[usage_windows] must be a table")
    unknown = sorted(set(values) - {"days", "work_time", "interval", "jitter", "targets"})
    if unknown:
        raise ConfigError(f"unknown field usage_windows.{unknown[0]}")
    if "days" in values:
        config.days = _parse_days(values["days"], "usage_windows.days")
    if "work_time" in values:
        config.work_time = _parse_work_time(values["work_time"], "usage_windows.work_time")
    if "interval" in values:
        config.interval = _parse_usage_duration(
            values["interval"], "usage_windows.interval", jitter=False
        )
    if "jitter" in values:
        config.jitter = _parse_usage_duration(values["jitter"], "usage_windows.jitter", jitter=True)
    targets = values.get("targets")
    if targets is None:
        return
    if not isinstance(targets, Mapping):
        raise ConfigError("[usage_windows.targets] must be a table")
    for raw_id, raw_values in targets.items():
        target_id = str(raw_id)
        label = f"usage_windows.targets.{target_id}"
        if not isinstance(raw_values, Mapping):
            raise ConfigError(f"[{label}] must be a table")
        unknown = sorted(
            set(raw_values)
            - {"enabled", "backend", "model", "options", "days", "work_time", "interval", "jitter"}
        )
        if unknown:
            raise ConfigError(f"unknown field {label}.{unknown[0]}")
        existing = config.targets.get(target_id)
        target = (
            UsageWindowTargetConfig(id=target_id)
            if existing is None
            else UsageWindowTargetConfig(
                id=existing.id,
                enabled=existing.enabled,
                backend=existing.backend,
                model=existing.model,
                options=dict(existing.options),
                days=None if existing.days is None else list(existing.days),
                work_time=existing.work_time,
                interval=existing.interval,
                jitter=existing.jitter,
            )
        )
        if "enabled" in raw_values:
            target.enabled = _expect_bool(raw_values["enabled"], f"{label}.enabled")
        if "backend" in raw_values:
            target.backend = _expect_str(raw_values["backend"], f"{label}.backend")
        if "model" in raw_values:
            target.model = _expect_str(raw_values["model"], f"{label}.model")
        if "options" in raw_values:
            options = _expect_backend_options(raw_values["options"], f"{label}.options")
            if "model" in options:
                raise ConfigError(f"{label}.options.model is not allowed; use {label}.model")
            target.options.update(options)
        if "days" in raw_values:
            target.days = _parse_days(raw_values["days"], f"{label}.days")
        if "work_time" in raw_values:
            target.work_time = _parse_work_time(raw_values["work_time"], f"{label}.work_time")
        if "interval" in raw_values:
            target.interval = _parse_usage_duration(
                raw_values["interval"], f"{label}.interval", jitter=False
            )
        if "jitter" in raw_values:
            target.jitter = _parse_usage_duration(
                raw_values["jitter"], f"{label}.jitter", jitter=True
            )
        config.targets[target_id] = target


def effective_usage_window_schedule(
    config: CollaborationConfig, target: UsageWindowTargetConfig
) -> tuple[List[str], WorkTimeConfig, timedelta, timedelta]:
    """Return target schedule fields after applying global defaults."""

    usage = config.usage_windows
    return (
        list(target.days if target.days is not None else usage.days),
        target.work_time or usage.work_time,
        target.interval or usage.interval,
        target.jitter if target.jitter is not None else usage.jitter,
    )


def normalized_usage_window_options(
    config: CollaborationConfig, target: UsageWindowTargetConfig
) -> Dict[str, Any]:
    """Normalize one target through its backend-owned option contract."""

    from . import backends as backend_registry
    from .backend_contract import BackendOptionError

    agent_type, backend_id = split_canonical_backend(target.backend)
    backend = backend_registry.get_backend(agent_type, backend_id or "")
    section = config.backends[target.backend]
    agent = config.agents.get(target.backend) or AgentConfig(
        id=target.backend,
        type=agent_type,
        command=section.command,
        args=list(section.args),
        enabled=True,
        env=dict(section.env),
        cwd=section.cwd,
        timeout=section.timeout,
        backend_config=dict(section.backend_config),
        options=dict(section.options),
        default_options=dict(section.default_options),
        backend=backend_id,
    )
    try:
        return dict(backend.normalize_options(agent, {**target.options, "model": target.model}))
    except BackendOptionError as exc:
        if exc.field == "model":
            path = f"usage_windows.targets.{target.id}.model"
        else:
            field = f".{exc.field}" if exc.field else ""
            path = f"usage_windows.targets.{target.id}.options{field}"
        raise ConfigError(f"{path}: {exc.message}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"usage_windows.targets.{target.id}.options: {exc}") from exc


def _validate_usage_windows(config: CollaborationConfig) -> None:
    timezone_name = config.system.timezone
    if timezone_name != "local":
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                "system.timezone must be 'local' or a valid IANA timezone name"
            ) from exc
    if config.usage_windows.interval < timedelta(minutes=15):
        raise ConfigError("usage_windows.interval must be at least 15m")
    if config.usage_windows.jitter >= config.usage_windows.interval:
        raise ConfigError("usage_windows.jitter must be smaller than usage_windows.interval")
    enabled_pairs: Dict[tuple[str, str], str] = {}
    from .backends import registered_backend_names

    registered = set(registered_backend_names())
    for target in config.usage_windows.targets.values():
        label = f"usage_windows.targets.{target.id}"
        if _TARGET_ID.fullmatch(target.id) is None:
            raise ConfigError(
                f"{label}: target name must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$"
            )
        if not target.backend:
            raise ConfigError(f"{label}.backend is required")
        if target.backend not in registered:
            raise ConfigError(
                f"{label}.backend is not a registered canonical backend; expected one of: "
                + ", ".join(sorted(registered))
            )
        if not target.model.strip():
            raise ConfigError(f"{label}.model must be a non-empty string")
        _days, _work_time, interval, jitter = effective_usage_window_schedule(config, target)
        if interval < timedelta(minutes=15):
            raise ConfigError(f"{label}.interval must be at least 15m")
        if jitter >= interval:
            raise ConfigError(f"{label}.jitter must be smaller than its effective interval")
        normalized = normalized_usage_window_options(config, target)
        normalized_model = normalized.get("model")
        if not isinstance(normalized_model, str) or not normalized_model.strip():
            raise ConfigError(f"{label}.model must normalize to a non-empty string")
        if target.enabled:
            pair = (target.backend, normalized_model)
            previous = enabled_pairs.get(pair)
            if previous is not None:
                raise ConfigError(
                    f"{label} duplicates enabled target {previous!r} for backend/model "
                    f"{target.backend}/{normalized_model}"
                )
            enabled_pairs[pair] = target.id


def _merge_persona(
    existing: Optional[PersonaConfig],
    canonical: str,
    persona_id: str,
    values: Any,
) -> PersonaConfig:
    label = f"backends.{canonical}.agents.{persona_id}"
    if not isinstance(values, Mapping):
        raise ConfigError(f"[{label}] must be a table")
    persona = (
        PersonaConfig(id=persona_id)
        if existing is None
        else PersonaConfig(id=existing.id, name=existing.name, options=dict(existing.options))
    )
    for key, value in values.items():
        if key == "name":
            persona.name = _expect_str(value, f"{label}.name")
        elif key == "options":
            persona.options = _expect_backend_options(value, f"{label}.options")
        else:
            # Personae differ by options only; execution settings live on the
            # backend section so a persona can never change what runs.
            raise ConfigError(f"{label}.{key}: nested agents may set only 'name' and 'options'")
    return persona


def split_canonical_backend(canonical: str) -> tuple[str, Optional[str]]:
    """Split a canonical backend name into (agent type, backend id)."""

    if canonical == "mock":
        return "mock", None
    if "_" in canonical:
        agent_type, backend_id = canonical.rsplit("_", 1)
        if agent_type and backend_id:
            return agent_type, backend_id
    raise ConfigError(
        f"backends.{canonical} is not a canonical backend name (expected <type>_<backend>)"
    )


def _rebuild_derived_agents(config: CollaborationConfig) -> None:
    """Derive the runtime agents from the backend sections.

    Every enabled backend defines its default agent under the canonical name;
    nested personae derive `<canonical>.<persona>` agents that inherit the
    backend's execution settings and override options only.
    """

    agents: Dict[str, AgentConfig] = {}
    for canonical, section in config.backends.items():
        if not section.enabled:
            continue
        agent_type, backend_id = split_canonical_backend(canonical)
        agents[canonical] = AgentConfig(
            id=canonical,
            type=agent_type,
            command=section.command,
            args=list(section.args),
            enabled=True,
            name=config.agent_names.get(canonical, section.name),
            env=dict(section.env),
            cwd=section.cwd,
            timeout=section.timeout,
            backend_config=dict(section.backend_config),
            options=dict(section.options),
            default_options=dict(section.default_options),
            backend=backend_id,
        )
        for persona in section.agents.values():
            derived_id = f"{canonical}.{persona.id}"
            agents[derived_id] = AgentConfig(
                id=derived_id,
                type=agent_type,
                command=section.command,
                args=list(section.args),
                enabled=True,
                name=config.agent_names.get(derived_id, persona.name),
                env=dict(section.env),
                cwd=section.cwd,
                timeout=section.timeout,
                backend_config=dict(section.backend_config),
                options={**section.options, **persona.options},
                default_options=dict(section.default_options),
                backend=backend_id,
            )
    config.agents = agents


def workflow_member_state(config: CollaborationConfig, member_id: str) -> str:
    """Classify a workflow member reference: 'ok', 'disabled', or 'unknown'."""

    agent = config.agents.get(member_id)
    if agent is not None:
        return "ok" if agent.enabled else "disabled"
    canonical, _, persona = member_id.partition(".")
    section = config.backends.get(canonical)
    if section is None:
        return "unknown"
    if persona and persona not in section.agents:
        return "unknown"
    return "disabled"


def _merge_workflow(
    existing: Optional[WorkflowConfig], workflow_id: str, values: Mapping[str, Any]
) -> WorkflowConfig:
    if "sequence" in values and "parallel" in values:
        raise ConfigError(
            f"workflows.{workflow_id} must define exactly one of sequence or parallel"
        )
    workflow = (
        WorkflowConfig(id=workflow_id)
        if existing is None
        else WorkflowConfig(
            id=existing.id,
            sequence=list(existing.sequence),
            parallel=None if existing.parallel is None else list(existing.parallel),
        )
    )
    for key, value in values.items():
        if key == "sequence":
            workflow.sequence = _expect_str_list(value, f"workflows.{workflow_id}.sequence")
            workflow.parallel = None
        elif key == "parallel":
            workflow.parallel = _expect_str_list(value, f"workflows.{workflow_id}.parallel")
            workflow.sequence = []
        else:
            raise ConfigError(f"unknown field workflows.{workflow_id}.{key}")
    return workflow


def validate_config(config: CollaborationConfig) -> None:
    from .backends import registered_backend_names

    registered = set(registered_backend_names()) | {"mock"}
    for name in config.backends:
        if name not in registered:
            raise ConfigError(
                f"backends.{name} is not a registered canonical backend; expected one of: "
                + ", ".join(sorted(registered))
            )
    for agent in config.agents.values():
        validate_agent(agent)
    # A workflow whose members reference a disabled backend stays loadable and
    # becomes start-ineligible; only unknown references are configuration errors.
    for workflow in config.workflows.values():
        validate_workflow(config, workflow.id, allow_disabled=True)
    _validate_usage_windows(config)


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
    """Generate a user config with explicit enablement for every registered backend.

    Enablement mirrors the built-in defaults so a freshly generated file
    changes nothing: an enabled backend defines its default agent.
    """

    from .backends import registered_backend_names
    from .config_migrations import CURRENT_CONFIG_SCHEMA

    builtin_enabled = {
        name for name, section in builtin_config().backends.items() if section.enabled
    }
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
        enabled = "true" if name in builtin_enabled else "false"
        lines.extend((f"[backends.{name}]", f"enabled = {enabled}", ""))
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

    Creation is serialized with an inter-process lock so concurrent callers
    (two ``daemon token`` runs, or one racing daemon start) converge on a
    single persisted token instead of each generating and writing a different
    one — a last-writer-wins race would otherwise hand a caller a token that
    never survived on disk.
    """

    resolved_home = home or AgentCollabHome.resolve(env)
    existing = load_daemon_token(resolved_home)
    if existing:
        return existing
    with _daemon_token_lock(resolved_home):
        # Re-check under the lock: a concurrent creator may have won the race
        # while we waited, in which case we return its token untouched.
        existing = load_daemon_token(resolved_home)
        if existing:
            return existing
        return _generate_daemon_token(resolved_home)


def _generate_daemon_token(home: AgentCollabHome) -> str:
    """Generate and persist a fresh daemon token. Call under the token lock."""

    # Operate on the symlink target so a dotfile-managed config keeps its link:
    # atomic_write_private_text os.replaces the path, which would sever a
    # symlink at config_path itself. Resolve before the existence check so a
    # dangling link (dotfiles cloned, target not written yet) creates the
    # target instead of replacing the link with a regular file. This mirrors
    # the config migration writer.
    config_path = home.config_path.resolve()
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


@contextmanager
def _daemon_token_lock(home: AgentCollabHome) -> Iterator[None]:
    """Exclusive, blocking lock serializing daemon-token creation for one home.

    Best-effort on platforms without ``fcntl`` (the project targets Unix): the
    lock is skipped there and creation falls back to the prior behavior.
    """

    try:
        import fcntl
    except ImportError:
        yield
        return
    home.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(home.root / "config-token.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _config_path(agent_id: str) -> str:
    """Render a derived agent id as its backend-first TOML path fragment."""

    canonical, _, persona = agent_id.partition(".")
    return f"{canonical}.agents.{persona}" if persona else canonical


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
            f"backends.{_config_path(agent.id)} selects backend {agent.backend!r} which is not "
            f"registered for type {agent.type!r}; registered backends: {registered}"
        )
    backend_id = agent.backend or DEFAULT_BACKEND
    backend_impl = get_backend(agent.type, backend_id)
    config_normalizer = getattr(backend_impl, "normalize_config", None)
    if agent.backend_config and not callable(config_normalizer):
        field = sorted(agent.backend_config)[0]
        raise ConfigError(
            f"backends.{_config_path(agent.id)}.{field} is not a configuration field declared "
            f"by backend {backend_id!r}"
        )
    try:
        if callable(config_normalizer):
            config_normalizer(agent)
        backend_impl.normalize_options(agent, {})
    except BackendOptionError as exc:
        section = "options" if exc.field in agent.options else ""
        path = (
            f"backends.{_config_path(agent.id)}.{section + '.' if section else ''}{exc.field}"
        ).rstrip(".")
        raise ConfigError(f"{path}: {exc.message}") from exc
    if agent.enabled and backend_id == "cli" and not agent.command:
        canonical = f"{agent.type}_{backend_id}"
        raise ConfigError(f"backends.{canonical}.command is required for backend 'cli'")


def validate_workflow(
    config: CollaborationConfig, workflow_id: str, *, allow_disabled: bool = False
) -> None:
    workflow = config.workflows.get(workflow_id)
    if workflow is None:
        raise ConfigError(f"unknown workflow {workflow_id!r}")
    if workflow.parallel is not None:
        kind = "parallel"
        if workflow.sequence:
            raise ConfigError(
                f"workflows.{workflow_id} must define exactly one of sequence or parallel"
            )
        if not workflow.parallel:
            raise ConfigError(f"workflows.{workflow_id}.parallel must not be empty")
        if len(workflow.parallel) == 1:
            raise ConfigError(
                f"workflows.{workflow_id}.parallel must contain at least two agents; "
                "use sequence for a single agent"
            )
        if len(set(workflow.parallel)) != len(workflow.parallel):
            raise ConfigError(f"workflows.{workflow_id}.parallel must not contain duplicate agents")
        if len(workflow.parallel) > MAX_PARALLEL_WORKFLOW_WIDTH:
            raise ConfigError(
                f"workflows.{workflow_id}.parallel exceeds the maximum width "
                f"of {MAX_PARALLEL_WORKFLOW_WIDTH}"
            )
        members = workflow.parallel
    else:
        kind = "sequence"
        if not workflow.sequence:
            raise ConfigError(f"workflows.{workflow_id}.sequence must not be empty")
        members = workflow.sequence
    for agent_id in members:
        state = workflow_member_state(config, agent_id)
        if state == "unknown":
            raise ConfigError(
                f"workflows.{workflow_id}.{kind} references unknown agent {agent_id!r}"
            )
        if state == "disabled" and not allow_disabled:
            raise ConfigError(
                f"workflows.{workflow_id}.{kind} references agent {agent_id!r} "
                "of a disabled backend"
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
            for part in _split_section_parts(section, line_number):
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


def _split_section_parts(section: str, line_number: int) -> List[str]:
    """Split a TOML section header on dots outside quotes, unquoting parts.

    Quoted parts (`[agents."codex_cli.readonly"]`) carry derived persona ids
    that contain a literal dot; the fallback parser must not split inside
    them.
    """

    parts: List[str] = []
    for part in _split_top_level(section, "."):
        item = part.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in {"'", '"'}:
            item = item[1:-1]
        if not item:
            raise ConfigError(f"line {line_number}: invalid TOML section {section!r}")
        parts.append(item)
    if not parts:
        raise ConfigError(f"line {line_number}: invalid TOML section {section!r}")
    return parts


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
