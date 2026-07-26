"""Grok CLI ownership, configuration, state, and native-profile facts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import stat
from typing import Any, Iterable, Mapping, Sequence, Tuple

from ...config import load_toml_file
from ...sandbox.paths import component_contains
from ...sandbox.plan import ResolvedSandboxPlan
from ...sandbox.specs import (
    BackendSandboxSpec,
    CompatibilityCheck,
    CreationPolicy,
    EnvironmentSpec,
    NativeSandboxProfile,
    PathAccess,
    Persistence,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
    StateRootSpec,
)

SYSTEM_MANAGED_CONFIG_ROOT = Path("/etc/grok")
LEGACY_MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PATH_ARGUMENTS = ("--agent", "--cwd")
_WRITE_PATH_ENVIRONMENT = ("GROK_LOG_FILE", "GROK_COPY_FILE")
_MANAGED_NAMES = ("managed_config.toml", "requirements.toml")
_PROJECT_EXTENSION_NAMES = (
    "config.toml",
    "sandbox.toml",
    "lsp.json",
    "skills",
    "plugins",
    "agents",
    "hooks",
    "workflows",
)
_EXTERNAL_SERVICE_SUMMARY = (
    "Configured remote model, MCP, and authentication services "
    "(external services outside filesystem boundary)",
)


@dataclass(frozen=True)
class XaiCliSandboxAdapter:
    """Describe the complete Grok process tree and writable state."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        home = _effective_home(context)
        configured = context.inherited_environment.get("GROK_HOME")
        state = Path(configured).expanduser() if configured is not None else home / ".grok"

        compatibility = [
            CompatibilityCheck(
                "state_lookup",
                lambda: _validate_state_contract(state, home),
            ),
            CompatibilityCheck(
                "managed_configuration",
                lambda: _validate_managed_configuration(state),
            ),
            CompatibilityCheck(
                "configuration",
                lambda: _validate_configuration(context, state, home),
            ),
        ]
        if context.command_preview:
            compatibility.append(
                CompatibilityCheck(
                    "provider_command",
                    lambda: _prepare_read_only_command(context.command_preview),
                )
            )

        return BackendSandboxSpec(
            support=SandboxSupport.DIRECT_PROCESS,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=(
                StateRootSpec(
                    label="Grok state",
                    destination=state,
                    access=PathAccess.WRITABLE,
                    persistence=Persistence.HOST,
                    creation=CreationPolicy.MUST_EXIST,
                ),
            ),
            provider_visible_paths=tuple(_provider_visible_paths(context, state)),
            environment=EnvironmentSpec(
                set_values={"GROK_HOME": str(state)},
                unset_names=("GROK_SANDBOX", "GROK_SANDBOX_PROFILE"),
                secret_names=("XAI_API_KEY",),
            ),
            native_profile=NativeSandboxProfile(
                summary={
                    "permissions": "bypassed_after_outer_ack",
                    "native_sandbox": "disabled_after_outer_ack",
                    "state": "complete_grok_home_persistent_writable",
                    "authentication": (
                        "cached_state_or_environment_key; configured external providers "
                        "compatibility_checked"
                    ),
                    "managed_configuration": "incompatible",
                    "extensions": "contained_or_rejected_before_execution",
                    "legacy_shared_tmp": "warning_only_in_common_private_scratch",
                },
                command=(
                    "--permission-mode",
                    "bypassPermissions",
                    "--sandbox",
                    "off",
                ),
            ),
            compatibility=tuple(compatibility),
            external_services=_EXTERNAL_SERVICE_SUMMARY,
        )

    def prepare_inner(
        self,
        plan: ResolvedSandboxPlan,
        command: Sequence[str],
    ) -> Tuple[str, ...]:
        if plan.policy.effective is SandboxPolicy.NONE:
            return tuple(command)
        return _prepare_read_only_command(command)


def _effective_home(context: SandboxContext) -> Path:
    raw = context.inherited_environment.get("HOME")
    return Path(raw).expanduser() if raw else Path.home()


