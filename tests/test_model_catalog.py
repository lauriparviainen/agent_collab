"""Hermetic tests for Phase 3 model-catalog wiring (#45).

Every path runs against an injected cache directory, fake CLI runner or
discoverer, fake clock, and fake version resolver — nothing touches real CLIs,
the network, or the user's agent-collab home.
"""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from agent_collab.backends.base import BackendHealth
from agent_collab.backends.common.model_discovery import (
    CliResult,
    ModelCatalogObservation,
    compute_source_fingerprint,
)
from agent_collab.config import AgentConfig, builtin_config
from agent_collab.daemon import (
    SessionManager,
    StartSessionRequest,
    _model_catalog_refresh_wanted,
)
from agent_collab.model_catalog import (
    CONFIGURED_DEFAULT_NOT_IN_CATALOG,
    MODEL_DISCOVERY_FAILED,
    CatalogView,
    ModelCatalogRefresher,
    ModelCatalogService,
    configured_default_warning,
    merge_model_suggestions,
    run_install_discovery,
    start_catalog_warnings,
)
from agent_collab.options import describe_options
from agent_collab.paths import GlobalDataPaths

AGY_OUTPUT = "gemini-3.6-flash-high\ngemini-3.6-flash-medium\ngemini-3.5-flash-low\n"
GROK_OUTPUT = "Available models:\n  * grok-4.5 (default)\n  * grok-composer-2.5-fast\n"
NOW = "2026-07-22T01:00:00+00:00"
RECENT = "2026-07-22T00:30:00+00:00"
OLD = "2026-07-20T00:00:00+00:00"  # more than the 24h TTL before NOW


def _clock(value=NOW):
    return lambda: value


def _agent(command="agy"):
    return SimpleNamespace(command=command, args=[], env={}, backend_config={})


def _observation(
    canonical,
    fingerprint,
    models,
    *,
    status="ok",
    complete=True,
    checked=RECENT,
    reason_code=None,
):
    return ModelCatalogObservation(
        backend_id=canonical,
        status=status,
        models=tuple(models),
        source="cli",
        complete=complete,
        checked_at=checked,
        last_attempt_at=checked,
        source_fingerprint=fingerprint,
        last_success_at=checked if status == "ok" else None,
        reason_code=reason_code,
    )


class _ScriptedRunner:
    """Fake async CLI runner keyed by binary; records calls."""

    def __init__(self, outputs=None, *, raises=None):
        self.outputs = outputs or {}
        self.raises = raises
        self.calls = []

    async def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        if self.raises is not None:
            raise self.raises
        text = self.outputs.get(argv[0])
        if text is None:
            return CliResult(returncode=1, stdout="", stderr="boom")
        return CliResult(returncode=0, stdout=text)


def _service(tmp, *, runner=None, now=None, monotonic=None, min_fresh_interval=60.0):
    return ModelCatalogService(
        cache_dir=Path(tmp),
        runner=runner,
        now=now or _clock(),
        monotonic=monotonic or (lambda: 0.0),
        min_fresh_interval=min_fresh_interval,
    )


