"""Live, side-effect-free backend health probes and a short-TTL cache.

Availability is a *live* property of the daemon, not frozen at startup:
installing ``agy``, signing in, or ``pip install``-ing the SDK extra should make
a backend usable, and removing one should be diagnosable before a session burns
a turn. Every probe here is standard-library only and never makes a model call
(``agy models`` and any SDK call cost/require live auth).

Probes take injectable dependencies (``which``/``find_spec``/version/clock/
credential functions) so tests can drive them with fake PATH, clock, and
filesystem without touching real CLIs, the SDK, or ``~/.gemini``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ...events import utc_timestamp
from ..base import (
    CREDENTIALS_MISSING,
    CREDENTIALS_OK,
    CREDENTIALS_UNKNOWN,
    HEALTH_OK,
    HEALTH_UNAVAILABLE,
    BackendHealth,
)

WhichFn = Callable[[str], Optional[str]]
CredentialsFn = Callable[[], str]
VersionFn = Callable[[str, str], Optional[str]]
NowFn = Callable[[], str]

DEFAULT_TTL_SECONDS = 60.0


def default_version_runner(binary: str, path: str) -> Optional[str]:
    """Best-effort ``<binary> --version``; ``None`` when it fails or is silent."""

    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return None
    return output.splitlines()[0].strip()


def probe_cli_backend(
    binary: str,
    *,
    which: Optional[WhichFn] = None,
    run_version: Optional[VersionFn] = None,
    credentials: Optional[CredentialsFn] = None,
    now: Optional[NowFn] = None,
) -> BackendHealth:
    """Probe a subprocess (``cli``) backend: PATH presence, version, credentials."""

    which = which or shutil.which
    now = now or utc_timestamp
    checked_at = now()
    path = which(binary)
    if not path:
        return BackendHealth(
            status=HEALTH_UNAVAILABLE,
            reason=f"{binary}: command not found on PATH",
            credentials=CREDENTIALS_UNKNOWN,
            version=None,
            checked_at=checked_at,
            checks={
                "dependency": {"status": "missing", "kind": "path", "command": binary},
                "credentials": {"status": "not_checked"},
            },
            reason_codes=("dependency_missing",),
            remediation=(
                {
                    "code": "install_cli",
                    "message": f"Install {binary} and ensure it is available on the daemon PATH.",
                },
            ),
        )
    version = run_version(binary, path) if run_version is not None else None
    creds = credentials() if credentials is not None else CREDENTIALS_UNKNOWN
    return BackendHealth(
        status=HEALTH_OK,
        reason=None,
        credentials=creds,
        version=version,
        checked_at=checked_at,
        checks={
            "dependency": {
                "status": "present",
                "kind": "path",
                "command": binary,
                "version": version,
            },
            "credentials": {
                "status": creds,
                "method": "provider_local_evidence" if credentials is not None else "not_checked",
            },
        },
    )


def probe_sdk_backend(
    module_name: str,
    *,
    find_spec: Optional[Callable[[str], Any]] = None,
    package_version: Optional[Callable[[], Optional[str]]] = None,
    credentials: Optional[CredentialsFn] = None,
    now: Optional[NowFn] = None,
    extra_hint: Optional[str] = None,
) -> BackendHealth:
    """Probe an ``sdk`` backend by import check only (never executes the SDK)."""

    if find_spec is None:
        import importlib.util

        find_spec = importlib.util.find_spec
    now = now or utc_timestamp
    checked_at = now()
    try:
        spec = find_spec(module_name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        reason = f"{module_name} is not importable"
        if extra_hint:
            reason = f"{reason}; {extra_hint}"
        return BackendHealth(
            status=HEALTH_UNAVAILABLE,
            reason=reason,
            credentials=CREDENTIALS_UNKNOWN,
            version=None,
            checked_at=checked_at,
            checks={
                "dependency": {"status": "missing", "kind": "python_module", "module": module_name},
                "credentials": {"status": "not_checked"},
            },
            reason_codes=("dependency_missing",),
            remediation=(
                {
                    "code": "install_sdk",
                    "message": extra_hint or f"Install the Python package that provides {module_name}.",
                },
            ),
        )
    version = package_version() if package_version is not None else None
    creds = credentials() if credentials is not None else CREDENTIALS_UNKNOWN
    return BackendHealth(
        status=HEALTH_OK,
        reason=None,
        credentials=creds,
        version=version,
        checked_at=checked_at,
        checks={
            "dependency": {
                "status": "present",
                "kind": "python_module",
                "module": module_name,
                "version": version,
            },
            "credentials": {
                "status": creds,
                "method": "provider_local_evidence" if credentials is not None else "not_checked",
            },
        },
    )


def antigravity_credentials(gemini_home: Optional[Path] = None) -> str:
    """Best-effort Antigravity sign-in check under ``~/.gemini`` (never a call).

    ``ok`` when a cached OAuth token or an ``active`` Google account is present;
    ``missing`` when neither exists (a definite absence); ``unknown`` when the
    accounts file exists but cannot be read/parsed (indeterminate — never block).
    """

    base = gemini_home if gemini_home is not None else Path.home() / ".gemini"
    token = base / "antigravity-cli" / "antigravity-oauth-token"
    if token.exists():
        return CREDENTIALS_OK
    accounts = base / "google_accounts.json"
    if accounts.exists():
        try:
            data = json.loads(accounts.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return CREDENTIALS_UNKNOWN
        if isinstance(data, dict) and data.get("active"):
            return CREDENTIALS_OK
        return CREDENTIALS_MISSING
    return CREDENTIALS_MISSING


def anthropic_api_key_credentials(env: Optional[Mapping[str, str]] = None) -> str:
    """Credential check for the Claude **sdk** backend (never a model call).

    ``ANTHROPIC_API_KEY`` present is a definite ``ok``; its absence is only
    ``unknown`` (never ``missing``) because the Claude Agent SDK also authenticates
    through Claude Code's local sign-in — so this warns, never blocks, a setup that
    is signed in without an env key.
    """

    environ = os.environ if env is None else env
    if environ.get("ANTHROPIC_API_KEY"):
        return CREDENTIALS_OK
    return CREDENTIALS_UNKNOWN


def codex_api_key_credentials(env: Optional[Mapping[str, str]] = None) -> str:
    """Credential check for the Codex **sdk** backend (never a model call).

    ``OPENAI_API_KEY`` present is a definite ``ok``; its absence is only
    ``unknown`` (never ``missing``) because the Codex SDK drives the local Codex
    app-server, which also has its own local sign-in — so this warns, never blocks.
    """

    environ = os.environ if env is None else env
    if environ.get("OPENAI_API_KEY"):
        return CREDENTIALS_OK
    return CREDENTIALS_UNKNOWN


def gemini_api_key_credentials(env: Optional[Mapping[str, str]] = None) -> str:
    """Credential check for the Antigravity **sdk** backend.

    The SDK authenticates with a Gemini API key (``GEMINI_API_KEY`` env or
    ``LocalAgentConfig(api_key=...)``) — **not** the ``~/.gemini`` OAuth that the
    ``agy`` CLI uses. ``GEMINI_API_KEY`` present is a definite ``ok``; its absence
    is only ``unknown`` (never ``missing``), because the key can also come from
    config or Vertex/ADC — so this never blocks a working setup, only warns.
    """

    environ = os.environ if env is None else env
    if environ.get("GEMINI_API_KEY"):
        return CREDENTIALS_OK
    return CREDENTIALS_UNKNOWN


@dataclass(frozen=True)
class HealthObservation:
    health: BackendHealth
    cache_hit: bool
    age_seconds: float
    ttl_seconds: float

    @property
    def stale(self) -> bool:
        return self.age_seconds >= self.ttl_seconds


class HealthCache:
    """Cache probe results with a short TTL; ``fresh=True`` always re-probes.

    ``describe_options`` reads cached health for near-current display without
    hammering the filesystem; start requests pass ``fresh=True`` so gating never
    acts on stale state and "install/sign in, then start" works with no restart.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: Dict[Tuple[str, str], Tuple[BackendHealth, float]] = {}

    def health(self, backend: Any, *, fresh: bool = False) -> BackendHealth:
        return self.observe(backend, fresh=fresh).health

    def observe(self, backend: Any, *, fresh: bool = False) -> HealthObservation:
        key = (backend.agent_type, backend.id)
        now = self._clock()
        if not fresh:
            cached = self._entries.get(key)
            if cached is not None and (now - cached[1]) < self._ttl:
                return HealthObservation(cached[0], True, max(0.0, now - cached[1]), self._ttl)
        result = backend.probe()
        stored_at = self._clock()
        self._entries[key] = (result, stored_at)
        return HealthObservation(result, False, 0.0, self._ttl)

    def clear(self) -> None:
        self._entries.clear()
