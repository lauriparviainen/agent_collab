"""Model-catalog serving, background refresh, and install-time discovery.

Phase 3 wiring for dynamic backend model discovery (#45). This module decides
*what to serve* — a fresh successful catalog, else the last-known-good catalog
flagged ``stale``, else static suggestions — while
``backends/common/model_discovery.py`` decides *how to observe*. The split
keeps one policy for every caller:

- ``describe_options`` serves cache-only under ``model_refresh`` ``"none"`` and
  ``"cached"`` (an empty cache falls back to the static ``suggested`` arrays)
  and probes inline only under ``"fresh"``, bounded by a per-backend minimum
  re-probe interval so repeated ``fresh`` requests cannot fan out unbounded
  provider calls.
- The daemon owns a ``ModelCatalogRefresher`` background task, started only
  after the server is ready; it refreshes stale/missing catalogs and logs a
  transition line when a refresh changes a backend's catalog or its warning
  state. Warn-only: a flapping catalog flaps a warning, never behavior.
- The installer awaits ``run_install_discovery`` with non-fatal degradation.

The configured default model is **never** validated against a catalog for
gating: ``configured_default_warning`` fires only on an authoritative
(``ok`` + ``complete``, not stale) catalog that omits the default, and the
default is always passed through unchanged. Discovery never writes
configuration; results live only under the cache directory.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .backends.common.model_discovery import (
    CLI_DISCOVERY,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_TTL_SECONDS,
    STATUS_ERROR,
    STATUS_OK,
    CliRunner,
    ModelCatalogCache,
    ModelCatalogObservation,
    ModelDiscoverer,
    ServedCatalog,
    cli_discovery_spec,
    compute_source_fingerprint,
)
from .events import utc_timestamp
from .paths import GlobalDataPaths

MODEL_REFRESH_MODES = ("none", "cached", "fresh")
CONFIGURED_DEFAULT_NOT_IN_CATALOG = "configured_default_not_in_catalog"
MODEL_DISCOVERY_FAILED = "model_discovery_failed"

# ``fresh`` performs live, possibly billable provider calls; one probe per
# backend per interval is the fan-out bound for repeated fresh requests.
MIN_FRESH_PROBE_INTERVAL_SECONDS = 60.0
# Background refresher cadence: how often stale/missing catalogs are
# re-examined, and the shortest gap between attempts for one backend (the
# "short retry" for transient failures — well under the 24h success TTL).
REFRESH_CHECK_INTERVAL_SECONDS = 900.0
MIN_BACKGROUND_PROBE_INTERVAL_SECONDS = 300.0
# Minimum spacing between kick-driven refresh cycles. A ``cached`` describe
# storm over a persistently non-authoritative catalog (e.g. provider CLI not
# installed) kicks on every response; the cooldown coalesces those kicks into
# at most one cycle per interval so the loop's config reload and cache checks
# cannot spin, independent of the per-backend probe slot that already bounds
# provider calls.
KICK_COOLDOWN_SECONDS = 60.0
# Install-time overall deadline; probes run concurrently under it and install
# degrades non-fatally to static suggestions when it expires.
INSTALL_DISCOVERY_TIMEOUT_SECONDS = 12.0

MonotonicFn = Callable[[], float]
CacheDirSource = Union[Path, str, Callable[[], Path], None]


@dataclass(frozen=True)
class CatalogView:
    """One backend's catalog as served to a caller (observation + serve tier)."""

    canonical_backend: str
    supported: bool
    refresh_request: str
    observation: Optional[ModelCatalogObservation]
    stale: bool
    served_from: str  # "fresh_probe" | "cache" | "static"
    probed: bool
    probe_gate: Optional[str] = None  # why a requested fresh probe did not run

    @property
    def catalog_ok(self) -> bool:
        observation = self.observation
        return observation is not None and observation.status == STATUS_OK and observation.complete

    @property
    def models(self) -> Tuple[str, ...]:
        """Discovered models usable for suggestions; empty unless ok+complete
        (a stale last-known-good catalog still contributes suggestions)."""

        return self.observation.models if self.catalog_ok else ()

    @property
    def authoritative(self) -> bool:
        """ok + complete and not stale — the only state allowed to warn about a
        configured default missing from the catalog."""

        return self.catalog_ok and not self.stale