def _validate_state_contract(state: Path, home: Path) -> None:
    if not state.is_absolute():
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "GROK_HOME must be an absolute path for outer read-only",
            remediation=(
                "Set GROK_HOME to the absolute complete Grok state directory or unset it "
                "to use the absolute ~/.grok default.",
            ),
        )
    if home.is_absolute() and component_contains(state, home):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "GROK_HOME may not make the effective user home or its ancestor writable",
            remediation=("Select the complete Grok state directory itself, normally ~/.grok.",),
        )


def _validate_managed_configuration(state: Path) -> None:
    candidates = (
        *(state / name for name in _MANAGED_NAMES),
        *(SYSTEM_MANAGED_CONFIG_ROOT / name for name in _MANAGED_NAMES),
        LEGACY_MANAGED_SETTINGS,
    )
    if any(os.path.lexists(path) for path in candidates):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "Grok managed or requirements configuration is incompatible with outer read-only",
            remediation=(
                "Use an installation without managed Grok configuration or select sandbox='none'.",
            ),
        )


def _provider_visible_paths(
    context: SandboxContext,
    state: Path,
) -> Iterable[StateRootSpec]:
    seen: set[Path] = set()
    for flag, raw in _lax_option_values(context.command_preview, _PATH_ARGUMENTS):
        candidate = _argument_path(
            raw,
            context.cwd,
            bare_is_path=flag == "--cwd",
        )
        if candidate is None or component_contains(state, candidate):
            continue
        declaration = (
            candidate if _looks_like_directory_argument(raw, candidate) else candidate.parent
        )
        if declaration in seen:
            continue
        seen.add(declaration)
        yield StateRootSpec(
            label="Grok provider-visible path",
            destination=declaration,
            access=PathAccess.READ_ONLY,
            persistence=Persistence.HOST,
            creation=CreationPolicy.MUST_EXIST,
        )
    raw_agent = context.inherited_environment.get("GROK_AGENT")
    if raw_agent:
        candidate = _argument_path(raw_agent, context.cwd)
        if (
            candidate is not None
            and not component_contains(state, candidate)
            and candidate.parent not in seen
        ):
            yield StateRootSpec(
                label="Grok provider-visible path",
                destination=candidate.parent,
                access=PathAccess.READ_ONLY,
                persistence=Persistence.HOST,
                creation=CreationPolicy.MUST_EXIST,
            )


def _looks_like_directory_argument(raw: str, candidate: Path) -> bool:
    return raw.endswith(os.sep) or candidate.is_dir()


def _argument_path(raw: str, cwd: Path, *, bare_is_path: bool = False) -> Path | None:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if not bare_is_path and os.sep not in raw and not raw.startswith("."):
            return None
        candidate = cwd / candidate
    return candidate.absolute()


def _lax_option_values(
    command: Sequence[str],
    flags: Sequence[str],
) -> Tuple[Tuple[str, str], ...]:
    values: list[Tuple[str, str]] = []
    wanted = set(flags)
    index = 1
    while index < len(command):
        item = command[index]
        matched = next((flag for flag in flags if item.startswith(f"{flag}=")), None)
        if matched is not None:
            value = item[len(matched) + 1 :]
            if value:
                values.append((matched, value))
            index += 1
            continue
        if item in wanted and index + 1 < len(command):
            value = command[index + 1]
            if value and not value.startswith("-"):
                values.append((item, value))
                index += 2
                continue
        index += 1
    return tuple(values)