class ServiceServeTests(unittest.TestCase):
    def test_none_and_cached_never_probe(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner)
            for refresh in ("none", "cached"):
                view = service.serve("antigravity_cli", _agent(), version="1.0", refresh=refresh)
                self.assertEqual(view.served_from, "static")
                self.assertFalse(view.probed)
                self.assertEqual(view.models, ())
        self.assertEqual(runner.calls, [])

    def test_fresh_probes_and_serves_fresh_catalog(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner)
            view = service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            self.assertEqual(view.served_from, "fresh_probe")
            self.assertTrue(view.probed)
            self.assertTrue(view.authoritative)
            self.assertEqual(view.models[0], "gemini-3.6-flash-high")
            path = Path(tmp) / "models_antigravity_cli.json"
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(tmp).stat().st_mode & 0o777, 0o700)
        self.assertEqual(runner.calls, [("agy", "models")])

    def test_fresh_min_interval_gates_repeated_probes(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner, monotonic=lambda: 100.0)
            first = service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            second = service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            self.assertEqual(first.served_from, "fresh_probe")
            self.assertEqual(second.probe_gate, "min_probe_interval")
            self.assertFalse(second.probed)
            # The gated request still serves the cached catalog.
            self.assertEqual(second.served_from, "cache")
            self.assertEqual(second.models, first.models)
        self.assertEqual(len(runner.calls), 1)

    def test_failed_fresh_serves_last_known_good_flagged_stale(self):
        runner = _ScriptedRunner({})  # nonzero exit for every binary
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner)
            fingerprint = compute_source_fingerprint("antigravity_cli", _agent(), "1.0")
            service.store(_observation("antigravity_cli", fingerprint, ("gemini-3.6-flash-high",)))
            view = service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            self.assertTrue(view.probed)
            self.assertEqual(view.served_from, "cache")
            self.assertEqual(view.models, ("gemini-3.6-flash-high",))
            self.assertTrue(view.stale)  # just-failed refresh flags it stale
            self.assertFalse(view.authoritative)

    def test_failed_fresh_without_prior_reports_failure_observation(self):
        runner = _ScriptedRunner({})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner)
            view = service.serve("xai_cli", _agent("grok"), version="1.0", refresh="fresh")
            self.assertEqual(view.served_from, "fresh_probe")
            self.assertEqual(view.observation.status, "error")
            self.assertEqual(view.models, ())
            self.assertFalse(view.authoritative)

    def test_cached_entry_past_ttl_is_flagged_stale_not_dropped(self):
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            fingerprint = compute_source_fingerprint("antigravity_cli", _agent(), "1.0")
            service.store(
                _observation("antigravity_cli", fingerprint, ("gemini-3.5-flash-low",), checked=OLD)
            )
            view = service.serve("antigravity_cli", _agent(), version="1.0", refresh="cached")
            self.assertEqual(view.served_from, "cache")
            self.assertTrue(view.stale)
            self.assertFalse(view.authoritative)
            # Stale last-known-good still contributes suggestions.
            self.assertEqual(view.models, ("gemini-3.5-flash-low",))

    def test_fingerprint_mismatch_serves_static(self):
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            fingerprint = compute_source_fingerprint("antigravity_cli", _agent(), "1.0")
            service.store(_observation("antigravity_cli", fingerprint, ("m",)))
            view = service.serve("antigravity_cli", _agent(), version="2.0", refresh="cached")
            self.assertEqual(view.served_from, "static")
            self.assertIsNone(view.observation)

    def test_unsupported_backend_never_probes_even_fresh(self):
        runner = _ScriptedRunner({"claude": "x\n"})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner)
            view = service.serve("claude_cli", _agent("claude"), version="1.0", refresh="fresh")
            self.assertFalse(view.supported)
            self.assertEqual(view.probe_gate, "unsupported")
            self.assertEqual(view.served_from, "static")
        self.assertEqual(runner.calls, [])

    def test_disabled_backend_never_probes_even_fresh(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner)
            view = service.serve(
                "antigravity_cli", _agent(), version="1.0", refresh="fresh", allow_probe=False
            )
            self.assertEqual(view.probe_gate, "backend_disabled")
        self.assertEqual(runner.calls, [])

    def test_invalid_refresh_mode_raises(self):
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            with self.assertRaises(ValueError):
                service.serve("antigravity_cli", _agent(), version=None, refresh="eventually")

    def test_probe_slot_is_shared_between_fresh_and_background_callers(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT})
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner, monotonic=lambda: 100.0)
            service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            # A background caller consulting the same service is gated by the
            # inline fresh probe that just ran (and vice versa).
            self.assertFalse(service.acquire_probe_slot("antigravity_cli", 300.0))
            self.assertTrue(service.acquire_probe_slot("xai_cli", 300.0))

    def test_failed_fresh_keeps_flagging_ok_catalog_stale_until_success(self):
        ticks = iter(range(0, 100000, 1000))
        runner = _ScriptedRunner({})  # every probe fails
        with TemporaryDirectory() as tmp:
            service = _service(tmp, runner=runner, monotonic=lambda: next(ticks))
            fingerprint = compute_source_fingerprint("antigravity_cli", _agent(), "1.0")
            service.store(_observation("antigravity_cli", fingerprint, ("gemini-3.6-flash-high",)))
            failed = service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            self.assertTrue(failed.stale)
            # The failed revalidation is durable: a later cached serve still
            # flags the surviving last-known-good stale (and never warns).
            cached = service.serve("antigravity_cli", _agent(), version="1.0", refresh="cached")
            self.assertTrue(cached.stale)
            self.assertFalse(cached.authoritative)
            self.assertEqual(cached.models, ("gemini-3.6-flash-high",))
            # A probe success clears the flag.
            runner.outputs["agy"] = AGY_OUTPUT
            recovered = service.serve("antigravity_cli", _agent(), version="1.0", refresh="fresh")
            self.assertFalse(recovered.stale)
            after = service.serve("antigravity_cli", _agent(), version="1.0", refresh="cached")
            self.assertFalse(after.stale)
            self.assertTrue(after.authoritative)


