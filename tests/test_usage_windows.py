import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from agent_collab.config import (
    ConfigError,
    WorkTimeConfig,
    builtin_config,
    load_toml_text,
    merge_config_data,
    validate_config,
)
from agent_collab.config_migrations import migrate_config_data
from agent_collab.daemon import SessionManager, SessionRequestError, StartSessionRequest
from agent_collab.paths import AgentCollabHome, GlobalDataPaths
from agent_collab.server_http import AgentCollabHttpServer, _load_daemon_policy
from agent_collab.usage_windows import (
    USAGE_WINDOW_PROMPT,
    UsageWindowScheduler,
    UsageWindowStateStore,
    anchors_for_day,
    plan_target,
    resolve_local_datetime,
    resolve_timezone,
    schedule_fingerprint,
    usage_window_status,
)


class _MinimumRandom:
    def uniform(self, lower, _upper):
        return lower


class _MaximumRandom:
    def uniform(self, _lower, upper):
        return upper


class UsageWindowConfigTests(unittest.TestCase):
    def test_packaged_matrix_has_one_disabled_target_per_real_backend(self):
        config = builtin_config()
        self.assertEqual(len(config.usage_windows.targets), 8)
        self.assertTrue(all(not target.enabled for target in config.usage_windows.targets.values()))
        self.assertEqual(
            {target.backend for target in config.usage_windows.targets.values()},
            set(config.backends),
        )
        self.assertIn("usage-window", config.workflows)

    def test_one_line_override_inherits_target(self):
        config = builtin_config()
        merge_config_data(
            config,
            {"usage_windows": {"targets": {"codex_cli_luna": {"enabled": True}}}},
        )
        validate_config(config)
        enabled = [target for target in config.usage_windows.targets.values() if target.enabled]
        self.assertEqual(len(enabled), 1)
        self.assertEqual((enabled[0].backend, enabled[0].model), ("codex_cli", "gpt-5.6-luna"))
        self.assertEqual(enabled[0].options["sandbox"], "read-only")

    def test_second_model_is_allowed_but_duplicate_enabled_pair_is_not(self):
        config = builtin_config()
        merge_config_data(
            config,
            {
                "usage_windows": {
                    "targets": {
                        "one": {
                            "enabled": True,
                            "backend": "codex_cli",
                            "model": "one",
                        },
                        "two": {
                            "enabled": True,
                            "backend": "codex_cli",
                            "model": "two",
                        },
                    }
                }
            },
        )
        validate_config(config)
        config.usage_windows.targets["two"].model = "one"
        with self.assertRaisesRegex(ConfigError, "duplicates enabled target"):
            validate_config(config)

    def test_validation_rejects_short_interval_and_model_in_options(self):
        config = builtin_config()
        with self.assertRaisesRegex(ConfigError, "at least 15m"):
            merge_config_data(config, {"usage_windows": {"interval": "14m"}})
            validate_config(config)
        config = builtin_config()
        with self.assertRaisesRegex(ConfigError, "options.model is not allowed"):
            merge_config_data(
                config,
                {"usage_windows": {"targets": {"codex_cli_luna": {"options": {"model": "other"}}}}},
            )

    def test_per_target_schedule_overrides_are_independent(self):
        config = builtin_config()
        merge_config_data(
            config,
            {
                "usage_windows": {
                    "targets": {
                        "claude_cli_sonnet": {"work_time": {"start": "16:00", "end": "22:00"}},
                        "codex_cli_luna": {
                            "work_time": {"start": "08:00", "end": "14:00"},
                            "interval": "3h",
                            "jitter": "0m",
                        },
                    }
                }
            },
        )
        claude = config.usage_windows.targets["claude_cli_sonnet"]
        codex = config.usage_windows.targets["codex_cli_luna"]
        self.assertEqual((claude.work_time.start.hour, claude.work_time.end.hour), (16, 22))
        self.assertEqual((codex.work_time.start.hour, codex.work_time.end.hour), (8, 14))
        self.assertEqual(codex.interval, timedelta(hours=3))
        self.assertEqual(codex.jitter, timedelta(0))

    def test_fallback_toml_parser_handles_inline_work_time(self):
        parsed = load_toml_text('[usage_windows]\nwork_time = { start = "08:00", end = "18:00" }\n')
        self.assertEqual(parsed["usage_windows"]["work_time"]["start"], "08:00")

    def test_project_system_and_usage_windows_are_stripped(self):
        warnings = []
        result = migrate_config_data(
            {
                "schema_version": 9,
                "system": {"timezone": "UTC"},
                "usage_windows": {"targets": {}},
            },
            source="project.toml",
            scope="project",
            warnings=warnings,
        )
        self.assertNotIn("system", result)
        self.assertNotIn("usage_windows", result)
        self.assertEqual({item["path"] for item in warnings}, {"system", "usage_windows"})

    def test_daemon_policy_loads_once_and_fails_closed_on_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config_path = home / "config.toml"
            config_path.write_text(
                "schema_version = 9\n[usage_windows.targets.codex_cli_luna]\nenabled = true\n",
                encoding="utf-8",
            )
            loaded = _load_daemon_policy(home)
            self.assertTrue(loaded.usage_windows.targets["codex_cli_luna"].enabled)
            config_path.write_text(
                "schema_version = 9\n[system]\ntimezone = 42\n", encoding="utf-8"
            )
            failed = _load_daemon_policy(home)
            self.assertEqual(failed.sessions.retention_days, 0)
            self.assertEqual(failed.usage_windows.targets, {})


