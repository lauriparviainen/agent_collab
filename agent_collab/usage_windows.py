"""Usage-window planning, private state, and daemon-owned scheduling.

The planner is wall-clock aware and side-effect free.  The scheduler persists
every jitter choice before sleeping and marks an anchor attempted before it
starts a normal visible collaboration session.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import (
    CollaborationConfig,
    UsageWindowTargetConfig,
    WorkTimeConfig,
    backend_policy,
    effective_usage_window_schedule,
    normalized_usage_window_options,
    split_canonical_backend,
)
from .paths import GlobalDataPaths, atomic_write_private_text
from .retention import DONE, TERMINAL_STATUSES


STATE_SCHEMA_VERSION = 1
ON_TIME_WAKE_GRACE = timedelta(minutes=1)
USAGE_WINDOW_PROMPT = (
    "agent-collab usage-window alignment request. Do not use any tools; reply with exactly: OK"
)
DAY_INDEX = {
    name: index for index, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
}
ZONEINFO_ROOTS = (
    Path("/usr/share/zoneinfo"),
    Path("/usr/share/lib/zoneinfo"),
    Path("/usr/lib/zoneinfo"),
    Path("/var/db/timezone/zoneinfo"),
)


class RandomSource(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True)
class WorkInterval:
    local_day: date
    start: datetime
    end: datetime


@dataclass(frozen=True)
class UsageWindowAnchor:
    base: datetime
    window: WorkInterval


@dataclass(frozen=True)
class ScheduleDecision:
    action: str  # wait | attempt | none
    anchor: Optional[UsageWindowAnchor] = None
    planned_at: Optional[datetime] = None
    catch_up: bool = False
    skipped: int = 0


@dataclass
class LoadedUsageWindowState:
    targets: Dict[str, Dict[str, Any]]
    trustworthy: bool


def _iana_name_for_path(path: Path) -> Optional[str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    for root in ZONEINFO_ROOTS:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
    return None


def _zone_from_tz_environment() -> tuple[Optional[tzinfo], bool]:
    """Resolve an authoritative TZ override without consulting host defaults."""

    if "TZ" not in os.environ:
        return None, False
    raw = os.environ["TZ"].removeprefix(":")
    if not raw:
        return ZoneInfo("UTC"), True
    path = Path(raw)
    try:
        if path.is_absolute():
            iana_name = _iana_name_for_path(path)
            if iana_name is not None:
                return ZoneInfo(iana_name), True
            with path.open("rb") as zone_file:
                return ZoneInfo.from_file(zone_file, key=raw), True
        return ZoneInfo(raw), True
    except (OSError, ValueError, ZoneInfoNotFoundError):
        # POSIX TZ rules are not understood by zoneinfo. The platform-local
        # fixed-offset fallback still honors the process override for the
        # current season and is safer than consulting contradictory /etc data.
        return None, True


def _local_zone_names() -> list[str]:
    """Return IANA names advertised by standard host-local settings."""

    names: list[str] = []
    localtime_name = _iana_name_for_path(Path("/etc/localtime"))
    if localtime_name is not None:
        names.append(localtime_name)

    try:
        timezone_name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        timezone_name = ""
    if timezone_name and not Path(timezone_name).is_absolute():
        names.append(timezone_name)
    return list(dict.fromkeys(names))


def resolve_timezone(name: str) -> tzinfo:
    """Resolve an IANA name or the best available platform-local timezone."""

    if name == "local":
        env_zone, env_configured = _zone_from_tz_environment()
        if env_configured:
            return env_zone or datetime.now().astimezone().tzinfo or timezone.utc
        for local_name in _local_zone_names():
            try:
                return ZoneInfo(local_name)
            except (OSError, ValueError, ZoneInfoNotFoundError):
                continue
        return datetime.now().astimezone().tzinfo or timezone.utc
    return ZoneInfo(name)


def _valid_local_candidates(naive: datetime, zone: tzinfo) -> list[datetime]:
    candidates = []
    seen = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) != naive:
            continue
        key = aware.astimezone(timezone.utc)
        if key not in seen:
            candidates.append(aware)
            seen.add(key)
    candidates.sort(key=lambda item: item.astimezone(timezone.utc))
    return candidates


def resolve_local_datetime(naive: datetime, zone: tzinfo) -> datetime:
    """Resolve one local wall time, choosing fold 0 and advancing gaps."""

    candidate = naive
    # Civil-time gaps are normally one hour.  A two-day bound also covers
    # historical date-line changes without an unbounded loop.
    for _minute in range(48 * 60 + 1):
        valid = _valid_local_candidates(candidate, zone)
        if valid:
            return valid[0]
        candidate += timedelta(minutes=1)
    raise ValueError(f"could not resolve local datetime {naive.isoformat()}")


def work_interval_for_day(local_day: date, work_time: WorkTimeConfig, zone: tzinfo) -> WorkInterval:
    """Build the interval owned by ``local_day`` (including overnight)."""

    start_naive = datetime.combine(local_day, work_time.start)
    end_day = local_day + timedelta(days=1) if work_time.end <= work_time.start else local_day
    end_naive = datetime.combine(end_day, work_time.end)
    start = resolve_local_datetime(start_naive, zone)
    end = resolve_local_datetime(end_naive, zone)
    return WorkInterval(local_day=local_day, start=start, end=end)


def anchors_for_day(
    local_day: date,
    work_time: WorkTimeConfig,
    interval: timedelta,
    zone: tzinfo,
) -> list[UsageWindowAnchor]:
    """Return drift-free base anchors strictly before the interval end."""

    window = work_interval_for_day(local_day, work_time, zone)
    start_naive = datetime.combine(local_day, work_time.start)
    end_day = local_day + timedelta(days=1) if work_time.end <= work_time.start else local_day
    end_naive = datetime.combine(end_day, work_time.end)
    result = []
    seen_instants = set()
    naive = start_naive
    while naive < end_naive:
        base = resolve_local_datetime(naive, zone)
        base_utc = base.astimezone(timezone.utc)
        if base_utc not in seen_instants and window.start.astimezone(
            timezone.utc
        ) <= base_utc < window.end.astimezone(timezone.utc):
            result.append(UsageWindowAnchor(base=base, window=window))
            seen_instants.add(base_utc)
        naive += interval
    return result


def jitter_anchor(
    anchor: UsageWindowAnchor,
    jitter: timedelta,
    random_source: RandomSource,
) -> datetime:
    """Choose symmetric jitter clipped to the half-open work interval."""

    lower = max(anchor.window.start, anchor.base - jitter)
    upper = min(anchor.window.end, anchor.base + jitter)
    return _choose_time(lower, upper, random_source)


def _choose_time(lower: datetime, upper: datetime, random_source: RandomSource) -> datetime:
    lower_utc = lower.astimezone(timezone.utc)
    upper_utc = upper.astimezone(timezone.utc)
    span = max(0.0, (upper_utc - lower_utc).total_seconds())
    if span <= 0:
        return lower
    # Keep the upper bound half-open even for a fake random source returning b.
    offset = min(max(0.0, float(random_source.uniform(0.0, span))), max(0.0, span - 1e-6))
    return (lower_utc + timedelta(seconds=offset)).astimezone(lower.tzinfo)


def schedule_fingerprint(config: CollaborationConfig, target: UsageWindowTargetConfig) -> str:
    days, work_time, interval, jitter = effective_usage_window_schedule(config, target)
    normalized = normalized_usage_window_options(config, target)
    value = {
        "target": target.id,
        "backend": target.backend,
        "model": normalized.get("model", target.model),
        "options": normalized,
        "timezone": config.system.timezone,
        "days": days,
        "work_time": {
            "start": work_time.start.strftime("%H:%M"),
            "end": work_time.end.strftime("%H:%M"),
        },
        "interval_seconds": int(interval.total_seconds()),
        "jitter_seconds": int(jitter.total_seconds()),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def deterministic_session_id(target_id: str, anchor: datetime) -> str:
    return f"uw-{target_id}-{anchor.strftime('%Y%m%d-%H%M')}"


def _eligible_day(day: date, days: list[str]) -> bool:
    return day.weekday() in {DAY_INDEX[name] for name in days}


def _candidate_anchors(
    now: datetime,
    *,
    days: list[str],
    work_time: WorkTimeConfig,
    interval: timedelta,
    zone: tzinfo,
    past_days: int = 8,
    future_days: int = 15,
) -> list[UsageWindowAnchor]:
    local_date = now.astimezone(zone).date()
    result = []
    for offset in range(-past_days, future_days + 1):
        day = local_date + timedelta(days=offset)
        if _eligible_day(day, days):
            result.extend(anchors_for_day(day, work_time, interval, zone))
    result.sort(key=lambda item: item.base.astimezone(timezone.utc))
    return result


def current_work_interval(
    now: datetime,
    *,
    days: list[str],
    work_time: WorkTimeConfig,
    zone: tzinfo,
) -> Optional[WorkInterval]:
    local_date = now.astimezone(zone).date()
    instant = now.astimezone(timezone.utc)
    for day in (local_date - timedelta(days=1), local_date):
        if not _eligible_day(day, days):
            continue
        window = work_interval_for_day(day, work_time, zone)
        if window.start.astimezone(timezone.utc) <= instant < window.end.astimezone(timezone.utc):
            return window
    return None


def _parse_state_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _next_future_jittered_plan(
    anchors: list[UsageWindowAnchor],
    instant: datetime,
    jitter: timedelta,
    random_source: RandomSource,
    *,
    require_unopened_window: bool = False,
) -> tuple[Optional[UsageWindowAnchor], Optional[datetime], int]:
    """Choose a future anchor whose sampled execution time is also future."""

    skipped = 0
    for anchor in anchors:
        if anchor.base.astimezone(timezone.utc) <= instant:
            continue
        earliest = max(anchor.window.start, anchor.base - jitter)
        if require_unopened_window and earliest.astimezone(timezone.utc) <= instant:
            # With untrusted state, an anchor whose jitter window has opened
            # may already have run before state was lost. A fresh random
            # sample later in the same window cannot make it safe.
            skipped += 1
            continue
        planned = jitter_anchor(anchor, jitter, random_source)
        if planned.astimezone(timezone.utc) > instant:
            return anchor, planned, skipped
        # Missing or untrusted state cannot prove that this negatively
        # jittered execution did not already happen. Skip it fail-closed.
        skipped += 1
    return None, None, skipped


def plan_target(
    config: CollaborationConfig,
    target: UsageWindowTargetConfig,
    *,
    now: datetime,
    entry: Optional[Mapping[str, Any]],
    trustworthy: bool,
    random_source: RandomSource,
) -> Tuple[ScheduleDecision, Dict[str, Any]]:
    """Plan one target, including fail-closed restart and bounded catch-up."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    fingerprint = schedule_fingerprint(config, target)
    days, work_time, interval, jitter = effective_usage_window_schedule(config, target)
    zone = resolve_timezone(config.system.timezone)
    anchors = _candidate_anchors(now, days=days, work_time=work_time, interval=interval, zone=zone)
    instant = now.astimezone(timezone.utc)
    current = current_work_interval(now, days=days, work_time=work_time, zone=zone)

    def fresh_entry() -> Dict[str, Any]:
        return {
            "schedule_fingerprint": fingerprint,
            "anchor": None,
            "planned_at": None,
            "attempted_anchor": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_outcome": None,
            "last_session_id": None,
            "consecutive_failures": 0,
            "retry_not_before": None,
            "eligible": True,
            "ineligibility_reason": None,
            "catch_up": False,
        }

    state = dict(entry or {})
    same_schedule = trustworthy and state.get("schedule_fingerprint") == fingerprint
    if not same_schedule:
        previous = state
        state = fresh_entry()
        # Preserve observability only; old schedule state cannot prove a miss.
        for key in (
            "last_attempt_at",
            "last_success_at",
            "last_outcome",
            "last_session_id",
            "consecutive_failures",
            "retry_not_before",
        ):
            if key in previous:
                state[key] = previous[key]
        future, planned, skipped = _next_future_jittered_plan(
            anchors,
            instant,
            jitter,
            random_source,
            require_unopened_window=True,
        )
        if future is None or planned is None:
            return ScheduleDecision("none", skipped=skipped), state
        state.update(anchor=future.base.isoformat(), planned_at=planned.isoformat(), catch_up=False)
        return ScheduleDecision("wait", future, planned, skipped=skipped), state

    anchor_at = _parse_state_datetime(state.get("anchor"))
    planned_at = _parse_state_datetime(state.get("planned_at"))
    attempted_anchor = state.get("attempted_anchor")
    skipped_expired = 0
    if anchor_at is not None and planned_at is not None:
        matching = next(
            (
                anchor
                for anchor in anchors
                if anchor.base.astimezone(timezone.utc) == anchor_at.astimezone(timezone.utc)
            ),
            None,
        )
        if matching is not None and attempted_anchor != state.get("anchor"):
            if planned_at.astimezone(timezone.utc) > instant:
                return ScheduleDecision(
                    "wait", matching, planned_at, catch_up=bool(state.get("catch_up"))
                ), state
            # A persisted catch-up delay has elapsed: run it without rerolling.
            if state.get("catch_up"):
                return ScheduleDecision("attempt", matching, planned_at, catch_up=True), state
            if current is not None:
                current_due = [
                    anchor
                    for anchor in anchors
                    if anchor.window.local_day == current.local_day
                    and anchor.base.astimezone(timezone.utc) <= instant
                ]
                if current_due and current_due[-1].base.astimezone(
                    timezone.utc
                ) > matching.base.astimezone(timezone.utc):
                    latest = current_due[-1]
                    later = next(
                        (
                            anchor
                            for anchor in anchors
                            if anchor.window.local_day == current.local_day
                            and anchor.base.astimezone(timezone.utc) > instant
                        ),
                        None,
                    )
                    upper = current.end if later is None else later.base
                    upper = min(upper, now + jitter)
                    planned = _choose_time(now, upper, random_source)
                    state.update(
                        anchor=latest.base.isoformat(),
                        planned_at=planned.isoformat(),
                        attempted_anchor=None,
                        catch_up=True,
                    )
                    action = "attempt" if planned.astimezone(timezone.utc) <= instant else "wait"
                    return ScheduleDecision(
                        action,
                        latest,
                        planned,
                        catch_up=True,
                        skipped=max(1, len(current_due) - 1),
                    ), state
                # A scheduler wake is inevitably a little later than its
                # requested instant. Keep that normal path distinct from a
                # materially late daemon restart, which receives one freshly
                # jittered catch-up persisted before another sleep.
                if matching.window.local_day == current.local_day:
                    lateness = instant - planned_at.astimezone(timezone.utc)
                    if lateness <= ON_TIME_WAKE_GRACE:
                        return ScheduleDecision("attempt", matching, planned_at), state
                    later = next(
                        (
                            anchor
                            for anchor in anchors
                            if anchor.window.local_day == current.local_day
                            and anchor.base.astimezone(timezone.utc) > instant
                        ),
                        None,
                    )
                    upper = current.end if later is None else later.base
                    upper = min(upper, now + jitter)
                    catch_up_at = _choose_time(now, upper, random_source)
                    state.update(planned_at=catch_up_at.isoformat(), catch_up=True)
                    action = (
                        "attempt" if catch_up_at.astimezone(timezone.utc) <= instant else "wait"
                    )
                    return ScheduleDecision(action, matching, catch_up_at, catch_up=True), state
            # The persisted time passed outside its work interval. Never call
            # it late; fall through and plan a future anchor.
            skipped_expired = 1

    future, planned, skipped_jitter = _next_future_jittered_plan(
        anchors, instant, jitter, random_source
    )
    if future is None or planned is None:
        state.update(anchor=None, planned_at=None, catch_up=False)
        return ScheduleDecision("none", skipped=skipped_expired + skipped_jitter), state
    state.update(anchor=future.base.isoformat(), planned_at=planned.isoformat(), catch_up=False)
    return ScheduleDecision(
        "wait", future, planned, skipped=skipped_expired + skipped_jitter
    ), state


class UsageWindowStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> LoadedUsageWindowState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return LoadedUsageWindowState({}, False)
        if not isinstance(raw, Mapping) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            return LoadedUsageWindowState({}, False)
        targets = raw.get("targets")
        if not isinstance(targets, Mapping):
            return LoadedUsageWindowState({}, False)
        safe = {
            str(key): dict(value)
            for key, value in targets.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }
        return LoadedUsageWindowState(safe, True)

    def save(self, targets: Mapping[str, Mapping[str, Any]]) -> None:
        payload = {"schema_version": STATE_SCHEMA_VERSION, "targets": targets}
        atomic_write_private_text(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        )


InvokeFn = Callable[
    [UsageWindowTargetConfig, UsageWindowAnchor, str],
    Awaitable[Mapping[str, Any]] | Mapping[str, Any],
]
ProbeFn = Callable[[UsageWindowTargetConfig], Awaitable[Optional[str]] | Optional[str]]


class UsageWindowScheduler:
    """One daemon-owned scheduler with independent per-target attempts."""

    def __init__(
        self,
        *,
        config: CollaborationConfig,
        manager: Any,
        paths: GlobalDataPaths,
        logger: Callable[[str], None],
        invoke: Optional[InvokeFn] = None,
        probe: Optional[ProbeFn] = None,
        random_source: Optional[RandomSource] = None,
        now: Optional[Callable[[], datetime]] = None,
        poll_seconds: float = 60.0,
    ) -> None:
        self.config = config
        self.manager = manager
        self.paths = paths
        self.logger = logger
        self.invoke = invoke or self._invoke_session
        self.probe = probe or self._probe_backend
        self.random = random_source or secrets.SystemRandom()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.poll_seconds = poll_seconds
        self.store = UsageWindowStateStore(paths.usage_window_state_path)
        loaded = self.store.load()
        self.targets_state = loaded.targets
        self._state_was_trustworthy = loaded.trustworthy
        self._state_lock = asyncio.Lock()
        self._attempt_tasks: Dict[str, asyncio.Task] = {}
        self._eligibility: Dict[str, tuple[float, Optional[str]]] = {}

    async def run(self) -> None:
        self.paths.usage_window_workdir.mkdir(parents=True, exist_ok=True)
        self.paths.usage_window_workdir.chmod(0o700)
        try:
            while True:
                try:
                    delay = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Persistence/planning failures never cross the paid-call
                    # boundary and must not permanently kill daemon scheduling.
                    self.logger("usage-window scheduler cycle failed")
                    delay = self.poll_seconds
                await asyncio.sleep(max(0.01, min(self.poll_seconds, delay)))
        finally:
            tasks = list(self._attempt_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run_once(self) -> float:
        now = self.now()
        next_delay = self.poll_seconds
        dirty = False
        for target in self.config.usage_windows.targets.values():
            if not target.enabled:
                continue
            # Planning starts from a snapshot, but installing a changed plan
            # must serialize with _attempt's outcome updates.  After the probe
            # await below, mutate only the live entry; never replace it with the
            # pre-await snapshot, which could restore stale "started" state.
            async with self._state_lock:
                entry = self.targets_state.get(target.id)
                decision, planned = plan_target(
                    self.config,
                    target,
                    now=now,
                    entry=entry,
                    trustworthy=self._state_was_trustworthy,
                    random_source=self.random,
                )
                plan_changed = planned != entry
                if plan_changed:
                    self.targets_state[target.id] = planned
                    dirty = True
            reason = await self._eligibility_reason(target, force=decision.action == "attempt")
            eligible = reason is None
            async with self._state_lock:
                live = self.targets_state[target.id]
                if live.get("eligible") != eligible or live.get("ineligibility_reason") != reason:
                    live["eligible"] = eligible
                    live["ineligibility_reason"] = reason
                    dirty = True
                if decision.action == "attempt" and reason is not None:
                    assert decision.anchor is not None
                    anchor_text = decision.anchor.base.isoformat()
                    if (
                        live.get("attempted_anchor") != anchor_text
                        or live.get("last_outcome") != reason
                    ):
                        live["attempted_anchor"] = anchor_text
                        live["last_outcome"] = reason
                        dirty = True
            if decision.skipped:
                self.logger(
                    f"usage-window target={target.id} backend={target.backend} model={target.model} "
                    f"skipped={decision.skipped} missed anchor(s)"
                )
            if decision.action == "wait" and decision.planned_at is not None:
                seconds = (
                    decision.planned_at.astimezone(timezone.utc) - now.astimezone(timezone.utc)
                ).total_seconds()
                next_delay = min(next_delay, max(0.01, seconds))
                if plan_changed:
                    self.logger(
                        f"usage-window target={target.id} backend={target.backend} model={target.model} "
                        f"planned={decision.planned_at.isoformat()}"
                    )
            elif decision.action == "attempt" and target.id not in self._attempt_tasks:
                if reason is None and decision.anchor is not None:
                    task = asyncio.create_task(
                        self._attempt(target, decision),
                        name=f"agent-collab-usage-window-{target.id}",
                    )
                    self._attempt_tasks[target.id] = task
                    task.add_done_callback(
                        lambda done, key=target.id: self._attempt_done(key, done)
                    )
                else:
                    self.logger(
                        f"usage-window target={target.id} backend={target.backend} model={target.model} "
                        f"ineligible={reason} anchor skipped"
                    )
        if dirty:
            await self._save_state()
        # Once a freshly initialized/corrupt file has been safely replanned and
        # persisted, subsequent wakes may trust the state created this run.
        self._state_was_trustworthy = True
        return next_delay

    def _attempt_done(self, target_id: str, task: asyncio.Task) -> None:
        self._attempt_tasks.pop(target_id, None)
        if task.cancelled():
            return
        if task.exception() is not None:
            self.logger(f"usage-window target={target_id} attempt bookkeeping failed")

    async def _eligibility_reason(
        self, target: UsageWindowTargetConfig, *, force: bool = False
    ) -> Optional[str]:
        policy = backend_policy(self.config, target.backend)
        if not policy.enabled:
            return "backend_disabled"
        monotonic = asyncio.get_running_loop().time()
        cached = self._eligibility.get(target.id)
        if not force and cached is not None and monotonic - cached[0] < 60.0:
            return cached[1]
        try:
            result = self.probe(target)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception:
            result = "backend_unavailable"
        reason = str(result) if result else None
        self._eligibility[target.id] = (monotonic, reason)
        return reason

    async def _probe_backend(self, target: UsageWindowTargetConfig) -> Optional[str]:
        from .backends.base import HEALTH_UNAVAILABLE
        from . import backends as backend_registry

        agent_type, backend_id = split_canonical_backend(target.backend)
        backend = backend_registry.get_backend(agent_type, backend_id or "")
        agent = self.config.agents.get(target.backend)
        configured_probe = getattr(backend, "probe_for_agent", None)
        if agent is not None and callable(configured_probe):
            health = await asyncio.to_thread(configured_probe, agent)
        else:
            health = await asyncio.to_thread(backend_registry.HEALTH.health, backend, fresh=True)
        return "backend_unavailable" if health.status == HEALTH_UNAVAILABLE else None

    async def _attempt(self, target: UsageWindowTargetConfig, decision: ScheduleDecision) -> None:
        assert decision.anchor is not None
        anchor_text = decision.anchor.base.isoformat()
        session_id = deterministic_session_id(target.id, decision.anchor.base)
        started = self.now()
        async with self._state_lock:
            entry = self.targets_state[target.id]
            if entry.get("attempted_anchor") == anchor_text:
                return
            retry_at = _parse_state_datetime(entry.get("retry_not_before"))
            if retry_at is not None and started.astimezone(timezone.utc) < retry_at.astimezone(
                timezone.utc
            ):
                self.logger(
                    f"usage-window target={target.id} retry guidance defers anchor until "
                    f"{retry_at.isoformat()}"
                )
                entry["attempted_anchor"] = anchor_text
                entry["last_outcome"] = "retry_not_before"
                await self._save_state_locked()
                return
            # Mark attempted before the paid boundary. A crash now fails closed.
            entry.update(
                attempted_anchor=anchor_text,
                last_attempt_at=started.isoformat(),
                last_outcome="started",
                last_session_id=session_id,
                catch_up=decision.catch_up,
            )
            await self._save_state_locked()
        mode = "caught_up" if decision.catch_up else "on_time"
        self.logger(
            f"usage-window target={target.id} backend={target.backend} model={target.model} "
            f"attempt={mode} session={session_id}"
        )
        try:
            result = self.invoke(target, decision.anchor, session_id)
            if inspect.isawaitable(result):
                result = await result
            outcome = str(result.get("outcome") or "failed")
            retry_after = result.get("retry_after_seconds")
        except asyncio.CancelledError:
            outcome = "cancelled"
            retry_after = None
            cancelled = True
        except Exception:
            outcome = "failed"
            retry_after = None
            cancelled = False
        else:
            cancelled = False
        finished = self.now()
        async with self._state_lock:
            entry = self.targets_state[target.id]
            entry["last_outcome"] = outcome
            entry["last_session_id"] = session_id
            if outcome == "completed":
                entry["last_success_at"] = finished.isoformat()
                entry["consecutive_failures"] = 0
            else:
                entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
            if (
                isinstance(retry_after, (int, float))
                and not isinstance(retry_after, bool)
                and retry_after > 0
            ):
                entry["retry_not_before"] = (
                    finished + timedelta(seconds=float(retry_after))
                ).isoformat()
            else:
                entry["retry_not_before"] = None
            await self._save_state_locked()
        self.logger(
            f"usage-window target={target.id} backend={target.backend} model={target.model} "
            f"session={session_id} outcome={outcome}"
        )
        if cancelled:
            raise asyncio.CancelledError

    async def _invoke_session(
        self, target: UsageWindowTargetConfig, anchor: UsageWindowAnchor, session_id: str
    ) -> Mapping[str, Any]:
        from .daemon import StartSessionRequest

        options = dict(target.options)
        options["model"] = target.model
        timeout_seconds = self.config.backends[target.backend].timeout or 900
        request = StartSessionRequest(
            task=USAGE_WINDOW_PROMPT,
            workflow="usage-window",
            workdir=self.paths.usage_window_workdir,
            max_turns=1,
            timeout=timeout_seconds,
            session_id=session_id,
            backend_options={target.backend: options},
            members={"claude_cli": target.backend},
            interactive=False,
            internal_workdir_exempt=True,
        )
        started = False
        try:
            await self.manager.start_session(request)
            started = True

            async def wait_terminal() -> Any:
                while True:
                    state = self.manager.get_session(session_id)
                    if state.status in TERMINAL_STATUSES:
                        return state
                    await asyncio.sleep(0.1)

            try:
                state = await asyncio.wait_for(wait_terminal(), timeout=timeout_seconds + 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await self.manager.stop_session(session_id)
                return {"outcome": "timed_out"}
        except asyncio.CancelledError:
            if started:
                with contextlib.suppress(Exception):
                    await self.manager.stop_session(session_id)
            raise
        records = list(state.turn_outcomes or [])
        completed = (
            state.status == DONE and len(records) == 1 and records[0].get("outcome") == "completed"
        )
        result: Dict[str, Any] = {"outcome": "completed" if completed else _session_outcome(state)}
        retry_values = [
            record.get("retry_after_seconds")
            for record in records
            if isinstance(record.get("retry_after_seconds"), (int, float))
            and not isinstance(record.get("retry_after_seconds"), bool)
        ]
        if retry_values:
            result["retry_after_seconds"] = max(retry_values)
        return result

    async def _save_state(self) -> None:
        async with self._state_lock:
            await self._save_state_locked()

    async def _save_state_locked(self) -> None:
        snapshot = {key: dict(value) for key, value in self.targets_state.items()}
        await asyncio.to_thread(self.store.save, snapshot)


def _session_outcome(state: Any) -> str:
    failure = getattr(state, "failure", None)
    if isinstance(failure, Mapping) and isinstance(failure.get("code"), str):
        return failure["code"]
    records = getattr(state, "turn_outcomes", None) or []
    if records and isinstance(records[-1], Mapping):
        return str(records[-1].get("code") or records[-1].get("outcome") or "failed")
    return str(getattr(state, "status", None) or "failed")


def enabled_usage_window_targets(config: CollaborationConfig) -> list[UsageWindowTargetConfig]:
    return [target for target in config.usage_windows.targets.values() if target.enabled]


def usage_window_status(config: CollaborationConfig, state_path: Path) -> Dict[str, Any]:
    """Build a read-only status view from current config plus private state."""

    loaded = UsageWindowStateStore(state_path).load()
    enabled = []
    disabled = 0
    for target in config.usage_windows.targets.values():
        if not target.enabled:
            disabled += 1
            continue
        days, work_time, interval, jitter = effective_usage_window_schedule(config, target)
        fingerprint = schedule_fingerprint(config, target)
        entry = loaded.targets.get(target.id, {}) if loaded.trustworthy else {}
        mismatch = not entry or entry.get("schedule_fingerprint") != fingerprint
        policy_enabled = backend_policy(config, target.backend).enabled
        enabled.append(
            {
                "id": target.id,
                "backend": target.backend,
                "model": target.model,
                "days": days,
                "work_time": (
                    f"{work_time.start.strftime('%H:%M')}-{work_time.end.strftime('%H:%M')}"
                ),
                "timezone": config.system.timezone,
                "interval": _format_duration(interval),
                "jitter": _format_duration(jitter),
                "eligible": bool(entry.get("eligible", policy_enabled)) and policy_enabled,
                "ineligibility_reason": (
                    "backend_disabled" if not policy_enabled else entry.get("ineligibility_reason")
                ),
                "pending_restart": mismatch,
                "next_planned_at": (
                    None
                    if mismatch or entry.get("attempted_anchor") == entry.get("anchor")
                    else entry.get("planned_at")
                ),
                "last_attempt_at": entry.get("last_attempt_at"),
                "last_success_at": entry.get("last_success_at"),
                "last_outcome": entry.get("last_outcome"),
                "last_session_id": entry.get("last_session_id"),
            }
        )
    return {"disabled_count": disabled, "enabled": enabled, "state_trustworthy": loaded.trustworthy}


def _format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"