def merge_model_suggestions(
    configured_default: Optional[str],
    discovered: Sequence[str],
    static_suggested: Optional[Sequence[str]],
) -> List[str]:
    """``[configured_default] + [discovered_catalog] + [static_fallback]`` with
    order-preserving dedup; the configured default always leads."""

    ordered: List[str] = []
    if isinstance(configured_default, str) and configured_default.strip():
        ordered.append(configured_default)
    for group in (discovered, static_suggested or ()):
        ordered.extend(item for item in group if isinstance(item, str) and item.strip())
    return list(dict.fromkeys(ordered))


def configured_default_warning(
    canonical_backend: str, view: CatalogView, configured_default: Optional[str]
) -> Optional[Dict[str, str]]:
    """The warn-only default check. Fires only when an authoritative catalog
    (``ok`` + ``complete``, not stale) omits the configured default; never on
    ``unsupported``/``unavailable``/``timeout``/``error``/incomplete/stale/
    fingerprint-mismatched observations — absence of evidence is never absence
    of the model. The default itself is always passed through unchanged."""

    if not isinstance(configured_default, str) or not configured_default.strip():
        return None
    if not view.authoritative or configured_default in view.models:
        return None
    return {
        "path": f"backend_options.{canonical_backend}.model",
        "code": CONFIGURED_DEFAULT_NOT_IN_CATALOG,
        "canonical_backend": canonical_backend,
        "model": configured_default,
        "message": (
            f"configured default model {configured_default!r} for {canonical_backend!r} "
            "is not present in the live model catalog; the default is used unchanged "
            "(catalog naming may differ from option values) and the provider's "
            "first-turn error remains the authority"
        ),
    }


def configured_default_model(agent_config: Any, backend: Any = None) -> Optional[str]:
    """The effective configured default model for one agent, via the backend's
    own normalization (config options over shipped defaults). ``None`` — never
    an exception — when the backend cannot normalize or ships no default."""

    if backend is None:
        from . import backends as backend_registry

        backend_id = getattr(agent_config, "backend", None) or backend_registry.DEFAULT_BACKEND
        agent_type = getattr(agent_config, "type", "")
        if not backend_registry.is_registered(agent_type, backend_id):
            return None
        backend = backend_registry.get_backend(agent_type, backend_id)
    try:
        value = dict(backend.normalize_options(agent_config, {})).get("model")
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None