def _validate_configuration(context: SandboxContext, state: Path, home: Path) -> None:
    if not home.is_absolute():
        _configuration_incompatible("Grok HOME is relative")

    _validate_command_paths(context, state)
    for name in _WRITE_PATH_ENVIRONMENT:
        value = context.inherited_environment.get(name)
        if value:
            _require_writable_state_path(value, context.cwd, state)

    external_auth = context.inherited_environment.get("GROK_AUTH_PROVIDER_COMMAND")
    if external_auth:
        _external_auth_incompatible()

    raw_agent = context.inherited_environment.get("GROK_AGENT")
    if raw_agent:
        _validate_declared_path(raw_agent, context.cwd)

    execution_cwd = _effective_command_cwd(context)
    configs = [state / "config.toml", *_project_config_paths(context)]
    for path in configs:
        data = _load_optional_toml(path)
        if data is not None:
            _validate_config_data(data, context, state, execution_cwd)
    for path in _project_mcp_paths(context):
        data = _load_optional_json(path)
        if data is not None:
            _validate_project_mcp_data(data, context, state, execution_cwd)

    _validate_project_extension_paths(context)
    _validate_ambient_compatibility(context, home, configs)


def _validate_command_paths(context: SandboxContext, state: Path) -> None:
    for flag in _PATH_ARGUMENTS:
        for raw in _strict_option_values(context.command_preview, flag):
            _validate_declared_path(
                raw,
                context.cwd,
                bare_is_path=flag == "--cwd",
            )
    for raw in _strict_option_values(context.command_preview, "--debug-file"):
        _require_writable_state_path(raw, context.cwd, state)


def _strict_option_values(command: Sequence[str], flag: str) -> Tuple[str, ...]:
    values: list[str] = []
    prefix = f"{flag}="
    index = 1
    while index < len(command):
        item = command[index]
        if item.startswith(prefix):
            value = item[len(prefix) :]
            if not value:
                _command_incompatible()
            values.append(value)
            index += 1
            continue
        if item != flag:
            index += 1
            continue
        if index + 1 >= len(command) or command[index + 1].startswith("-"):
            _command_incompatible()
        values.append(command[index + 1])
        index += 2
    return tuple(values)


def _project_config_paths(context: SandboxContext) -> Tuple[Path, ...]:
    return _project_file_paths(context, Path(".grok") / "config.toml")


def _project_mcp_paths(context: SandboxContext) -> Tuple[Path, ...]:
    return _project_file_paths(context, Path(".mcp.json"))


def _project_file_paths(context: SandboxContext, relative: Path) -> Tuple[Path, ...]:
    return tuple(directory / relative for directory in _project_directories(context))


def _project_directories(context: SandboxContext) -> Tuple[Path, ...]:
    directories: list[Path] = []
    for root in _project_roots(context):
        current = root
        boundary = _project_boundary(context, root)
        while True:
            if current not in directories:
                directories.append(current)
            if current == boundary or current.parent == current:
                break
            current = current.parent
    return tuple(directories)


def _project_roots(context: SandboxContext) -> Tuple[Path, ...]:
    roots = [context.cwd]
    for raw in _strict_option_values(context.command_preview, "--cwd"):
        candidate = _argument_path(raw, context.cwd, bare_is_path=True)
        if candidate is not None and candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


def _effective_command_cwd(context: SandboxContext) -> Path:
    effective = context.cwd
    for raw in _strict_option_values(context.command_preview, "--cwd"):
        candidate = _argument_path(raw, context.cwd, bare_is_path=True)
        if candidate is not None:
            effective = candidate
    return effective


def _project_boundary(context: SandboxContext, root: Path) -> Path:
    if component_contains(context.workspace, root):
        return context.workspace
    current = root
    while True:
        if os.path.lexists(current / ".git"):
            return current
        if current.parent == current:
            return root
        current = current.parent


def _load_optional_toml(path: Path) -> Mapping[str, Any] | None:
    contents = _load_optional_config(path)
    if contents is None:
        return None
    try:
        parsed = load_toml_file(path)
    except Exception:
        _configuration_incompatible("Grok configuration is malformed")
    if not isinstance(parsed, Mapping):
        _configuration_incompatible("Grok configuration is malformed")
    return parsed