class UsageWindowPlannerTests(unittest.TestCase):
    def setUp(self):
        self.random = _MinimumRandom()
        self.zone = ZoneInfo("Europe/Helsinki")

    def test_daily_and_overnight_anchors_exclude_end(self):
        ordinary = anchors_for_day(
            date(2026, 7, 15),
            WorkTimeConfig(time(9), time(17)),
            timedelta(hours=5),
            self.zone,
        )
        overnight = anchors_for_day(
            date(2026, 7, 15),
            WorkTimeConfig(time(16), time(22)),
            timedelta(hours=5),
            self.zone,
        )
        self.assertEqual([item.base.hour for item in ordinary], [9, 14])
        self.assertEqual([item.base.hour for item in overnight], [16, 21])

    def test_dst_gap_advances_and_fold_is_deterministic(self):
        gap = resolve_local_datetime(datetime(2026, 3, 29, 3, 30), self.zone)
        fold = resolve_local_datetime(datetime(2026, 10, 25, 3, 30), self.zone)
        self.assertEqual((gap.hour, gap.minute), (4, 0))
        self.assertEqual(fold.fold, 0)
        gap_anchors = anchors_for_day(
            date(2026, 3, 29),
            WorkTimeConfig(time(2, 30), time(5)),
            timedelta(minutes=30),
            self.zone,
        )
        instants = [item.base.astimezone(timezone.utc) for item in gap_anchors]
        self.assertEqual(len(instants), len(set(instants)))

    def test_local_timezone_uses_discovered_iana_rules(self):
        with (
            mock.patch.dict(os.environ, {"TZ": ""}),
            mock.patch(
                "agent_collab.usage_windows._local_zone_names",
                return_value=["Europe/Helsinki"],
            ),
        ):
            del os.environ["TZ"]
            zone = resolve_timezone("local")
        self.assertIsInstance(zone, ZoneInfo)
        self.assertEqual(zone.key, "Europe/Helsinki")
        self.assertNotEqual(
            datetime(2026, 1, 15, tzinfo=zone).utcoffset(),
            datetime(2026, 7, 15, tzinfo=zone).utcoffset(),
        )

    def test_local_timezone_honors_absolute_tz_override_before_host_zone(self):
        zone_path = Path("/usr/share/zoneinfo/America/New_York")
        if not zone_path.is_file():
            self.skipTest("system zoneinfo database is unavailable")
        with (
            mock.patch.dict(os.environ, {"TZ": f":{zone_path}"}),
            mock.patch(
                "agent_collab.usage_windows._local_zone_names",
                side_effect=AssertionError("host timezone must not override TZ"),
            ),
        ):
            zone = resolve_timezone("local")
        self.assertEqual(datetime(2026, 1, 15, tzinfo=zone).utcoffset(), timedelta(hours=-5))
        self.assertEqual(datetime(2026, 7, 15, tzinfo=zone).utcoffset(), timedelta(hours=-4))

    def test_new_target_plans_future_and_restart_resumes_jitter(self):
        config = builtin_config()
        target = config.usage_windows.targets["codex_cli_luna"]
        now = datetime(2026, 7, 15, 8, 0, tzinfo=self.zone)
        decision, state = plan_target(
            config,
            target,
            now=now,
            entry=None,
            trustworthy=False,
            random_source=self.random,
        )
        resumed, same_state = plan_target(
            config,
            target,
            now=now + timedelta(minutes=30),
            entry=state,
            trustworthy=True,
            random_source=self.random,
        )
        self.assertEqual(decision.planned_at, resumed.planned_at)
        self.assertEqual(state, same_state)

    def test_slightly_late_wake_uses_persisted_plan_without_reroll(self):
        config = builtin_config()
        config.system.timezone = "Europe/Helsinki"
        target = config.usage_windows.targets["codex_cli_luna"]
        _decision, state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 8, 0, tzinfo=self.zone),
            entry=None,
            trustworthy=False,
            random_source=self.random,
        )
        due, unchanged = plan_target(
            config,
            target,
            now=datetime.fromisoformat(state["planned_at"]) + timedelta(microseconds=1),
            entry=state,
            trustworthy=True,
            random_source=self.random,
        )
        self.assertEqual(due.action, "attempt")
        self.assertFalse(due.catch_up)
        self.assertEqual(unchanged, state)

    def test_untrusted_state_skips_an_anchor_whose_jitter_window_opened(self):
        config = builtin_config()
        config.system.timezone = "Europe/Helsinki"
        target = config.usage_windows.targets["codex_cli_luna"]
        now = datetime(2026, 7, 15, 13, 58, tzinfo=self.zone)
        for random_source in (_MinimumRandom(), _MaximumRandom()):
            with self.subTest(random_source=type(random_source).__name__):
                decision, state = plan_target(
                    config,
                    target,
                    now=now,
                    entry={"schedule_fingerprint": "corrupt"},
                    trustworthy=False,
                    random_source=random_source,
                )
                self.assertEqual(decision.action, "wait")
                self.assertFalse(decision.catch_up)
                self.assertEqual(decision.skipped, 1)
                self.assertGreater(decision.planned_at, now)
                next_cycle, _state = plan_target(
                    config,
                    target,
                    now=now + timedelta(seconds=1),
                    entry=state,
                    trustworthy=True,
                    random_source=random_source,
                )
                self.assertEqual(next_cycle.action, "wait")
                self.assertFalse(next_cycle.catch_up)

    def test_missed_window_catches_latest_once_and_skips_older(self):
        config = builtin_config()
        config.system.timezone = "Europe/Helsinki"
        target = config.usage_windows.targets["codex_cli_luna"]
        initial, state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 8, 0, tzinfo=self.zone),
            entry=None,
            trustworthy=False,
            random_source=self.random,
        )
        self.assertEqual(initial.anchor.base.hour, 9)
        caught, state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 15, 0, tzinfo=self.zone),
            entry=state,
            trustworthy=True,
            random_source=self.random,
        )
        self.assertTrue(caught.catch_up)
        self.assertEqual(caught.anchor.base.hour, 14)
        self.assertEqual(caught.skipped, 1)
        due, _state = plan_target(
            config,
            target,
            now=caught.planned_at,
            entry=state,
            trustworthy=True,
            random_source=self.random,
        )
        self.assertEqual(due.action, "attempt")

    def test_changed_fingerprint_never_catches_up(self):
        config = builtin_config()
        target = config.usage_windows.targets["codex_cli_luna"]
        _decision, state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 8, 0, tzinfo=self.zone),
            entry=None,
            trustworthy=False,
            random_source=self.random,
        )
        target.interval = timedelta(hours=4)
        replanned, new_state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 10, 0, tzinfo=self.zone),
            entry=state,
            trustworthy=True,
            random_source=self.random,
        )
        self.assertFalse(replanned.catch_up)
        self.assertGreater(replanned.anchor.base, datetime(2026, 7, 15, 10, 0, tzinfo=self.zone))
        self.assertEqual(new_state["schedule_fingerprint"], schedule_fingerprint(config, target))

    def test_attempted_anchor_does_not_prove_a_later_anchor_was_missed(self):
        config = builtin_config()
        target = config.usage_windows.targets["codex_cli_luna"]
        _decision, state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 8, 0, tzinfo=self.zone),
            entry=None,
            trustworthy=False,
            random_source=self.random,
        )
        state["attempted_anchor"] = state["anchor"]
        state["last_outcome"] = "failed"
        replanned, _state = plan_target(
            config,
            target,
            now=datetime(2026, 7, 15, 15, 0, tzinfo=self.zone),
            entry=state,
            trustworthy=True,
            random_source=self.random,
        )
        self.assertFalse(replanned.catch_up)
        self.assertGreater(replanned.anchor.base, datetime(2026, 7, 15, 15, 0, tzinfo=self.zone))


