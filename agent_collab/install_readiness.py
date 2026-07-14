"""Global configured-backend readiness snapshot for the durable installer.

The installer invokes this module through the newly installed virtual
environment so SDK imports and provider command lookup reflect that environment,
not whichever bootstrap Python happened to launch ``agent_collab.sh``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .backends.base import BackendHealth, HEALTH_UNKNOWN
from .config import CollaborationConfig, backend_policy, load_user_config
from .options import assess_backend


SNAPSHOT_VERSION = 1
MAX_PROBE_WORKERS = 4
ProbeKey = Tuple[str, Optional[str]]


def collect_install_readiness(
    config: Optional[CollaborationConfig] = None,
    *,
    health: Optional[Callable[[Any], BackendHealth]] = None,
    probe_source: str = "installed environment",
) -> Dict[str, Any]:
    """Collect fresh facts for effective backends of globally configured agents."""

    from . import backends as backend_registry

    effective = config or load_user_config()
    pending: Dict[ProbeKey, Tuple[Any, Any, Any]] = {}
    agent_facts: List[Dict[str, Any]] = []
    enabled_count = 0

    for agent in effective.agents.values():
        if agent.enabled:
            enabled_count += 1
        if agent.type == "mock":
            agent_facts.append(
                {
                    "agent": agent.id,
                    "enabled": agent.enabled,
                    "canonical_backend": None,
                    "kind": "mock",
                }
            )
            continue

        backend_id = backend_registry.resolve_backend_id(agent)
        canonical = backend_registry.backend_name(agent.type, backend_id)
        fact: Dict[str, Any] = {
            "agent": agent.id,
            "enabled": agent.enabled,
            "canonical_backend": canonical,
            "kind": backend_id,
        }
        if not backend_registry.is_registered(agent.type, backend_id):
            fact["registration_error"] = (
                f"backend {backend_id!r} is not registered for agent type {agent.type!r}"
            )
        else:
            backend = backend_registry.get_backend(agent.type, backend_id)
            policy = backend_policy(effective, canonical)
            fact["policy_enabled"] = policy.enabled
            if agent.enabled and policy.enabled:
                agent_probe = getattr(backend, "probe_for_agent", None)
                probe_identity = (agent.command or agent.id) if callable(agent_probe) else None
                probe_key = (canonical, probe_identity)
                fact["probe_key"] = probe_key
                pending.setdefault(probe_key, (backend, policy, agent))
        agent_facts.append(fact)

    health_results = _probe_selected_backends(pending, health)
    rows: List[Dict[str, Any]] = []
    attention_count = 0
    for fact in agent_facts:
        row = _readiness_row(fact, pending, health_results)
        rows.append(row)
        if row["enabled"] and row["state"] not in {"usable", "disabled"}:
            attention_count += 1

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
        "attention_count": attention_count,
        "rows": rows,
    }


def _probe_selected_backends(
    pending: Mapping[ProbeKey, Tuple[Any, Any, Any]],
    health: Optional[Callable[[Any], BackendHealth]],
) -> Dict[ProbeKey, BackendHealth]:
    if health is not None:
        return {
            probe_key: _safe_probe(backend, agent, health)
            for probe_key, (backend, _, agent) in pending.items()
        }
    if not pending:
        return {}
    workers = min(MAX_PROBE_WORKERS, len(pending))
    results: Dict[ProbeKey, BackendHealth] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="install-health") as executor:
        futures = {
            executor.submit(_safe_probe, backend, agent, None): probe_key
            for probe_key, (backend, _, agent) in pending.items()
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
    pending: Mapping[ProbeKey, Tuple[Any, Any, Any]],
    health_results: Mapping[ProbeKey, BackendHealth],
) -> Dict[str, Any]:
    agent_id = str(fact["agent"])
    enabled = bool(fact["enabled"])
    canonical = fact.get("canonical_backend")
    base: Dict[str, Any] = {
        "agent": agent_id,
        "enabled": enabled,
        "backend": canonical,
        "dependency": "not checked",
        "credentials": "—",
        "version": None,
        "reason": None,
        "remediation": [],
    }
    if not enabled:
        return {**base, "dependency": "agent disabled", "state": "disabled"}
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
    if not fact.get("policy_enabled", True):
        return {
            **base,
            "dependency": "policy disabled",
            "state": "unavailable",
            "reason": f"backend {canonical!r} is disabled by user config",
            "remediation": [
                {
                    "code": "enable_backend_in_user_config",
                    "message": f"Set [backends.{canonical}] enabled = true in the user config.",
                }
            ],
        }

    probe_key = fact.get("probe_key")
    if not isinstance(probe_key, tuple) or len(probe_key) != 2:
        raise ValueError("enabled backend fact is missing its probe key")
    backend, policy, _agent = pending[probe_key]
    observed = health_results[probe_key]
    assessment = assess_backend(
        str(canonical),
        {"health": observed.to_dict(), "stale": False},
        {
            "enabled": policy.enabled,
            "block_on_unavailable": backend.block_on_unavailable,
            "checks_credentials": backend.checks_credentials,
        },
    )
    return {
        **base,
        "dependency": _dependency_summary(observed),
        "credentials": _credential_summary(observed),
        "version": observed.version,
        "state": assessment["state"],
        "reason": observed.reason,
        "remediation": assessment["remediation"],
    }


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
        payload = collect_install_readiness(probe_source=args.probe_source)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