class ModelCatalogService:
    """Synchronous serve/store facade over the discovery module and cache.

    Both the daemon (via ``describe_options`` worker threads) and a bare-CLI
    ``describe_options`` process use this: reads resolve the cache directory
    per call (so ``AGENT_COLLAB_HOME`` is honored), and ``"fresh"`` probes run
    a short-lived event loop in the calling thread. In-flight dedup within one
    call comes from ``ModelDiscoverer``; the cross-call bound is one shared
    per-backend probe-slot map (``acquire_probe_slot``) that inline ``fresh``
    serving and the daemon's background refresher both consult — in the daemon
    process both use the ``default_service`` singleton, so a burst of fresh
    requests racing a background cycle cannot fan out concurrent provider
    calls for one backend."""

    def __init__(
        self,
        *,
        cache_dir: CacheDirSource = None,
        runner: Optional[CliRunner] = None,
        now: Callable[[], str] = utc_timestamp,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        per_probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        min_fresh_interval: float = MIN_FRESH_PROBE_INTERVAL_SECONDS,
        monotonic: MonotonicFn = time.monotonic,
    ) -> None:
        self._cache_dir = cache_dir
        self._runner = runner
        self._now = now
        self._ttl = ttl_seconds
        self._probe_timeout = per_probe_timeout
        self._min_fresh_interval = min_fresh_interval
        self._monotonic = monotonic
        self._lock = threading.Lock()
        # One probe-attempt map for every entry point (inline fresh and the
        # background refresher), keyed by canonical backend.
        self._last_probe: Dict[str, float] = {}
        # Backends whose most recent probe failed; an ok catalog for such a
        # backend keeps serving flagged stale until a probe succeeds again, so
        # a failed revalidation is durable within the process, not just for
        # the one response that observed it.
        self._failed_backends: set = set()

    def _resolve_cache_dir(self) -> Path:
        directory = self._cache_dir
        if callable(directory):
            directory = directory()
        if directory is None:
            directory = GlobalDataPaths.resolve().cache_dir
        return Path(directory)

    def _cache(self, directory: Optional[Path] = None) -> ModelCatalogCache:
        return ModelCatalogCache(
            directory if directory is not None else self._resolve_cache_dir(),
            ttl_seconds=self._ttl,
            now=self._now,
        )

    def read(self, canonical_backend: str, fingerprint: str) -> Optional[ServedCatalog]:
        return self._cache().read(canonical_backend, fingerprint=fingerprint)

    def store(self, observation: ModelCatalogObservation) -> None:
        # Track probe outcomes first so the failure memory is recorded even if
        # the cache directory turns out to be unwritable.
        with self._lock:
            if observation.status == STATUS_OK and observation.complete:
                self._failed_backends.discard(observation.backend_id)
            else:
                self._failed_backends.add(observation.backend_id)
        directory = self._resolve_cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # Owner-only, matching GlobalDataPaths.ensure_dirs for the same tree.
        with suppress(OSError):
            directory.chmod(0o700)
        self._cache(directory).store(observation)

    def recently_failed(self, canonical_backend: str) -> bool:
        """True while the backend's most recent probe (from any entry point in
        this process) failed and no probe has succeeded since."""

        with self._lock:
            return canonical_backend in self._failed_backends

    def acquire_probe_slot(self, canonical_backend: str, min_interval: float) -> bool:
        """Check-and-set the shared per-backend probe slot. Returns False when
        the last probe attempt (by any caller) is within ``min_interval``
        seconds, so inline ``fresh`` requests and background refresh cycles
        rate-limit each other instead of fanning out provider calls."""

        with self._lock:
            now = self._monotonic()
            last = self._last_probe.get(canonical_backend)
            if last is not None and (now - last) < min_interval:
                return False
            self._last_probe[canonical_backend] = now
            return True

    def serve(
        self,
        canonical_backend: str,
        agent_config: Any,
        *,
        version: Optional[str],
        refresh: str,
        allow_probe: bool = True,
    ) -> CatalogView:
        """Serve one backend's catalog under the requested refresh mode.

        ``"none"`` and ``"cached"`` are strictly local: cache read only, no
        network/CLI call of any kind. ``"fresh"`` probes inline when the
        backend is discoverable, enabled (``allow_probe``), and outside the
        minimum re-probe interval; a failed fresh probe serves the
        last-known-good catalog flagged ``stale``."""

        if refresh not in MODEL_REFRESH_MODES:
            raise ValueError("model_refresh must be 'none', 'cached', or 'fresh'")
        supported = cli_discovery_spec(canonical_backend) is not None
        if not supported:
            # No discovery source means no cache entry can exist; skip the
            # cache entirely (this also keeps the serve path safe for
            # externally registered backends whose canonical names fall
            # outside the cache-filename shape).
            return CatalogView(
                canonical_backend,
                False,
                refresh,
                None,
                stale=False,
                served_from="static",
                probed=False,
                probe_gate="unsupported" if refresh == "fresh" else None,
            )
        fingerprint = compute_source_fingerprint(canonical_backend, agent_config, version)
        probed = False
        probe_gate: Optional[str] = None
        failed_fresh = False
        observation: Optional[ModelCatalogObservation] = None
        if refresh == "fresh":
            if not allow_probe:
                probe_gate = "backend_disabled"
            elif not self.acquire_probe_slot(canonical_backend, self._min_fresh_interval):
                probe_gate = "min_probe_interval"
            else:
                probed = True
                observation = self._probe(canonical_backend, agent_config, version, fingerprint)
                try:
                    self.store(observation)
                except Exception:
                    # An unwritable cache degrades persistence, never serving.
                    pass
                if observation.status == STATUS_OK and observation.complete:
                    return CatalogView(
                        canonical_backend,
                        supported,
                        refresh,
                        observation,
                        stale=False,
                        served_from="fresh_probe",
                        probed=True,
                    )
                failed_fresh = True
        served = self.read(canonical_backend, fingerprint)
        if served is not None:
            catalog_ok = served.observation.status == STATUS_OK and served.observation.complete
            # A failed refresh flags the surviving last-known-good stale — not
            # just for the response that observed the failure, but for every
            # serve in this process until a probe succeeds again.
            stale = served.stale or (
                catalog_ok and (failed_fresh or self.recently_failed(canonical_backend))
            )
            served_from = "fresh_probe" if probed and not catalog_ok else "cache"
            return CatalogView(
                canonical_backend,
                supported,
                refresh,
                served.observation,
                stale=stale,
                served_from=served_from,
                probed=probed,
                probe_gate=probe_gate,
            )
        if observation is not None:
            # The failure could not be cached either; still report it honestly.
            return CatalogView(
                canonical_backend,
                supported,
                refresh,
                observation,
                stale=False,
                served_from="fresh_probe",
                probed=True,
                probe_gate=probe_gate,
            )
        return CatalogView(
            canonical_backend,
            supported,
            refresh,
            None,
            stale=False,
            served_from="static",
            probed=probed,
            probe_gate=probe_gate,
        )

    def _probe(
        self,
        canonical_backend: str,
        agent_config: Any,
        version: Optional[str],
        fingerprint: str,
    ) -> ModelCatalogObservation:
        kwargs: Dict[str, Any] = {"now": self._now, "per_probe_timeout": self._probe_timeout}
        if self._runner is not None:
            kwargs["runner"] = self._runner
        discoverer = ModelDiscoverer(**kwargs)
        try:
            return asyncio.run(
                discoverer.discover(canonical_backend, agent_config, version=version)
            )
        except Exception:
            # ``discover`` never raises; this guards the event-loop scaffolding
            # (e.g. a caller thread that unexpectedly runs a loop already) so a
            # probe failure is an observation, never an exception into serving.
            now = self._now()
            return ModelCatalogObservation(
                backend_id=canonical_backend,
                status=STATUS_ERROR,
                models=(),
                source="cli",
                complete=False,
                checked_at=now,
                last_attempt_at=now,
                source_fingerprint=fingerprint,
                reason_code="probe_execution_failed",
            )


