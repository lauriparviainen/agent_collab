"""Dynamic backend model-catalog discovery (CLI source).

Unlike ``health.py`` probes — standard-library only, never a model call — catalog
discovery runs the provider's ``models`` *listing* command, which per the
``health.py`` contract note can require live auth and incur cost. It is therefore
a separate module, gated behind explicit refresh modes/install/background
startup, and driven by injectable dependencies (an async CLI runner and a clock)
so tests never touch real CLIs or the network.

Scope here is Part 1 / Phase 2: the ``ModelCatalogObservation`` contract, the
non-secret ``source_fingerprint`` (config + resolved provider version), the
``source="cli"`` probes with per-backend tolerant parsers, in-flight
deduplication, and local cache storage. Wiring into MCP/API/daemon/installer is
Phase 3. Only backends whose CLI exposes a listing command are discoverable
(verified live 2026-07-22: ``antigravity_cli`` -> ``agy models`` and
``xai_cli`` -> ``grok models``, both local/no-auth; ``codex_cli`` and
``claude_cli`` have no such command -> ``unsupported`` + static fallback).
Discovery never writes configuration and never raises into the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Tuple

from ...events import utc_timestamp
from ...paths import atomic_write_private_text

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 24 * 60 * 60.0
# Per-probe deadline. The design floated 2s, but a live measurement (2026-07-22)
# put ``agy models`` at a steady ~2.45s of cold-start work — 2s would make
# antigravity discovery always time out — while ``grok models`` was ~0.5s. 8s
# gives a slow CLI real headroom; probes run concurrently under an overall
# collection deadline, and both install and background refresh degrade
# non-fatally, so the wider bound only affects a genuinely wedged probe.
DEFAULT_PROBE_TIMEOUT = 8.0

# Observation statuses. "ok" is the only one that yields a usable catalog; the
# rest carry an empty model tuple and fall back to static suggestions.
STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNAVAILABLE = "unavailable"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"

# Config keys whose *value* is a secret. The fingerprint only stores a one-way
# SHA-256 digest (never the payload), so this is not leak prevention — it keeps
# the cache key stable across credential rotation (a refreshed token must not
# invalidate a catalog that does not depend on which token was used) while a
# change to a non-secret identity field still re-keys. Matched as case-folded
# substrings; the value is redacted (not the key dropped) so a config that
# merely *has* a secret-named field stays distinguishable from one that does
# not. ``authorization`` (not bare ``auth``) and the omission of ``credential``
# avoid false positives on non-secret routing/identity fields like ``auth_url``
# and ``GOOGLE_APPLICATION_CREDENTIALS`` (a file path). Precise, per-field secret
# classification is a backend-parser concern deferred to the SDK backends
# (Phase 4), which actually carry endpoints/projects/keys.
_SECRET_MARKERS = (
    "token",
    "key",
    "secret",
    "password",
    "passwd",
    "pwd",
    "bearer",
    "jwt",
    "authorization",
    "cookie",
    "session",
    "apikey",
)
_REDACTED = "<redacted>"

# A conservative model-id shape. Real ``agy``/``grok`` ids are lowercase
# alphanumerics with ``-``/``.``/``/`` separators (``gemini-3.6-flash-high``,
# ``grok-composer-2.5-fast``, ``gpt-oss-120b-medium``); tolerating uppercase too
# costs nothing. Anything with spaces, colons, or URL punctuation — warnings,
# footers, update notices — is rejected so noise never becomes an authoritative
# catalog entry.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Canonical backend ids are registry-defined (``antigravity_cli``, ``xai_cli``,
# …). Constraining the cache filename to this shape means a stray ``/`` or
# ``..`` in a backend id can never make ``path_for`` resolve outside the cache
# directory and overwrite/unlink an unrelated data file.
_CANONICAL_BACKEND_RE = re.compile(r"^[A-Za-z0-9_]+$")

NowFn = Callable[[], str]


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI probe. A timed-out or un-execable probe never reaches
    here — the runner raises ``asyncio.TimeoutError`` / ``OSError`` instead."""

    returncode: int
    stdout: str
    stderr: str = ""