class UsageWindowStateTests(unittest.TestCase):
    def test_store_is_private_and_future_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "daemon" / "usage-window-state.json"
            store = UsageWindowStateStore(path)
            store.save({"one": {"last_outcome": "completed"}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(store.load().trustworthy)
            path.write_text(json.dumps({"schema_version": 2, "targets": {}}), encoding="utf-8")
            self.assertFalse(store.load().trustworthy)

    def test_tmp_parent_and_usage_workdir_are_owner_only(self):
        with tempfile.TemporaryDirectory() as temp:
            home = AgentCollabHome(Path(temp), Path(temp) / "config.toml")
            paths = GlobalDataPaths.from_home(home)
            paths.ensure_dirs()
            paths.usage_window_workdir.mkdir(mode=0o700)
            paths.usage_window_workdir.chmod(0o700)
            self.assertEqual(paths.tmp_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.usage_window_workdir.stat().st_mode & 0o777, 0o700)

    def test_status_marks_fingerprint_mismatch_pending_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            config = builtin_config()
            target = config.usage_windows.targets["codex_cli_luna"]
            target.enabled = True
            path = Path(temp) / "state.json"
            UsageWindowStateStore(path).save(
                {target.id: {"schedule_fingerprint": "old", "planned_at": "secret-no"}}
            )
            status = usage_window_status(config, path)["enabled"][0]
            self.assertTrue(status["pending_restart"])
            self.assertIsNone(status["next_planned_at"])


class UsageWindowSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_targets_disabled_starts_no_scheduler_task(self):
        server = AgentCollabHttpServer(manager=SessionManager(), daemon_config=builtin_config())
        self.assertIsNone(server.start_usage_window_task())

    async def test_scheduler_marks_attempt_before_one_injected_invocation(self):
        with tempfile.TemporaryDirectory() as temp:
            home = AgentCollabHome(Path(temp), Path(temp) / "config.toml")
            paths = GlobalDataPaths.from_home(home)
            paths.ensure_dirs()
            config = builtin_config()
            target = config.usage_windows.targets["codex_cli_luna"]
            target.enabled = True
            clock = [datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)]
            calls = []

            async def invoke(selected, anchor, session_id):
                calls.append((selected.id, anchor.base, session_id))
                return {"outcome": "completed"}

            scheduler = UsageWindowScheduler(
                config=config,
                manager=object(),
                paths=paths,
                logger=lambda _message: None,
                invoke=invoke,
                probe=lambda _target: None,
                random_source=self.random if hasattr(self, "random") else _MinimumRandom(),
                now=lambda: clock[0],
            )
            await scheduler.run_once()
            entry = scheduler.targets_state[target.id]
            clock[0] = datetime.fromisoformat(entry["planned_at"])
            await scheduler.run_once()
            await asyncio.gather(*scheduler._attempt_tasks.values())
            self.assertEqual(len(calls), 1)
            stored = UsageWindowStateStore(paths.usage_window_state_path).load()
            self.assertEqual(stored.targets[target.id]["last_outcome"], "completed")

    async def test_unavailable_backend_persists_skipped_anchor_without_invocation(self):
        with tempfile.TemporaryDirectory() as temp:
            home = AgentCollabHome(Path(temp), Path(temp) / "config.toml")
            paths = GlobalDataPaths.from_home(home)
            paths.ensure_dirs()
            config = builtin_config()
            target = config.usage_windows.targets["codex_cli_luna"]
            target.enabled = True
            clock = [datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)]
            calls = []
            scheduler = UsageWindowScheduler(
                config=config,
                manager=object(),
                paths=paths,
                logger=lambda _message: None,
                invoke=lambda *_args: calls.append(True),
                probe=lambda _target: "backend_unavailable",
                random_source=_MinimumRandom(),
                now=lambda: clock[0],
            )
            await scheduler.run_once()
            clock[0] = datetime.fromisoformat(scheduler.targets_state[target.id]["planned_at"])
            await scheduler.run_once()
            self.assertEqual(calls, [])
            self.assertEqual(
                scheduler.targets_state[target.id]["last_outcome"], "backend_unavailable"
            )
            stored = UsageWindowStateStore(paths.usage_window_state_path).load()
            persisted = stored.targets[target.id]
            self.assertEqual(persisted["attempted_anchor"], persisted["anchor"])
            self.assertEqual(persisted["last_outcome"], "backend_unavailable")

            # A restart after the backend recovers must not reopen the skipped
            # anchor as a paid catch-up.
            clock[0] += timedelta(seconds=1)
            restarted = UsageWindowScheduler(
                config=config,
                manager=object(),
                paths=paths,
                logger=lambda _message: None,
                invoke=lambda *_args: calls.append(True),
                probe=lambda _target: None,
                random_source=_MinimumRandom(),
                now=lambda: clock[0],
            )
            await restarted.run_once()
            self.assertEqual(calls, [])

    async def test_overlapping_cycle_cannot_restore_stale_attempt_outcome(self):
        with tempfile.TemporaryDirectory() as temp:
            home = AgentCollabHome(Path(temp), Path(temp) / "config.toml")
            paths = GlobalDataPaths.from_home(home)
            paths.ensure_dirs()
            config = builtin_config()
            target = config.usage_windows.targets["codex_cli_luna"]
            target.enabled = True
            clock = [datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)]
            invoke_started = asyncio.Event()
            finish_invoke = asyncio.Event()
            probe_started = asyncio.Event()
            release_probe = asyncio.Event()
            probe_calls = 0

            async def invoke(_selected, _anchor, _session_id):
                invoke_started.set()
                await finish_invoke.wait()
                return {"outcome": "completed"}

            async def probe(_target):
                nonlocal probe_calls
                probe_calls += 1
                if probe_calls == 3:
                    probe_started.set()
                    await release_probe.wait()
                return None

            scheduler = UsageWindowScheduler(
                config=config,
                manager=object(),
                paths=paths,
                logger=lambda _message: None,
                invoke=invoke,
                probe=probe,
                random_source=_MinimumRandom(),
                now=lambda: clock[0],
            )
            await scheduler.run_once()
            clock[0] = datetime.fromisoformat(scheduler.targets_state[target.id]["planned_at"])
            await scheduler.run_once()
            await invoke_started.wait()

            # The negative jitter means the anchor base is still in the future.
            # A later unchanged planning cycle takes a snapshot, then blocks in
            # its probe while the in-flight attempt completes.
            scheduler._eligibility.clear()
            clock[0] += timedelta(minutes=1)
            overlapping = asyncio.create_task(scheduler.run_once())
            await probe_started.wait()
            finish_invoke.set()
            await asyncio.gather(*scheduler._attempt_tasks.values())
            release_probe.set()
            await overlapping

            entry = scheduler.targets_state[target.id]
            self.assertEqual(entry["last_outcome"], "completed")
            self.assertIsNotNone(entry["last_success_at"])

            # Force a later dirty plan/save and prove the completed outcome is
            # still what restart-safe storage contains.
            clock[0] = datetime.fromisoformat(entry["anchor"]) + timedelta(minutes=1)
            await scheduler.run_once()
            stored = UsageWindowStateStore(paths.usage_window_state_path).load()
            self.assertEqual(stored.targets[target.id]["last_outcome"], "completed")
            self.assertIsNotNone(stored.targets[target.id]["last_success_at"])