_DEFAULT_SERVICE: Optional[ModelCatalogService] = None
_DEFAULT_SERVICE_LOCK = threading.Lock()


def default_service() -> ModelCatalogService:
    """Process-wide service (shared min-interval gate across MCP/HTTP callers).
    Cache paths resolve per call, so ``AGENT_COLLAB_HOME`` stays honored."""

    global _DEFAULT_SERVICE
    with _DEFAULT_SERVICE_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = ModelCatalogService()
        return _DEFAULT_SERVICE


def _registry_health_version(agent_type: str, backend_id: str) -> Optional[str]:
    """Provider version from the shared health cache (side-effect-free probe;
    the same source ``describe_options`` fingerprints with, so both paths key
    the cache identically)."""

    from . import backends as backend_registry

    try:
        backend = backend_registry.get_backend(agent_type, backend_id)
        return backend_registry.HEALTH.health(backend).version
    except Exception:
        return None


def _load_user_config_default() -> Any:
    from .config import load_user_config

    return load_user_config()


def start_catalog_warnings(
    config: Any,
    agent_backends: Mapping[str, str],
    *,
    service: Optional[ModelCatalogService] = None,
    version_for: Optional[Callable[[str, str], Optional[str]]] = None,
) -> List[Dict[str, str]]:
    """Echo the ``configured_default_not_in_catalog`` warning into a start.

    Cache-only (``refresh="cached"``): a start never probes a catalog, and
    backends without a CLI listing command are skipped entirely — so
    ``claude_cli``/``codex_cli`` keep their never-probed start contract. Any
    internal failure yields no warnings rather than failing the start."""

    from . import backends as backend_registry

    active_service = service or default_service()
    resolve_version = version_for or _registry_health_version
    warnings: List[Dict[str, str]] = []
    seen: set = set()
    for agent_id, backend_id in agent_backends.items():
        try:
            agent = config.agents[agent_id]
            canonical = backend_registry.backend_name(agent.type, backend_id)
            if cli_discovery_spec(canonical) is None:
                continue
            backend = backend_registry.get_backend(agent.type, backend_id)
            default = configured_default_model(agent, backend)
            if default is None or (canonical, default) in seen:
                continue
            seen.add((canonical, default))
            version = resolve_version(agent.type, backend_id)
            view = active_service.serve(canonical, agent, version=version, refresh="cached")
            warning = configured_default_warning(canonical, view, default)
            if warning is not None:
                warnings.append(warning)
        except Exception:
            continue
    return warnings