def _load_optional_json(path: Path) -> Mapping[str, Any] | None:
    contents = _load_optional_config(path)
    if contents is None:
        return None
    try:
        parsed = json.loads(contents)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _configuration_incompatible("Grok project MCP configuration is malformed")
    if not isinstance(parsed, Mapping):
        _configuration_incompatible("Grok project MCP configuration is malformed")
    return parsed


def _load_optional_config(path: Path) -> bytes | None:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        _configuration_incompatible("Grok configuration cannot be inspected")
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        _configuration_incompatible("Grok configuration has an unsafe file shape")
    try:
        return path.read_bytes()
    except OSError:
        _configuration_incompatible("Grok configuration cannot be inspected")


def _validate_config_data(
    data: Mapping[str, Any],
    context: SandboxContext,
    state: Path,
    execution_cwd: Path,
) -> None:
    auth = data.get("auth")
    if isinstance(auth, Mapping) and auth.get("auth_provider_command"):
        _external_auth_incompatible()

    models = data.get("model")
    if isinstance(models, Mapping):
        for model in models.values():
            if not isinstance(model, Mapping):
                continue
            env_key = model.get("env_key")
            names = [env_key] if isinstance(env_key, str) else env_key
            if names is None:
                continue
            if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
                _configuration_incompatible("a configured Grok environment key is malformed")
            for name in names:
                if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
                    _configuration_incompatible("a configured Grok environment key is malformed")

    for table_name, key in (("skills", "paths"), ("skills", "ignore"), ("plugins", "paths")):
        table = data.get(table_name)
        if not isinstance(table, Mapping) or key not in table:
            continue
        values = table[key]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            _configuration_incompatible("a configured Grok extension path is malformed")
        for raw in values:
            if not isinstance(raw, str):
                _configuration_incompatible("a configured Grok extension path is malformed")
            _require_inside_boundary(raw, execution_cwd, state, context.workspace)

    mcp = data.get("mcp_servers")
    if isinstance(mcp, Mapping):
        for server in mcp.values():
            if isinstance(server, Mapping) and server.get("enabled", True):
                _validate_process_configuration(server, context, state, execution_cwd)

    ui = data.get("ui")
    notifications = ui.get("notifications") if isinstance(ui, Mapping) else None
    hooks = notifications.get("hooks") if isinstance(notifications, Mapping) else None
    if hooks:
        if not isinstance(hooks, Sequence) or isinstance(hooks, (str, bytes)):
            _configuration_incompatible("configured Grok notification hooks are malformed")
        for hook in hooks:
            if not isinstance(hook, Mapping):
                _configuration_incompatible("configured Grok notification hooks are malformed")
            _validate_shell_command(hook.get("command"), context, state, execution_cwd)


def _validate_project_mcp_data(
    data: Mapping[str, Any],
    context: SandboxContext,
    state: Path,
    execution_cwd: Path,
) -> None:
    servers = data.get("mcpServers")
    if not isinstance(servers, Mapping):
        _configuration_incompatible("Grok project MCP configuration is malformed")
    for server in servers.values():
        if not isinstance(server, Mapping):
            _configuration_incompatible("Grok project MCP configuration is malformed")
        if server.get("enabled", True):
            _validate_process_configuration(server, context, state, execution_cwd)


def _validate_process_configuration(
    entry: Mapping[str, Any],
    context: SandboxContext,
    state: Path,
    execution_cwd: Path,
) -> None:
    command = entry.get("command")
    if command is not None:
        if not isinstance(command, str) or not command:
            _configuration_incompatible("a configured Grok extension command is malformed")
        _validate_command_token(command, context, state, execution_cwd)
    args = entry.get("args", ())
    if not isinstance(args, Sequence) or isinstance(args, (str, bytes)):
        _configuration_incompatible("configured Grok extension arguments are malformed")
    for value in args:
        if not isinstance(value, str):
            _configuration_incompatible("configured Grok extension arguments are malformed")
        path_value = _path_value(value)
        if path_value is not None:
            _require_inside_boundary(path_value, execution_cwd, state, context.workspace)
    cwd = entry.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd:
            _configuration_incompatible("a configured Grok extension cwd is malformed")
        _require_inside_boundary(cwd, execution_cwd, state, context.workspace)