# An injectable async CLI runner: given argv and a per-probe deadline, return a
# CliResult, or raise asyncio.TimeoutError (deadline) / OSError (exec failure).
CliRunner = Callable[[Sequence[str], float], Awaitable[CliResult]]


@dataclass(frozen=True)
class ModelCatalogObservation:
    """A single, structured catalog observation. On-disk JSON maps 1:1 to this."""

    backend_id: str  # canonical backend id, e.g. "antigravity_cli"
    status: str
    models: Tuple[str, ...]
    source: str  # "cli", "sdk", "static"
    complete: bool
    checked_at: str  # ISO-8601 UTC
    last_attempt_at: str  # ISO-8601 UTC
    source_fingerprint: str  # SHA-256 of non-secret effective config + version
    schema_version: int = SCHEMA_VERSION
    last_success_at: Optional[str] = None  # ISO-8601 UTC or None
    reason_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend_id,
            "status": self.status,
            "models": list(self.models),
            "source": self.source,
            "complete": self.complete,
            "checked_at": self.checked_at,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "source_fingerprint": self.source_fingerprint,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ModelCatalogObservation"]:
        """Rebuild from cache JSON; return ``None`` for anything malformed so the
        caller discards and re-probes rather than trusting a corrupt entry."""

        if not isinstance(data, Mapping):
            return None
        try:
            models = data["models"]
            if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
                return None
            schema_version = data.get("schema_version")
            if not isinstance(schema_version, int) or isinstance(schema_version, bool):
                return None
            required_str = (
                data["backend_id"],
                data["status"],
                data["source"],
                data["checked_at"],
                data["last_attempt_at"],
                data["source_fingerprint"],
            )
            if not all(isinstance(value, str) for value in required_str):
                return None
            complete = data["complete"]
            if not isinstance(complete, bool):
                return None
            last_success_at = data.get("last_success_at")
            if last_success_at is not None and not isinstance(last_success_at, str):
                return None
            reason_code = data.get("reason_code")
            if reason_code is not None and not isinstance(reason_code, str):
                return None
        except KeyError:
            return None
        return cls(
            backend_id=data["backend_id"],
            status=data["status"],
            models=tuple(models),
            source=data["source"],
            complete=complete,
            checked_at=data["checked_at"],
            last_attempt_at=data["last_attempt_at"],
            source_fingerprint=data["source_fingerprint"],
            schema_version=schema_version,
            last_success_at=last_success_at,
            reason_code=reason_code,
        )


@dataclass(frozen=True)
class ServedCatalog:
    """A cached observation returned for serving, plus whether it is past TTL.

    ``stale`` is a serve-time property, not persisted: the same last-known-good
    entry is fresh within the TTL and stale past it (serve precedence is
    fresh -> last-known-good ``stale=true`` -> static; static is the caller's
    tier when ``read`` returns ``None``)."""

    observation: ModelCatalogObservation
    stale: bool


@dataclass(frozen=True)
class CliDiscoverySpec:
    """How to list models for one discoverable CLI backend."""

    backend_id: str
    default_binary: str
    list_args: Tuple[str, ...]
    parser: Callable[[str], Tuple[str, ...]]


def _dedupe(values: Any) -> Tuple[str, ...]:
    """Order-preserving de-dup of tokens that match the model-id shape.

    Rejecting non-id tokens keeps parser tolerance (never raises, always returns
    a tuple) from turning stray CLI prose — warnings, footers, update notices —
    into catalog entries that a later ``ok``+``complete`` observation would treat
    as authoritative."""

    seen: set = set()
    result = []
    for value in values:
        text = value.strip()
        if text and text not in seen and _MODEL_ID_RE.match(text):
            seen.add(text)
            result.append(text)
    return tuple(result)


def parse_agy_models(text: str) -> Tuple[str, ...]:
    """Parse ``agy models`` — one canonical model id per line; non-id lines
    (warnings, update notices) are dropped by the shape filter."""

    return _dedupe(text.splitlines())