class MergeTests(unittest.TestCase):
    def test_configured_default_leads_then_discovered_then_static(self):
        merged = merge_model_suggestions(
            "Gemini 3.6 Flash (High)",
            ("gemini-3.6-flash-high", "gemini-3.5-flash-low"),
            ["Gemini 3.6 Flash (High)", "Gemini 3.1 Pro (High)"],
        )
        self.assertEqual(
            merged,
            [
                "Gemini 3.6 Flash (High)",
                "gemini-3.6-flash-high",
                "gemini-3.5-flash-low",
                "Gemini 3.1 Pro (High)",
            ],
        )

    def test_dedup_preserves_first_occurrence_order(self):
        merged = merge_model_suggestions("a", ("b", "a", "b"), ["c", "b", "a"])
        self.assertEqual(merged, ["a", "b", "c"])

    def test_blank_default_and_blank_entries_are_skipped(self):
        self.assertEqual(merge_model_suggestions("  ", ("m", " "), None), ["m"])
        self.assertEqual(merge_model_suggestions(None, (), ["s"]), ["s"])


class WarningTests(unittest.TestCase):
    def _view(self, observation, *, stale=False):
        return CatalogView(
            "antigravity_cli",
            True,
            "cached",
            observation,
            stale=stale,
            served_from="cache" if observation is not None else "static",
            probed=False,
        )

    def test_fires_only_on_authoritative_catalog_missing_default(self):
        observation = _observation("antigravity_cli", "fp", ("gemini-3.6-flash-high",))
        warning = configured_default_warning(
            "antigravity_cli", self._view(observation), "Gemini 3.6 Flash (High)"
        )
        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], CONFIGURED_DEFAULT_NOT_IN_CATALOG)
        self.assertIn("Gemini 3.6 Flash (High)", warning["message"])
        self.assertIn("antigravity_cli", warning["message"])

    def test_no_warning_when_default_is_in_catalog(self):
        observation = _observation("antigravity_cli", "fp", ("Gemini 3.6 Flash (High)",))
        self.assertIsNone(
            configured_default_warning(
                "antigravity_cli", self._view(observation), "Gemini 3.6 Flash (High)"
            )
        )

    def test_never_warns_on_non_authoritative_observations(self):
        cases = {
            "static": None,
            "error": _observation("antigravity_cli", "fp", (), status="error", complete=False),
            "timeout": _observation("antigravity_cli", "fp", (), status="timeout", complete=False),
            "unavailable": _observation(
                "antigravity_cli", "fp", (), status="unavailable", complete=False
            ),
            "unsupported": _observation(
                "antigravity_cli", "fp", (), status="unsupported", complete=False
            ),
            "incomplete": _observation("antigravity_cli", "fp", ("m",), complete=False),
        }
        for label, observation in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(
                    configured_default_warning(
                        "antigravity_cli", self._view(observation), "Gemini 3.6 Flash (High)"
                    )
                )
        stale_good = _observation("antigravity_cli", "fp", ("other-model",))
        with self.subTest(case="stale"):
            self.assertIsNone(
                configured_default_warning(
                    "antigravity_cli",
                    self._view(stale_good, stale=True),
                    "Gemini 3.6 Flash (High)",
                )
            )

    def test_never_warns_without_a_configured_default(self):
        observation = _observation("antigravity_cli", "fp", ("m",))
        for default in (None, "", "   "):
            self.assertIsNone(
                configured_default_warning("antigravity_cli", self._view(observation), default)
            )