def _validate_shell_command(
    value: Any,
    context: SandboxContext,
    state: Path,
    execution_cwd: Path,
) -> None:
    if not isinstance(value, str) or not value:
        _configuration_incompatible("a configured Grok hook command is malformed")
    try:
        parts = shlex.split(value)
    except ValueError:
        _configuration_incompatible("a configured Grok hook command is malformed")
    if not parts:
        _configuration_incompatible("a configured Grok hook command is malformed")
    _validate_command_token(parts[0], context, state, execution_cwd)
    for part in parts[1:]:
        path_value = _path_value(part)
        if path_value is not None:
            _require_inside_boundary(path_value, execution_cwd, state, context.workspace)


def _validate_command_token(
    value: str,
    context: SandboxContext,
    state: Path,
    execution_cwd: Path,
) -> None:
    if os.sep in value or value in {".", "..", "~"} or value.startswith("~"):
        _require_inside_boundary(value, execution_cwd, state, context.workspace)


def _path_value(value: str) -> str | None:
    if not value.startswith("-"):
        return value
    if "=" in value:
        candidate = value.split("=", 1)[1]
        return candidate or None
    if os.sep in value or ".." in value or "~" in value:
        _configuration_incompatible(
            "a configured Grok extension argument has an ambiguous path-bearing option"
        )
    return None


def _require_inside_boundary(raw: str, cwd: Path, state: Path, workspace: Path) -> None:
    try:
        candidate = Path(raw).expanduser()
    except (OSError, RuntimeError):
        _configuration_incompatible("a configured Grok filesystem dependency is malformed")
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.absolute()
    if candidate.resolve(strict=False) != candidate:
        _configuration_incompatible(
            "a configured Grok filesystem dependency has non-preserved path identity"
        )
    if not (component_contains(state, candidate) or component_contains(workspace, candidate)):
        _configuration_incompatible(
            "configured Grok filesystem dependencies escape declared state and workspace"
        )


def _require_writable_state_path(raw: str, cwd: Path, state: Path) -> None:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.absolute()
    if candidate.resolve(strict=False) != candidate or not component_contains(state, candidate):
        _configuration_incompatible(
            "a configured Grok write path escapes the complete writable state root"
        )


def _validate_declared_path(raw: str, cwd: Path, *, bare_is_path: bool = False) -> None:
    candidate = _argument_path(raw, cwd, bare_is_path=bare_is_path)
    if candidate is None:
        return
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        _configuration_incompatible("a configured Grok provider-visible path is missing")
    if resolved != candidate:
        _configuration_incompatible("a configured Grok provider-visible path is symlinked")


def _validate_project_extension_paths(context: SandboxContext) -> None:
    roots = {directory / ".grok" for directory in _project_directories(context)}
    for root in roots:
        for name in _PROJECT_EXTENSION_NAMES:
            path = root / name
            if not os.path.lexists(path):
                continue
            try:
                value = os.lstat(path)
            except OSError:
                _configuration_incompatible("a Grok project extension cannot be inspected")
            if stat.S_ISLNK(value.st_mode):
                _configuration_incompatible("a Grok project extension path is symlinked")


def _validate_ambient_compatibility(
    context: SandboxContext,
    home: Path,
    configs: Sequence[Path],
) -> None:
    merged: dict[str, Any] = {}
    for path in configs:
        data = _load_optional_toml(path)
        compat = data.get("compat") if isinstance(data, Mapping) else None
        if not isinstance(compat, Mapping):
            continue
        for vendor, values in compat.items():
            if isinstance(values, Mapping):
                current = merged.setdefault(str(vendor), {})
                current.update(values)

    sources = {
        "cursor": (
            home / ".cursor",
            *(directory / ".cursor" for directory in _project_directories(context)),
        ),
        "claude": (
            home / ".claude",
            home / ".claude.json",
            *(directory / ".claude" for directory in _project_directories(context)),
        ),
    }
    for vendor, paths in sources.items():
        settings = merged.get(vendor, {})
        active = any(
            settings.get(name, True) for name in ("skills", "rules", "agents", "mcps", "hooks")
        )
        if active and any(os.path.lexists(path) for path in paths):
            _configuration_incompatible(
                "ambient compatibility extensions escape the declared Grok state boundary"
            )


