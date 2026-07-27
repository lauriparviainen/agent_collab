"""Global configured-backend readiness snapshot for the durable installer.

The installer invokes this module through the newly installed virtual
environment so SDK imports and provider command lookup reflect that environment,
not whichever bootstrap Python happened to launch ``agent_collab.sh``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .backends.base import BackendHealth, HEALTH_UNKNOWN
from .config import (
    CollaborationConfig,
    UsageWindowTargetConfig,
    load_user_config,
)
from .options import assess_backend
from .sandbox.paths import resolve_state_root
from .sandbox.specs import (
    CreationPolicy,
    Persistence,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
)


SNAPSHOT_VERSION = 5
MAX_PROBE_WORKERS = 4
STATE_ROOT_NOT_APPLICABLE = "—"
ProbeKey = Tuple[str, Optional[str]]

ModelDiscoveryFn = Callable[[CollaborationConfig, Mapping[str, Optional[str]]], Dict[str, Any]]


def collect_install_readiness(
    config: Optional[CollaborationConfig] = None,
    *,
    health: Optional[Callable[[Any], BackendHealth]] = None,
    probe_source: str = "installed environment",
    model_discovery: Optional[ModelDiscoveryFn] = None,
) -> Dict[str, Any]:
    """Collect fresh facts for effective backends of globally configured agents.

    Rows are backend-first: one row per probe target (canonical backend plus
    the agent-configured command identity), aggregating every enabled agent
    that selects it. Disabled agents are summarized, never probed.

    ``model_discovery`` runs live model-catalog discovery (the installer passes
    ``default_model_discovery``); the default ``None`` skips it so this
    function stays hermetic for embedded callers and tests.
    """

    from . import backends as backend_registry

    effective = config or load_user_config()
    pending: Dict[ProbeKey, Tuple[Any, Any]] = {}
    groups: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    disabled_backends = [
        name for name, section in effective.backends.items() if not section.enabled
    ]
    enabled_count = 0

    for agent in effective.agents.values():
        enabled_count += 1
        if agent.type == "mock":
            _group(groups, ("mock", agent.id), {"kind": "mock"}, agent.id)
            continue

        backend_id = backend_registry.resolve_backend_id(agent)
        canonical = backend_registry.backend_name(agent.type, backend_id)
        fact: Dict[str, Any] = {"canonical_backend": canonical, "kind": backend_id}
        if not backend_registry.is_registered(agent.type, backend_id):
            fact["registration_error"] = (
                f"backend {backend_id!r} is not registered for agent type {agent.type!r}"
            )
            _group(groups, ("unregistered", canonical), fact, agent.id)
            continue

        backend = backend_registry.get_backend(agent.type, backend_id)
        agent_probe = getattr(backend, "probe_for_agent", None)
        probe_identity = (agent.command or agent.id) if callable(agent_probe) else None
        probe_key = (canonical, probe_identity)
        fact["probe_key"] = probe_key
        pending.setdefault(probe_key, (backend, agent))
        _group(groups, ("probe", *probe_key), fact, agent.id)

    health_results = _probe_selected_backends(pending, health)
    outer_read_only = effective.system.sandbox_default is SandboxPolicy.READ_ONLY
    rows: List[Dict[str, Any]] = []
    attention_count = 0
    for group in groups.values():
        row = _readiness_row(
            group["fact"],
            group["agents"],
            pending,
            health_results,
            outer_read_only=outer_read_only,
        )
        rows.append(row)
        if row["state"] != "usable":
            attention_count += 1

    if model_discovery is not None:
        versions: Dict[str, Optional[str]] = {}
        for (canonical, _identity), observed in health_results.items():
            versions.setdefault(canonical, observed.version)
        discovery_summary = model_discovery(effective, versions)
    else:
        discovery_summary = {"attempted": [], "backends": {}, "warnings": [], "skipped": True}

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "scope": "global user config",
        "config_source": (
            "built-in defaults + user config"
            if effective.loaded_paths
            else "built-in defaults (no user config)"
        ),
        "probe_source": probe_source,
        "enabled_count": enabled_count,
        "selected_count": len(rows),
        "attention_count": attention_count,
        "disabled_backends": disabled_backends,
        "usage_windows": _usage_window_summary(effective),
        "model_discovery": discovery_summary,
        "rows": rows,
    }


def default_model_discovery(
    config: CollaborationConfig, versions: Mapping[str, Optional[str]]
) -> Dict[str, Any]:
    """Awaited install-time discovery with non-fatal degradation: any failure
    becomes a warning and the install completes on static fallbacks."""

    from .model_catalog import MODEL_DISCOVERY_FAILED, run_install_discovery

    try:
        return run_install_discovery(config, versions)
    except Exception as exc:
        return {
            "attempted": [],
            "backends": {},
            "warnings": [
                {
                    "code": MODEL_DISCOVERY_FAILED,
                    "message": (
                        "model catalog discovery failed "
                        f"({exc.__class__.__name__}); continuing with static model suggestions"
                    ),
                }
            ],
        }


def _usage_window_summary(config: CollaborationConfig) -> Dict[str, Any]:
    usage = config.usage_windows
    targets = []
    for target in sorted(usage.targets.values(), key=lambda item: item.id):
        policy = config.backends.get(target.backend)
        if not target.enabled or policy is None or not policy.enabled:
            continue
        targets.append(
            {
                "backend": target.backend,
                "model": target.model,
                "overrides": _usage_window_overrides(target),
            }
        )
    return {
        "timezone": config.system.timezone,
        "days": list(usage.days),
        "work_time": (
            f"{usage.work_time.start.strftime('%H:%M')}-{usage.work_time.end.strftime('%H:%M')}"
        ),
        "interval": _format_duration(usage.interval),
        "jitter": _format_jitter(usage.jitter),
        "targets": targets,
    }


def _usage_window_overrides(target: UsageWindowTargetConfig) -> List[str]:
    overrides = []
    if target.days is not None:
        overrides.append("days=" + ",".join(target.days))
    if target.work_time is not None:
        overrides.append(
            "work_time="
            f"{target.work_time.start.strftime('%H:%M')}-"
            f"{target.work_time.end.strftime('%H:%M')}"
        )
    if target.interval is not None:
        overrides.append("interval=" + _format_duration(target.interval))
    if target.jitter is not None:
        overrides.append("jitter=" + _format_jitter(target.jitter))
    return overrides


def _format_duration(value: Any) -> str:
    seconds = int(value.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _format_jitter(value: Any) -> str:
    rendered = _format_duration(value)
    return rendered if not value else f"±{rendered}"


def _group(
    groups: Dict[Tuple[Any, ...], Dict[str, Any]],
    key: Tuple[Any, ...],
    fact: Dict[str, Any],
    agent_id: str,
) -> None:
    entry = groups.setdefault(key, {"fact": fact, "agents": []})
    entry["agents"].append(agent_id)


def _probe_selected_backends(
    pending: Mapping[ProbeKey, Tuple[Any, Any]],
    health: Optional[Callable[[Any], BackendHealth]],
) -> Dict[ProbeKey, BackendHealth]:
    if health is not None:
        return {
            probe_key: _safe_probe(backend, agent, health)
            for probe_key, (backend, agent) in pending.items()
        }
    if not pending:
        return {}
    workers = min(MAX_PROBE_WORKERS, len(pending))
    results: Dict[ProbeKey, BackendHealth] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="install-health") as executor:
        futures = {
            executor.submit(_safe_probe, backend, agent, None): probe_key
            for probe_key, (backend, agent) in pending.items()
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def _safe_probe(
    backend: Any,
    agent: Any,
    health: Optional[Callable[[Any], BackendHealth]],
) -> BackendHealth:
    try:
        if health is not None:
            result = health(backend)
        else:
            agent_probe = getattr(backend, "probe_for_agent", None)
            result = agent_probe(agent) if callable(agent_probe) else backend.probe()
        if not isinstance(result, BackendHealth):
            raise TypeError("backend probe did not return BackendHealth")
        return result
    except Exception:
        return BackendHealth(
            status=HEALTH_UNKNOWN,
            reason="backend health probe failed",
            reason_codes=("probe_failed",),
            remediation=(
                {
                    "code": "retry_backend_probe",
                    "message": "Retry backend discovery after checking the provider installation.",
                },
            ),
        )


def _readiness_row(
    fact: Mapping[str, Any],
    agents: List[str],
    pending: Mapping[ProbeKey, Tuple[Any, Any]],
    health_results: Mapping[ProbeKey, BackendHealth],
    *,
    outer_read_only: bool = False,
) -> Dict[str, Any]:
    canonical = fact.get("canonical_backend")
    base: Dict[str, Any] = {
        "backend": canonical,
        "agents": list(agents),
        "dependency": "not checked",
        "credentials": "—",
        "version": None,
        "state_root": STATE_ROOT_NOT_APPLICABLE,
        "reason": None,
        "remediation": [],
    }
    if fact.get("kind") == "mock":
        return {
            **base,
            "backend": "built in",
            "dependency": "built in",
            "state": "usable",
        }
    if fact.get("registration_error"):
        return {
            **base,
            "dependency": "backend missing",
            "state": "unavailable",
            "reason": fact["registration_error"],
            "remediation": [
                {
                    "code": "select_registered_backend",
                    "message": "Select a backend registered for this agent type.",
                }
            ],
        }
    probe_key = fact.get("probe_key")
    if not isinstance(probe_key, tuple) or len(probe_key) != 2:
        raise ValueError("enabled backend fact is missing its probe key")
    backend, _agent = pending[probe_key]
    observed = health_results[probe_key]
    assessment = assess_backend(
        str(canonical),
        {"health": observed.to_dict(), "stale": False},
        {
            "enabled": True,
            "block_on_unavailable": backend.block_on_unavailable,
            "checks_credentials": backend.checks_credentials,
        },
    )
    state_root, state_remediation = _state_root_summary(backend, _agent)
    state = assessment["state"]
    remediation = list(assessment["remediation"])
    # A provider that has never been signed in has no state directory yet, and
    # under outer read-only that directory is the writable exception rather than
    # something the sandbox may create. Report it and leave the backend not
    # ready; signing in once is what creates it.
    if state_remediation is not None and outer_read_only:
        # A probe that already found something worse keeps its own state; an
        # unusable directory is the milder, more specific finding.
        if state == "usable":
            state = "unavailable"
        remediation.append(state_remediation)
    return {
        **base,
        "dependency": _dependency_summary(observed),
        "credentials": _credential_summary(observed),
        "version": observed.version,
        "state_root": state_root,
        "state": state,
        "reason": observed.reason,
        "remediation": remediation,
    }


def _state_root_summary(backend: Any, agent: Any) -> Tuple[str, Optional[Dict[str, str]]]:
    """Report the host-persistent state directories outer read-only requires.

    Returns the display cell plus a remediation entry when the directory is not
    usable. Backends whose roots are session-private, or that have no local
    state at all, report no requirement.

    The verdict comes from ``resolve_state_root`` itself rather than a private
    existence check, so install cannot green-light a path the session-start plan
    resolver would refuse — a symlinked or group-writable directory exists but
    is rejected there. Only ``MUST_EXIST`` roots reach it, so the resolver's
    create branch is unreachable and this stays a read-only inspection.
    """

    adapter = getattr(backend, "sandbox_adapter", None)
    if adapter is None:
        return STATE_ROOT_NOT_APPLICABLE, None
    environment = {**os.environ, **dict(getattr(agent, "env", None) or {})}
    workdir = Path.cwd()
    try:
        spec = adapter.describe(SandboxContext(workdir, workdir, environment))
        required = [
            root
            for root in spec.state_roots
            if root.persistence is Persistence.HOST and root.creation is CreationPolicy.MUST_EXIST
        ]
    except Exception:
        return "unknown", None
    if not required:
        return STATE_ROOT_NOT_APPLICABLE, None
    for root in required:
        display = _display_path(root.destination)
        try:
            resolve_state_root(root)
        except SandboxFailure as failure:
            if failure.code == "outer_sandbox_path_missing":
                return "missing", {
                    "code": "initialize_provider_state",
                    "message": (
                        f"Sign in to this provider once so it creates {display}; the outer "
                        "read-only sandbox requires that directory and will not create it."
                    ),
                }
            return "invalid", {
                "code": "repair_provider_state",
                "message": (
                    f"{display} cannot be the outer read-only writable root: {failure}. "
                    "Repair the directory or select sandbox='none'."
                ),
            }
        except Exception:
            return "unknown", None
    return "ok", None


def _display_path(path: Path) -> str:
    """Render below the home directory as ``~/...`` so install output stays terse."""

    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _dependency_summary(health: BackendHealth) -> str:
    dependency = health.checks.get("dependency")
    if not isinstance(dependency, Mapping):
        return "unknown"
    status = str(dependency.get("status") or "unknown")
    name = dependency.get("command") or dependency.get("module")
    if status == "present":
        return f"{name} found" if name else "found"
    if status == "missing":
        return f"{name} missing" if name else "missing"
    return status.replace("_", " ")


def _credential_summary(health: BackendHealth) -> str:
    credentials = health.checks.get("credentials")
    if isinstance(credentials, Mapping):
        if credentials.get("status") == "not_checked" or credentials.get("method") == "not_checked":
            return "not checked"
    return str(health.credentials or "unknown").replace("_", " ")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=argparse.SUPPRESS)
    parser.add_argument("--probe-source", default="installed environment")
    args = parser.parse_args(argv)
    try:
        payload = collect_install_readiness(
            probe_source=args.probe_source, model_discovery=default_model_discovery
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