class DescribeOptionsIntegrationTests(unittest.TestCase):
    """describe_options wiring: model_catalog fields, effective merge, and the
    warn-only default check, with an injected service and fake health."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name)
        self.config = builtin_config()

    def _health(self, backend):
        return BackendHealth(status="ok", version="9.9", checked_at=RECENT)

    def _describe(self, service, model_refresh="cached"):
        return describe_options(
            self.config,
            health=self._health,
            model_refresh=model_refresh,
            model_catalogs=service,
        )

    def _seed(self, service, canonical, models, **kwargs):
        agent = self.config.agents[canonical]
        fingerprint = compute_source_fingerprint(canonical, agent, "9.9")
        service.store(_observation(canonical, fingerprint, models, **kwargs))

    def test_cached_serves_seeded_catalog_and_merges_effective_suggestions(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.6-flash-high", "gemini-3.5-flash-low"))
        payload = self._describe(service)
        self.assertEqual(payload["discovery"]["model_request"], "cached")
        entry = payload["backends"]["antigravity_cli"]
        catalog = entry["model_catalog"]
        self.assertTrue(catalog["supported"])
        self.assertEqual(catalog["status"], "ok")
        self.assertEqual(catalog["served_from"], "cache")
        self.assertTrue(catalog["authoritative"])
        self.assertEqual(catalog["models"], ["gemini-3.6-flash-high", "gemini-3.5-flash-low"])
        # One namespace (v10): the shipped canonical default is in the
        # catalog, so no warning fires.
        self.assertEqual(catalog["reason_codes"], [])
        self.assertEqual(catalog["configured_default"], "gemini-3.6-flash-high")
        effective = entry["effective"]["option_schema"]["properties"]["model"]
        self.assertEqual(effective["default"], "gemini-3.6-flash-high")
        # The configured default leads, then the remaining discovered models,
        # then the static fallback — order-preserving dedup keeps one copy.
        self.assertEqual(
            effective["suggested"][:3],
            ["gemini-3.6-flash-high", "gemini-3.5-flash-low", "gemini-3.6-flash-medium"],
        )
        static_spec = entry["static"]["option_schema"]["properties"]["model"]
        for suggestion in static_spec["suggested"]:
            self.assertIn(suggestion, effective["suggested"])
        self.assertEqual(effective["suggested"].count("gemini-3.6-flash-high"), 1)

    def test_authoritative_catalog_missing_default_warns_but_default_leads(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.5-flash-low",))
        entry = self._describe(service)["backends"]["antigravity_cli"]
        catalog = entry["model_catalog"]
        self.assertIn(CONFIGURED_DEFAULT_NOT_IN_CATALOG, catalog["reason_codes"])
        self.assertEqual(catalog["configured_default"], "gemini-3.6-flash-high")
        # Warn-only: the default is passed through unchanged and still leads.
        effective = entry["effective"]["option_schema"]["properties"]["model"]
        self.assertEqual(effective["default"], "gemini-3.6-flash-high")
        self.assertEqual(effective["suggested"][0], "gemini-3.6-flash-high")

    def test_static_schema_is_not_mutated_by_effective_merge(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-9.9-experimental",))
        payload = self._describe(service)
        entry = payload["backends"]["antigravity_cli"]
        static_suggested = entry["static"]["option_schema"]["properties"]["model"]["suggested"]
        self.assertNotIn("gemini-9.9-experimental", static_suggested)
        self.assertIn(
            "gemini-9.9-experimental",
            entry["effective"]["option_schema"]["properties"]["model"]["suggested"],
        )

    def test_no_warning_when_catalog_contains_configured_default(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.6-flash-high", "gemini-3.5-flash-low"))
        catalog = self._describe(service)["backends"]["antigravity_cli"]["model_catalog"]
        self.assertEqual(catalog["reason_codes"], [])
        self.assertEqual(catalog["warnings"], [])
        self.assertEqual(catalog["configured_default"], "gemini-3.6-flash-high")

    def test_stale_catalog_serves_models_but_never_warns(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.5-flash-low",), checked=OLD)
        catalog = self._describe(service)["backends"]["antigravity_cli"]["model_catalog"]
        self.assertTrue(catalog["stale"])
        self.assertEqual(catalog["models"], ["gemini-3.5-flash-low"])
        self.assertNotIn(CONFIGURED_DEFAULT_NOT_IN_CATALOG, catalog["reason_codes"])

    def test_unsupported_backends_fall_back_to_static_with_default_leading(self):
        service = _service(self.cache_dir)
        payload = self._describe(service)
        entry = payload["backends"]["claude_cli"]
        catalog = entry["model_catalog"]
        self.assertFalse(catalog["supported"])
        self.assertEqual(catalog["served_from"], "static")
        self.assertEqual(catalog["status"], "absent")
        self.assertEqual(catalog["models"], [])
        self.assertEqual(catalog["reason_codes"], [])
        effective = entry["effective"]["option_schema"]["properties"]["model"]
        self.assertEqual(effective["suggested"][0], "opus")
        self.assertEqual(effective["default"], "opus")
        self.assertEqual(
            effective["suggested"], ["opus", "fable", "sonnet"]
        )  # default leads, order-preserving dedup over the static list

    def test_none_and_cached_never_probe_through_describe(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT, "grok": GROK_OUTPUT})
        service = _service(self.cache_dir, runner=runner)
        for refresh in ("none", "cached"):
            self._describe(service, model_refresh=refresh)
        self.assertEqual(runner.calls, [])

    def test_fresh_probes_only_enabled_supported_backends(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT, "grok": GROK_OUTPUT})
        service = _service(self.cache_dir, runner=runner)
        payload = self._describe(service, model_refresh="fresh")
        probed = {argv[0] for argv in runner.calls}
        self.assertEqual(probed, {"agy", "grok"})
        antigravity = payload["backends"]["antigravity_cli"]["model_catalog"]
        self.assertEqual(antigravity["served_from"], "fresh_probe")
        self.assertEqual(antigravity["models"][0], "gemini-3.6-flash-high")
        xai = payload["backends"]["xai_cli"]["model_catalog"]
        self.assertEqual(xai["models"], ["grok-4.5", "grok-composer-2.5-fast"])
        # Both canonical defaults are present in their catalogs: no warnings.
        self.assertEqual(xai["reason_codes"], [])
        self.assertEqual(antigravity["reason_codes"], [])

    def test_discovery_writes_only_cache_files_never_config(self):
        runner = _ScriptedRunner({"agy": AGY_OUTPUT, "grok": GROK_OUTPUT})
        service = _service(self.cache_dir, runner=runner)
        config_path = self.cache_dir.parent / "config.toml"
        self._describe(service, model_refresh="fresh")
        written = sorted(path.name for path in self.cache_dir.iterdir())
        self.assertEqual(written, ["models_antigravity_cli.json", "models_xai_cli.json"])
        self.assertFalse(config_path.exists())

    def test_invalid_model_refresh_raises(self):
        with self.assertRaises(ValueError):
            describe_options(self.config, model_refresh="eventually")


class StartWarningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name)
        self.config = builtin_config()

    def _seed(self, service, canonical, models, **kwargs):
        agent = self.config.agents[canonical]
        fingerprint = compute_source_fingerprint(canonical, agent, "9.9")
        service.store(_observation(canonical, fingerprint, models, **kwargs))

    def test_warning_echoed_when_authoritative_catalog_omits_default(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.5-flash-low",))
        warnings = start_catalog_warnings(
            self.config,
            {"antigravity_cli": "cli"},
            service=service,
            version_for=lambda agent_type, backend_id: "9.9",
        )
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], CONFIGURED_DEFAULT_NOT_IN_CATALOG)
        self.assertEqual(warnings[0]["canonical_backend"], "antigravity_cli")
        self.assertEqual(warnings[0]["model"], "gemini-3.6-flash-high")

    def test_no_warning_when_default_is_in_catalog(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.6-flash-high",))
        warnings = start_catalog_warnings(
            self.config,
            {"antigravity_cli": "cli"},
            service=service,
            version_for=lambda agent_type, backend_id: "9.9",
        )
        self.assertEqual(warnings, [])

    def test_no_warning_when_catalog_is_not_authoritative(self):
        service = _service(self.cache_dir)
        self._seed(service, "antigravity_cli", ("gemini-3.5-flash-low",), checked=OLD)
        warnings = start_catalog_warnings(
            self.config,
            {"antigravity_cli": "cli"},
            service=service,
            version_for=lambda agent_type, backend_id: "9.9",
        )
        self.assertEqual(warnings, [])

    def test_unsupported_backends_are_skipped_without_any_version_lookup(self):
        service = _service(self.cache_dir)
        calls = []

        def version_for(agent_type, backend_id):
            calls.append((agent_type, backend_id))
            return "9.9"

        warnings = start_catalog_warnings(
            self.config,
            {"claude_cli": "cli", "codex_cli": "cli"},
            service=service,
            version_for=version_for,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(calls, [])

    def test_internal_failure_yields_no_warnings_not_an_exception(self):
        class _Boom:
            def serve(self, *args, **kwargs):
                raise RuntimeError("cache exploded")

        warnings = start_catalog_warnings(
            self.config,
            {"antigravity_cli": "cli"},
            service=_Boom(),
            version_for=lambda agent_type, backend_id: "9.9",
        )
        self.assertEqual(warnings, [])


class _FakeDiscoverer:
    """Scripted async discoverer producing observations with real fingerprints."""

    def __init__(self, specs, now=_clock()):
        self.specs = dict(specs)  # canonical -> (status, models)
        self.now = now
        self.calls = []

    async def discover(self, canonical, agent, *, version):
        self.calls.append(canonical)
        status, models = self.specs[canonical]
        fingerprint = compute_source_fingerprint(canonical, agent, version)
        return _observation(
            canonical,
            fingerprint,
            models,
            status=status,
            complete=status == "ok",
            checked=self.now(),
        )

    async def discover_all(self, requests):
        return tuple(
            [
                await self.discover(canonical, agent, version=version)
                for canonical, agent, version in requests
            ]
        )


def _refresher_config():
    agents = {
        "antigravity_cli": AgentConfig(
            id="antigravity_cli",
            type="antigravity",
            command="agy",
            backend="cli",
            # A canonical-namespace default the fake catalogs never contain,
            # so warning-flip expectations stay deterministic.
            default_options={"model": "gemini-9.9-flash-high"},
        ),
        "xai_cli": AgentConfig(
            id="xai_cli",
            type="xai",
            command="grok",
            backend="cli",
            default_options={"model": "grok-4.5"},
        ),
    }
    return SimpleNamespace(agents=agents)


class RefresherTests(unittest.IsolatedAsyncioTestCase):
    def _refresher(self, tmp, discoverer, *, logs=None, config=None):
        service = _service(tmp)
        return (
            ModelCatalogRefresher(
                logger=(logs.append if logs is not None else lambda line: None),
                service=service,
                load_config=lambda: config or _refresher_config(),
                discoverer=discoverer,
                version_for=lambda agent_type, backend_id: "9.9",
            ),
            service,
        )

    async def test_refresh_populates_cache_and_logs_transition(self):
        logs = []
        discoverer = _FakeDiscoverer(
            {
                "antigravity_cli": ("ok", ("gemini-3.6-flash-high",)),
                "xai_cli": ("ok", ("grok-4.5",)),
            }
        )
        with TemporaryDirectory() as tmp:
            refresher, service = self._refresher(tmp, discoverer, logs=logs)
            await refresher.refresh_once()
            config = _refresher_config()
            fingerprint = compute_source_fingerprint(
                "antigravity_cli", config.agents["antigravity_cli"], "9.9"
            )
            served = service.read("antigravity_cli", fingerprint)
            self.assertIsNotNone(served)
            self.assertEqual(served.observation.models, ("gemini-3.6-flash-high",))
        transition_lines = [line for line in logs if "model catalog transition" in line]
        self.assertEqual(len(transition_lines), 2)
        antigravity_line = next(line for line in transition_lines if "antigravity_cli" in line)
        # The catalog omits the display-name default -> the warning flips on,
        # warn-only (behavior unchanged).
        self.assertIn("default-warning off -> on", antigravity_line)
        xai_line = next(line for line in transition_lines if "xai_cli" in line)
        self.assertIn("default-warning off -> off", xai_line)

    async def test_fresh_ok_entry_is_not_reprobed(self):
        discoverer = _FakeDiscoverer(
            {"antigravity_cli": ("ok", ("m",)), "xai_cli": ("ok", ("grok-4.5",))}
        )
        with TemporaryDirectory() as tmp:
            refresher, _service_ = self._refresher(tmp, discoverer)
            await refresher.refresh_once()
            first_round = list(discoverer.calls)
            await refresher.refresh_once()
        self.assertEqual(discoverer.calls, first_round)

    async def test_min_interval_gates_failing_backend_retry(self):
        discoverer = _FakeDiscoverer({"antigravity_cli": ("error", ()), "xai_cli": ("error", ())})
        with TemporaryDirectory() as tmp:
            refresher, _service_ = self._refresher(tmp, discoverer)
            await refresher.refresh_once()
            await refresher.refresh_once()
        # One attempt per backend: the second cycle is inside the min interval.
        self.assertEqual(sorted(discoverer.calls), ["antigravity_cli", "xai_cli"])

    async def test_failed_refresh_preserves_last_known_good_and_stays_quiet(self):
        logs = []
        discoverer = _FakeDiscoverer({"antigravity_cli": ("error", ()), "xai_cli": ("error", ())})
        config = _refresher_config()
        with TemporaryDirectory() as tmp:
            refresher, service = self._refresher(tmp, discoverer, logs=logs, config=config)
            fingerprint = compute_source_fingerprint(
                "antigravity_cli", config.agents["antigravity_cli"], "9.9"
            )
            service.store(
                _observation(
                    "antigravity_cli", fingerprint, ("gemini-3.6-flash-high",), checked=OLD
                )
            )
            await refresher.refresh_once()
            served = service.read("antigravity_cli", fingerprint)
            self.assertIsNotNone(served)
            self.assertEqual(served.observation.status, "ok")
            self.assertEqual(served.observation.models, ("gemini-3.6-flash-high",))
        self.assertEqual(
            [line for line in logs if "antigravity_cli" in line and "transition" in line], []
        )

    async def test_recent_failure_revalidates_an_in_ttl_last_known_good(self):
        discoverer = _FakeDiscoverer(
            {
                "antigravity_cli": ("ok", ("gemini-3.6-flash-high",)),
                "xai_cli": ("ok", ("grok-4.5",)),
            }
        )
        config = _refresher_config()
        with TemporaryDirectory() as tmp:
            refresher, service = self._refresher(tmp, discoverer, config=config)
            fingerprint = compute_source_fingerprint(
                "antigravity_cli", config.agents["antigravity_cli"], "9.9"
            )
            # Fresh-in-TTL last-known-good, then a failed probe from another
            # entry point (e.g. an inline fresh serve) marks it for
            # revalidation.
            service.store(_observation("antigravity_cli", fingerprint, ("gemini-3.6-flash-high",)))
            service.store(
                _observation("antigravity_cli", fingerprint, (), status="error", complete=False)
            )
            self.assertTrue(service.recently_failed("antigravity_cli"))
            await refresher.refresh_once()
            self.assertIn("antigravity_cli", discoverer.calls)
            # The successful revalidation clears the failure flag.
            self.assertFalse(service.recently_failed("antigravity_cli"))

    async def test_kick_wakes_the_loop_early(self):
        cycles = []

        def load_config():
            cycles.append(len(cycles))
            return _refresher_config()

        discoverer = _FakeDiscoverer(
            {"antigravity_cli": ("ok", ("m",)), "xai_cli": ("ok", ("grok-4.5",))}
        )
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            refresher = ModelCatalogRefresher(
                logger=lambda line: None,
                service=service,
                load_config=load_config,
                discoverer=discoverer,
                version_for=lambda agent_type, backend_id: "9.9",
                check_interval=3600.0,
                kick_cooldown=0.0,
            )
            task = asyncio.ensure_future(refresher.run())
            try:
                await self._wait_for(lambda: len(cycles) >= 1)
                refresher.kick()
                await self._wait_for(lambda: len(cycles) >= 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_coalesced_kick_runs_a_cycle_once_the_cooldown_expires(self):
        cycles = []

        def load_config():
            cycles.append(len(cycles))
            return SimpleNamespace(agents={})

        with TemporaryDirectory() as tmp:
            refresher = ModelCatalogRefresher(
                logger=lambda line: None,
                service=_service(tmp),
                load_config=load_config,
                discoverer=_FakeDiscoverer({}),
                version_for=lambda agent_type, backend_id: None,
                check_interval=3600.0,
                # Finite: the kicked wake must sleep out the cooldown and then
                # actually run the coalesced cycle (not park forever).
                kick_cooldown=0.05,
            )
            task = asyncio.ensure_future(refresher.run())
            try:
                await self._wait_for(lambda: len(cycles) >= 1)
                refresher.kick()
                await self._wait_for(lambda: len(cycles) >= 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_timer_cadence_runs_cycles_without_any_kick(self):
        cycles = []

        def load_config():
            cycles.append(len(cycles))
            return SimpleNamespace(agents={})

        with TemporaryDirectory() as tmp:
            refresher = ModelCatalogRefresher(
                logger=lambda line: None,
                service=_service(tmp),
                load_config=load_config,
                discoverer=_FakeDiscoverer({}),
                version_for=lambda agent_type, backend_id: None,
                check_interval=0.02,
            )
            task = asyncio.ensure_future(refresher.run())
            try:
                # The baseline timer alone must keep cycles coming (the kick
                # cooldown applies only to kick-driven wakes).
                await self._wait_for(lambda: len(cycles) >= 3)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_kick_storm_is_coalesced_and_rate_limited(self):
        cycles = []

        def load_config():
            cycles.append(len(cycles))
            return SimpleNamespace(agents={})  # nothing to probe; cycles are cheap

        with TemporaryDirectory() as tmp:
            refresher = ModelCatalogRefresher(
                logger=lambda line: None,
                service=_service(tmp),
                load_config=load_config,
                discoverer=_FakeDiscoverer({}),
                version_for=lambda agent_type, backend_id: None,
                check_interval=3600.0,
                # Far longer than this test runs: after the first cycle, any
                # number of kicks must not schedule another cycle yet.
                kick_cooldown=3600.0,
            )
            task = asyncio.ensure_future(refresher.run())
            try:
                await self._wait_for(lambda: len(cycles) >= 1)
                for _ in range(25):
                    refresher.kick()
                    await asyncio.sleep(0)
                await asyncio.sleep(0.2)
                self.assertEqual(len(cycles), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def _wait_for(self, condition, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not reached before timeout")


class InstallDiscoveryTests(unittest.TestCase):
    def test_warns_only_for_ok_complete_catalog_missing_default(self):
        config = _refresher_config()
        discoverer = _FakeDiscoverer(
            {
                "antigravity_cli": ("ok", ("gemini-3.6-flash-high",)),
                "xai_cli": ("ok", ("grok-4.5", "grok-composer-2.5-fast")),
            }
        )
        with TemporaryDirectory() as tmp:
            service = _service(tmp)
            summary = run_install_discovery(
                config,
                {"antigravity_cli": "9.9", "xai_cli": "9.9"},
                service=service,
                discoverer=discoverer,
            )
            self.assertEqual(summary["attempted"], ["antigravity_cli", "xai_cli"])
            self.assertEqual(summary["backends"]["antigravity_cli"]["status"], "ok")
            self.assertTrue(summary["backends"]["antigravity_cli"]["cached"])
            codes = [warning["code"] for warning in summary["warnings"]]
            self.assertEqual(codes, [CONFIGURED_DEFAULT_NOT_IN_CATALOG])
            self.assertEqual(summary["warnings"][0]["canonical_backend"], "antigravity_cli")
            self.assertIn("gemini-9.9-flash-high", summary["warnings"][0]["message"])
            self.assertTrue((Path(tmp) / "models_antigravity_cli.json").exists())
            self.assertTrue((Path(tmp) / "models_xai_cli.json").exists())

    def test_failed_probes_never_produce_default_warnings(self):
        config = _refresher_config()
        discoverer = _FakeDiscoverer({"antigravity_cli": ("timeout", ()), "xai_cli": ("error", ())})
        with TemporaryDirectory() as tmp:
            summary = run_install_discovery(
                config, {}, service=_service(tmp), discoverer=discoverer
            )
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(summary["backends"]["antigravity_cli"]["status"], "timeout")

    def test_timeout_degrades_to_non_fatal_warning(self):
        class _Hanging:
            async def discover_all(self, requests):
                await asyncio.sleep(3600)

        config = _refresher_config()
        with TemporaryDirectory() as tmp:
            summary = run_install_discovery(
                config, {}, service=_service(tmp), discoverer=_Hanging(), timeout=0.05
            )
        self.assertEqual(summary["backends"], {})
        self.assertEqual(summary["warnings"][0]["code"], MODEL_DISCOVERY_FAILED)

    def test_no_enabled_discoverable_backends_probes_nothing(self):
        config = SimpleNamespace(agents={})
        summary = run_install_discovery(
            config, {}, service=_service(tempfile.gettempdir()), discoverer=_FakeDiscoverer({})
        )
        self.assertEqual(summary["attempted"], [])
        self.assertEqual(summary["backends"], {})
        self.assertEqual(summary["warnings"], [])

    def test_install_discovery_never_writes_config_files(self):
        config = _refresher_config()
        discoverer = _FakeDiscoverer(
            {"antigravity_cli": ("ok", ("m",)), "xai_cli": ("ok", ("grok-4.5",))}
        )
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_path = home / "config.toml"
            config_path.write_text("# user config\n", encoding="utf-8")
            cache_dir = home / "data" / "cache"
            run_install_discovery(config, {}, service=_service(cache_dir), discoverer=discoverer)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "# user config\n")
            created = sorted(path.name for path in cache_dir.iterdir())
            self.assertEqual(created, ["models_antigravity_cli.json", "models_xai_cli.json"])
            top_level = sorted(path.name for path in home.iterdir())
            self.assertEqual(top_level, ["config.toml", "data"])


class DaemonKickTests(unittest.IsolatedAsyncioTestCase):
    class _CannedManager(SessionManager):
        def __init__(self, payload):
            super().__init__()
            self._payload = payload

        def describe_options(
            self, workdir=None, *, health_refresh="cached", model_refresh="cached"
        ):
            return self._payload

    @staticmethod
    def _payload(
        *, served_from="cache", stale=False, supported=True, enabled=True, authoritative=None
    ):
        if authoritative is None:
            authoritative = served_from == "cache" and not stale
        return {
            "backends": {
                "antigravity_cli": {
                    "model_catalog": {
                        "supported": supported,
                        "served_from": served_from,
                        "stale": stale,
                        "authoritative": authoritative,
                    },
                    "policy": {"enabled": enabled},
                }
            }
        }

    def test_refresh_wanted_signal(self):
        self.assertTrue(_model_catalog_refresh_wanted(self._payload(stale=True)))
        self.assertTrue(_model_catalog_refresh_wanted(self._payload(served_from="static")))
        # A cached failed observation (non-stale, non-authoritative) also wants
        # a refresh: recovery must not wait out the full check interval.
        self.assertTrue(_model_catalog_refresh_wanted(self._payload(authoritative=False)))
        self.assertFalse(_model_catalog_refresh_wanted(self._payload()))
        self.assertFalse(
            _model_catalog_refresh_wanted(self._payload(served_from="static", supported=False))
        )
        self.assertFalse(_model_catalog_refresh_wanted(self._payload(stale=True, enabled=False)))
        self.assertFalse(_model_catalog_refresh_wanted({}))

    async def test_cached_describe_kicks_refresher_only_when_wanted(self):
        cases = [
            ("cached", self._payload(stale=True), True),
            ("cached", self._payload(), False),
            ("none", self._payload(stale=True), False),
            ("fresh", self._payload(stale=True), False),
        ]
        for model_refresh, payload, expected in cases:
            with self.subTest(model_refresh=model_refresh, expected=expected):
                manager = self._CannedManager(payload)
                kicks = []
                manager.model_catalog_kick = lambda: kicks.append(True)
                await manager.describe_options_async(model_refresh=model_refresh)
                self.assertEqual(bool(kicks), expected)


class SessionSnapshotInvariantTests(unittest.IsolatedAsyncioTestCase):
    TERMINAL = {"done", "failed", "stopped"}

    async def test_catalog_change_after_start_never_alters_session_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}):
                manager = SessionManager()
                state = await manager.start_session(
                    StartSessionRequest(
                        task="snapshot invariant",
                        mock=True,
                        max_turns=1,
                        timeout=5,
                        workdir=root,
                    )
                )
                snapshot = copy.deepcopy(state.settings)
                # A background refresh lands a new catalog after the start.
                cache_dir = GlobalDataPaths.resolve().cache_dir
                service = ModelCatalogService(cache_dir=cache_dir, now=_clock())
                service.store(_observation("antigravity_cli", "fp", ("gemini-9.9-experimental",)))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5.0
                while loop.time() < deadline:
                    if manager.get_session(state.session_id).status in self.TERMINAL:
                        break
                    await asyncio.sleep(0.02)
                final = manager.get_session(state.session_id)
                self.assertEqual(final.status, "done")
                # The snapshotted settings are byte-identical; no catalog data
                # ever reaches a running session.
                self.assertEqual(final.settings, snapshot)
                self.assertNotIn("model_catalog", json.dumps(final.settings))


if __name__ == "__main__":
    unittest.main()