def _prepare_read_only_command(command: Sequence[str]) -> Tuple[str, ...]:
    if not command:
        _command_incompatible()
    result: list[str] = [command[0]]
    index = 1
    while index < len(command):
        item = command[index]
        if item == "--":
            _command_incompatible()
        if item in {
            "--always-approve",
            "--allow",
            "--allowedTools",
            "--deny",
            "--disallowedTools",
        }:
            if item != "--always-approve":
                if index + 1 >= len(command) or command[index + 1].startswith("-"):
                    _command_incompatible()
                index += 2
            else:
                index += 1
            continue
        if item.startswith(
            (
                "--always-approve=",
                "--allow=",
                "--allowedTools=",
                "--deny=",
                "--disallowedTools=",
            )
        ):
            if item.endswith("="):
                _command_incompatible()
            index += 1
            continue
        if item in {"--permission-mode", "--sandbox"}:
            if index + 1 >= len(command) or command[index + 1].startswith("-"):
                _command_incompatible()
            index += 2
            continue
        if item.startswith(("--permission-mode=", "--sandbox=")):
            if item.endswith("="):
                _command_incompatible()
            index += 1
            continue
        if item in {
            "--leader-socket",
            "--prompt-file",
            "--worktree-ref",
            "--debug-file",
        }:
            if index + 1 >= len(command) or command[index + 1].startswith("-"):
                _command_incompatible()
            if item in {"--leader-socket", "--prompt-file", "--worktree-ref"}:
                _command_incompatible()
        if item.startswith(
            ("--leader-socket=", "--prompt-file=", "--worktree-ref=", "--debug-file=")
        ):
            if item.endswith("="):
                _command_incompatible()
            if not item.startswith("--debug-file="):
                _command_incompatible()
        if (
            item
            in {
                "--worktree",
                "-w",
                "--restore-code",
                "--continue",
                "-c",
                "--resume",
                "-r",
                "--fork-session",
            }
            or item.startswith(
                (
                    "--worktree=",
                    "-w=",
                    "--restore-code=",
                    "--continue=",
                    "-c=",
                    "--resume=",
                    "-r=",
                    "--fork-session=",
                )
            )
            or (item.startswith("-") and not item.startswith("--") and item[1:2] in {"c", "r", "w"})
        ):
            _command_incompatible()
        result.append(item)
        index += 1

    insertion = len(result)
    for position, item in enumerate(result):
        if item in {"-p", "--single"}:
            insertion = position
            break
    result[insertion:insertion] = [
        "--permission-mode",
        "bypassPermissions",
        "--sandbox",
        "off",
    ]
    return tuple(result)


def _command_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "Grok CLI arguments conflict with the required outer-sandbox profile",
        remediation=(
            "Remove malformed or incompatible Grok permission, sandbox, prompt-file, "
            "leader, resume, worktree, or path arguments.",
        ),
    )


def _configuration_incompatible(message: str) -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        message,
        remediation=(
            "Move Grok filesystem dependencies below the complete GROK_HOME or protected "
            "workspace, disable ambient extensions, or select sandbox='none'.",
        ),
    )


def _external_auth_incompatible() -> None:
    raise SandboxFailure(
        "outer_sandbox_backend_incompatible",
        "configured external Grok authentication providers are not filesystem-compatible",
        remediation=(
            "Use cached authentication under GROK_HOME or an environment API key for this "
            "outer-sandbox session.",
        ),
    )