def parse_grok_models(text: str) -> Tuple[str, ...]:
    """Parse ``grok models`` — an ``Available models:`` section of bulleted lines
    like ``  * grok-4.5 (default)``. Only bulleted (``*``/``-``) list items are
    read, so the un-authenticated preamble above the section and any prose footer
    below it (``Visit https://x.ai ...``) are ignored; the trailing ``(default)``
    annotation is dropped by taking the first token."""

    models = []
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not in_section:
            if line.lower().startswith("available models"):
                in_section = True
            continue
        if line[0] in "*-":
            item = line[1:].strip()
            tokens = item.split()
            if tokens:
                models.append(tokens[0])
        else:
            # The first non-bulleted line ends the model list; a later bulleted
            # footer section ("Next steps: * upgrade") is not read as models.
            in_section = False
    return _dedupe(models)


# Only these backends have a verified local CLI listing command. Any canonical
# backend absent from this map returns ``status="unsupported"``.
CLI_DISCOVERY: Dict[str, CliDiscoverySpec] = {
    "antigravity_cli": CliDiscoverySpec(
        backend_id="antigravity_cli",
        default_binary="agy",
        list_args=("models",),
        parser=parse_agy_models,
    ),
    "xai_cli": CliDiscoverySpec(
        backend_id="xai_cli",
        default_binary="grok",
        list_args=("models",),
        parser=parse_grok_models,
    ),
}


def cli_discovery_spec(canonical_backend: str) -> Optional[CliDiscoverySpec]:
    return CLI_DISCOVERY.get(canonical_backend)


def _strip_secrets(mapping: Any) -> Dict[str, Any]:
    """Redact secret-named *values* while keeping their keys, so rotation is
    cache-stable but distinct configs stay distinct. Tolerant of a non-mapping
    (returns ``{}``) so fingerprinting never raises on malformed config."""

    if not isinstance(mapping, Mapping):
        return {}
    redacted: Dict[str, Any] = {}
    for key, value in mapping.items():
        lowered = str(key).casefold()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            redacted[str(key)] = _REDACTED
        else:
            redacted[str(key)] = value
    return redacted