class UsageWindowInvocationBoundaryTests(unittest.TestCase):
    def test_internal_workdir_exemption_is_not_available_to_external_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root / "home"
            allowed = root / "allowed"
            usage_workdir = home / "data" / "tmp" / "usage-windows"
            home.mkdir()
            allowed.mkdir()
            usage_workdir.mkdir(parents=True)
            (home / "config.toml").write_text(
                f"schema_version = 9\n[workdir]\nrestrict_workdir_roots = [{str(allowed)!r}]\n",
                encoding="utf-8",
            )
            request = StartSessionRequest(
                task="minimal",
                workflow="usage-window",
                workdir=usage_workdir,
                max_turns=1,
                dry_run=True,
                members={"claude_cli": "codex_cli"},
                backend_options={"codex_cli": {"model": "gpt-5.6-luna"}},
            )
            manager = SessionManager()
            with mock.patch.dict("os.environ", {"AGENT_COLLAB_HOME": str(home)}):
                with self.assertRaises(SessionRequestError):
                    manager._prepare_session_start(request)
                request.internal_workdir_exempt = True
                prepared = manager._prepare_session_start(request)
            self.assertEqual(prepared.workdir, usage_workdir)


class UsageWindowVisibleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invocation_uses_fixed_one_turn_visible_session(self):
        with tempfile.TemporaryDirectory() as temp:
            home = AgentCollabHome(Path(temp), Path(temp) / "config.toml")
            paths = GlobalDataPaths.from_home(home)
            paths.ensure_dirs()
            paths.usage_window_workdir.mkdir(mode=0o700)
            config = builtin_config()
            target = config.usage_windows.targets["codex_cli_luna"]

            class Manager:
                request = None

                async def start_session(self, request):
                    self.request = request

                def get_session(self, _session_id):
                    return SimpleNamespace(
                        status="done",
                        turn_outcomes=[{"outcome": "completed"}],
                        failure=None,
                    )

            manager = Manager()
            scheduler = UsageWindowScheduler(
                config=config,
                manager=manager,
                paths=paths,
                logger=lambda _message: None,
            )
            anchor = anchors_for_day(
                date(2026, 7, 15),
                config.usage_windows.work_time,
                config.usage_windows.interval,
                timezone.utc,
            )[0]
            result = await scheduler._invoke_session(target, anchor, "uw-test-20260715-0900")
            request = manager.request
            self.assertEqual(result["outcome"], "completed")
            self.assertEqual(request.task, USAGE_WINDOW_PROMPT)
            self.assertEqual(request.workflow, "usage-window")
            self.assertEqual(request.max_turns, 1)
            self.assertFalse(request.interactive)
            self.assertTrue(request.internal_workdir_exempt)
            self.assertEqual(Path(request.workdir), paths.usage_window_workdir)
            self.assertEqual(request.members, {"claude_cli": "codex_cli"})
            self.assertEqual(request.backend_options["codex_cli"]["sandbox"], "read-only")