class ModelCatalogRefresher:
    """Daemon-owned background refresh loop.

    Runs only after the server is ready (the caller creates the task then).
    Each cycle re-reads the user config, and for every enabled discoverable
    backend refreshes the catalog when it is missing, keyed to a different
    fingerprint, stale, marked recently-failed, or a non-ok observation —
    bounded by the service's shared per-backend probe slot (so cycles also
    rate-limit against inline ``fresh`` probes). Running sessions are never
    touched: options are snapshotted into session settings at start, and this
    loop only writes the cache. ``kick()`` (from a ``cached`` describe that
    served a non-authoritative catalog) wakes the loop early; kicks are
    coalesced and rate-limited to one cycle per ``kick_cooldown``, and the
    per-backend probe slot separately bounds provider calls."""

    def __init__(
        self,
        *,
        logger: Callable[[str], None],
        service: Optional[ModelCatalogService] = None,
        load_config: Optional[Callable[[], Any]] = None,
        discoverer: Optional[ModelDiscoverer] = None,
        version_for: Optional[Callable[[str, str], Optional[str]]] = None,
        check_interval: float = REFRESH_CHECK_INTERVAL_SECONDS,
        min_probe_interval: float = MIN_BACKGROUND_PROBE_INTERVAL_SECONDS,
        kick_cooldown: float = KICK_COOLDOWN_SECONDS,
    ) -> None:
        self._log = logger
        self._service = service or default_service()
        self._load_config = load_config or _load_user_config_default
        self._discoverer = discoverer or ModelDiscoverer()
        self._version_for = version_for or _registry_health_version
        self._check_interval = check_interval
        self._min_probe_interval = min_probe_interval
        self._kick_cooldown = kick_cooldown
        self._wake: Optional[asyncio.Event] = None

    def kick(self) -> None:
        """Request an early refresh cycle; safe to call before ``run`` starts."""

        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        self._wake = asyncio.Event()
        loop = asyncio.get_running_loop()
        while True:
            cycle_started = loop.time()
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"model catalog refresh cycle failed: {exc!r}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._check_interval)
            except asyncio.TimeoutError:
                pass
            else:
                # Kicked awake: enforce the cooldown since the last cycle
                # started, so a describe-poll kick storm collapses into at
                # most one cycle per cooldown. Kicks landing during the sleep
                # keep the event set and fold into the cycle about to run.
                remaining = self._kick_cooldown - (loop.time() - cycle_started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._wake.clear()

    async def refresh_once(self) -> None:
        config = await asyncio.to_thread(self._load_config)
        for canonical in sorted(CLI_DISCOVERY):
            # Derived default agents exist only for enabled backends, so a
            # disabled backend is skipped without any probe.
            agent = config.agents.get(canonical)
            if agent is None:
                continue
            try:
                await self._refresh_backend(canonical, agent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"model catalog refresh failed for {canonical}: {exc!r}")

    async def _refresh_backend(self, canonical: str, agent: Any) -> None:
        from . import backends as backend_registry

        backend_id = agent.backend or backend_registry.DEFAULT_BACKEND
        version = await asyncio.to_thread(self._version_for, agent.type, backend_id)
        fingerprint = compute_source_fingerprint(canonical, agent, version)
        before = await asyncio.to_thread(self._service.read, canonical, fingerprint)
        if (
            before is not None
            and not before.stale
            and before.observation.status == STATUS_OK
            and before.observation.complete
            # A recently failed probe (from any entry point) keeps this
            # last-known-good under revalidation until a probe succeeds.
            and not self._service.recently_failed(canonical)
        ):
            return
        # The shared probe slot rate-limits background attempts against both
        # earlier cycles and inline ``fresh`` probes for the same backend.
        if not self._service.acquire_probe_slot(canonical, self._min_probe_interval):
            return
        observation = await self._discoverer.discover(canonical, agent, version=version)
        # The cache refuses to clobber a last-known-good catalog with a failure;
        # the shorter cycle interval is the transient-failure retry.
        await asyncio.to_thread(self._service.store, observation)
        after = await asyncio.to_thread(self._service.read, canonical, fingerprint)
        self._log_transition(canonical, agent, before, after)

    def _log_transition(
        self,
        canonical: str,
        agent: Any,
        before: Optional[ServedCatalog],
        after: Optional[ServedCatalog],
    ) -> None:
        default = configured_default_model(agent)
        before_view = _view_from_served(canonical, before)
        after_view = _view_from_served(canonical, after)
        before_models = before_view.models
        after_models = after_view.models
        before_warn = configured_default_warning(canonical, before_view, default) is not None
        after_warn = configured_default_warning(canonical, after_view, default) is not None
        if before_models == after_models and before_warn == after_warn:
            return
        added = len(set(after_models) - set(before_models))
        removed = len(set(before_models) - set(after_models))
        self._log(
            f"model catalog transition for {canonical}: "
            f"{len(before_models)} -> {len(after_models)} models (+{added}/-{removed}); "
            f"default-warning {'on' if before_warn else 'off'} -> "
            f"{'on' if after_warn else 'off'} (warn-only; behavior unchanged)"
        )


def _view_from_served(canonical: str, served: Optional[ServedCatalog]) -> CatalogView:
    if served is None:
        return CatalogView(
            canonical, True, "cached", None, stale=False, served_from="static", probed=False
        )
    return CatalogView(
        canonical,
        True,
        "cached",
        served.observation,
        stale=served.stale,
        served_from="cache",
        probed=False,
    )


def run_install_discovery(
    config: Any,
    versions: Mapping[str, Optional[str]],
    *,
    service: Optional[ModelCatalogService] = None,
    discoverer: Optional[ModelDiscoverer] = None,
    timeout: float = INSTALL_DISCOVERY_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Install-time discovery: awaited, concurrent, and non-fatal.

    Probes every enabled discoverable backend, seeds the cache, and returns a
    summary with any warnings — a timeout or error becomes a
    ``model_discovery_failed`` warning and the install completes on static
    fallbacks. The install-time ``configured_default_not_in_catalog`` warning
    (naming the backend and the missing default) is emitted only when an
    ok+complete catalog omits the configured default."""

    active_service = service or default_service()
    requests = []
    for canonical in sorted(CLI_DISCOVERY):
        agent = config.agents.get(canonical)
        if agent is None:  # disabled backends have no derived agent; never probed
            continue
        requests.append((canonical, agent, versions.get(canonical)))
    summary: Dict[str, Any] = {
        "attempted": [item[0] for item in requests],
        "backends": {},
        "warnings": [],
    }
    if not requests:
        return summary
    active_discoverer = discoverer or ModelDiscoverer()

    async def _collect() -> Tuple[ModelCatalogObservation, ...]:
        return await asyncio.wait_for(active_discoverer.discover_all(requests), timeout)

    try:
        observations = asyncio.run(_collect())
    except Exception as exc:
        summary["warnings"].append(
            {
                "code": MODEL_DISCOVERY_FAILED,
                "message": (
                    "model catalog discovery failed "
                    f"({exc.__class__.__name__}); continuing with static model suggestions"
                ),
            }
        )
        return summary
    by_backend = {observation.backend_id: observation for observation in observations}
    for canonical, agent, _version in requests:
        observation = by_backend.get(canonical)
        if observation is None:
            continue
        entry: Dict[str, Any] = {
            "status": observation.status,
            "complete": observation.complete,
            "models": len(observation.models),
            "reason_code": observation.reason_code,
        }
        try:
            active_service.store(observation)
            entry["cached"] = True
        except Exception:
            entry["cached"] = False
        summary["backends"][canonical] = entry
        if observation.status == STATUS_OK and observation.complete:
            view = CatalogView(
                canonical,
                True,
                "fresh",
                observation,
                stale=False,
                served_from="fresh_probe",
                probed=True,
            )
            warning = configured_default_warning(canonical, view, configured_default_model(agent))
            if warning is not None:
                summary["warnings"].append(warning)
    return summary