def compute_source_fingerprint(
    canonical_backend: str, agent_config: Any, version: Optional[str]
) -> str:
    """SHA-256 of the non-secret effective config plus the resolved provider
    version. The version is included so a provider upgrade (which can change the
    catalog with identical config) invalidates a cached catalog. Secret env and
    ``backend_config`` values are redacted first. Never raises: a malformed
    config falls back to a digest of its ``repr`` so discovery always produces an
    observation rather than propagating an exception into the caller."""

    try:
        payload = {
            "backend_id": canonical_backend,
            "command": getattr(agent_config, "command", None),
            "args": list(getattr(agent_config, "args", []) or []),
            "env": _strip_secrets(getattr(agent_config, "env", None)),
            "backend_config": _strip_secrets(getattr(agent_config, "backend_config", None)),
            "version": version,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        blob = f"unfingerprintable:{canonical_backend}:{version}:{agent_config!r}"
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


_REAP_GRACE_SECONDS = 1.0


def _kill_process_tree(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the probe's whole session, not just the direct child, so a CLI
    that forked an auth/update helper does not leak descendants past the
    deadline. ``start_new_session=True`` makes the child a group leader whose
    pgid equals its pid, so the group is signalled by ``killpg(proc.pid, ...)``
    directly — never ``killpg(getpgid(pid), ...)``, which would resolve to the
    daemon's own group and kill the server if the session flag ever failed to
    apply. Falls back to killing just the direct child when process groups are
    unavailable (non-POSIX) or the group is already gone."""

    if proc.returncode is not None or not proc.pid:
        return
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(OSError):
        proc.kill()


async def _reap(proc: "asyncio.subprocess.Process") -> None:
    """Collect a killed child within a bounded grace window; if the wait itself
    stalls (rare ``D`` state / platform wait hang), hand ownership to the loop
    rather than blocking the caller forever (mirrors ``runners.py``)."""

    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), _REAP_GRACE_SECONDS)
    except asyncio.TimeoutError:
        asyncio.ensure_future(proc.wait())
    except ProcessLookupError:
        pass


async def default_cli_runner(argv: Sequence[str], timeout: float) -> CliResult:
    """Run ``argv`` with a per-probe deadline; kill the process tree and re-raise
    on timeout or cancellation."""

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own session/process group so the whole probe tree can be signalled.
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _kill_process_tree(proc)
        await _reap(proc)
        raise
    return CliResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


class ModelDiscoverer:
    """Run CLI catalog probes with per-probe deadlines and in-flight dedup.

    Concurrent ``discover`` calls for the same backend share a single probe, so a
    burst of ``fresh`` requests or a background refresh racing an install cannot
    fan out redundant provider calls."""

    def __init__(
        self,
        *,
        runner: CliRunner = default_cli_runner,
        now: NowFn = utc_timestamp,
        per_probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    ) -> None:
        self._runner = runner
        self._now = now
        self._timeout = per_probe_timeout
        self._inflight: Dict[Tuple[str, str], "asyncio.Future[ModelCatalogObservation]"] = {}

    async def discover(
        self, canonical_backend: str, agent_config: Any, *, version: Optional[str]
    ) -> ModelCatalogObservation:
        # Dedup on (backend, fingerprint): concurrent probes for the same backend
        # under the *same* effective config share one call, but a probe for a
        # different config never receives another config's catalog/fingerprint.
        fingerprint = compute_source_fingerprint(canonical_backend, agent_config, version)
        key = (canonical_backend, fingerprint)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._run(canonical_backend, agent_config, fingerprint))
            self._inflight[key] = task
            # Evict exactly when the probe finishes — not in a waiter's finally —
            # so a sole caller cancelled mid-probe cannot leave a completed task
            # pinned in the map to be served (stale) to the next caller.
            task.add_done_callback(lambda finished, k=key: self._evict(k, finished))
        # Every waiter — including the originator — shields the shared task, so
        # one caller's cancellation never tears the probe out from under the
        # others. ``_run`` returns an observation rather than raising, so the
        # shared task always completes within the per-probe deadline; nothing
        # here cancels it.
        return await asyncio.shield(task)

    def _evict(self, key: Tuple[str, str], task: "asyncio.Future[Any]") -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)

    async def discover_all(
        self, requests: Sequence[Tuple[str, Any, Optional[str]]]
    ) -> Tuple[ModelCatalogObservation, ...]:
        """Probe several enabled backends concurrently. ``requests`` is a
        sequence of ``(canonical_backend, agent_config, version)`` tuples."""

        results = await asyncio.gather(
            *(
                self.discover(canonical, cfg, version=version)
                for canonical, cfg, version in requests
            )
        )
        return tuple(results)

    async def _run(
        self, canonical_backend: str, agent_config: Any, fingerprint: str
    ) -> ModelCatalogObservation:
        attempt = self._now()

        def observe(
            status: str,
            models: Tuple[str, ...],
            complete: bool,
            *,
            reason_code: Optional[str],
            success: bool = False,
        ) -> ModelCatalogObservation:
            return ModelCatalogObservation(
                backend_id=canonical_backend,
                status=status,
                models=models,
                source="cli",
                complete=complete,
                checked_at=attempt,
                last_attempt_at=attempt,
                source_fingerprint=fingerprint,
                last_success_at=attempt if success else None,
                reason_code=reason_code,
            )

        spec = CLI_DISCOVERY.get(canonical_backend)
        if spec is None:
            return observe(STATUS_UNSUPPORTED, (), False, reason_code="no_cli_catalog_command")

        binary = getattr(agent_config, "command", None) or spec.default_binary
        argv = (binary, *spec.list_args)
        try:
            result = await self._runner(argv, self._timeout)
        except asyncio.TimeoutError:
            return observe(STATUS_TIMEOUT, (), False, reason_code="probe_timeout")
        except OSError:
            return observe(STATUS_UNAVAILABLE, (), False, reason_code="binary_not_executable")
        except asyncio.CancelledError:
            # Cancellation is a control-plane signal (task teardown, daemon
            # shutdown), not a probe outcome — propagate it rather than
            # disguising it as a timeout observation. It is a BaseException, so
            # the broad ``except Exception`` below would not catch it anyway;
            # this clause is explicit for clarity.
            raise
        except Exception:
            # A tolerant parser never runs on a failed exec; any other runner
            # error still must not raise into the caller.
            return observe(STATUS_ERROR, (), False, reason_code="probe_failed")

        if result.returncode != 0:
            return observe(STATUS_ERROR, (), False, reason_code="nonzero_exit")
        try:
            models = spec.parser(result.stdout)
        except Exception:
            models = ()
        if not models:
            return observe(STATUS_ERROR, (), False, reason_code="empty_or_unparseable")
        return observe(STATUS_OK, models, True, reason_code=None, success=True)


def _parse_iso(value: str) -> Optional[datetime]:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(checked_at: str, now: str) -> Optional[float]:
    then = _parse_iso(checked_at)
    current = _parse_iso(now)
    if then is None or current is None:
        return None
    return (current - then).total_seconds()


class ModelCatalogCache:
    """Read/write discovered catalogs under ``cache/models_<backend>.json``.

    Enforces the persisted invariants: unknown/mismatched ``schema_version`` or a
    corrupt entry is discarded; a fingerprint mismatch invalidates the serve (the
    entry is kept but not returned); a 24h UTC TTL flags an entry stale without
    deleting it; and a failed probe never overwrites a last-known-good catalog
    (short-retry semantics live with the scheduler in Phase 3, not here)."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: NowFn = utc_timestamp,
    ) -> None:
        self._dir = cache_dir
        self._ttl = ttl_seconds
        self._now = now

    def path_for(self, canonical_backend: str) -> Path:
        if not _CANONICAL_BACKEND_RE.fullmatch(canonical_backend):
            # A malformed backend id is a programming error, not user input;
            # fail loud rather than let a path-separator escape the cache tree.
            raise ValueError(f"unsafe canonical backend id for cache path: {canonical_backend!r}")
        return self._dir / f"models_{canonical_backend}.json"

    def _load(self, canonical_backend: str) -> Optional[ModelCatalogObservation]:
        path = self.path_for(canonical_backend)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            self._discard(path)
            return None
        observation = ModelCatalogObservation.from_dict(raw)
        if observation is None or observation.schema_version != SCHEMA_VERSION:
            # Corrupt, or written by an incompatible future/past schema: discard
            # and re-probe rather than trusting it.
            self._discard(path)
            return None
        return observation

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            pass

    def read(self, canonical_backend: str, *, fingerprint: str) -> Optional[ServedCatalog]:
        """Return the cached catalog for this fingerprint, flagged stale past the
        TTL; ``None`` when absent, unparseable, schema-mismatched, or keyed to a
        different effective configuration (caller falls back to static)."""

        observation = self._load(canonical_backend)
        if observation is None:
            return None
        if observation.source_fingerprint != fingerprint:
            return None
        age = _age_seconds(observation.checked_at, self._now())
        # Unparseable timestamps and a future ``checked_at`` (clock stepped back,
        # or a cache moved from a machine with forward drift) both count as stale
        # so a bad clock can never pin an entry fresh forever.
        stale = age is None or age < 0 or age >= self._ttl
        return ServedCatalog(observation=observation, stale=stale)

    def store(self, observation: ModelCatalogObservation) -> None:
        """Persist ``observation`` unless doing so would clobber a last-known-good
        catalog with a failed/incomplete probe.

        A successful (``ok``+``complete``) observation always writes. A failure
        never overwrites an existing last-known-good catalog — even one keyed to a
        different fingerprint: that entry is not served for this config (``read``
        invalidates it on fingerprint mismatch), but preserving it lets a config
        that flaps back recover its catalog instead of losing it to a transient
        failure. The first successful probe for the new config replaces it."""

        if not (observation.status == STATUS_OK and observation.complete):
            existing = self._load(observation.backend_id)
            if existing is not None and existing.status == STATUS_OK and existing.complete:
                return
        atomic_write_private_text(
            self.path_for(observation.backend_id),
            json.dumps(observation.to_dict(), sort_keys=True),
        )
