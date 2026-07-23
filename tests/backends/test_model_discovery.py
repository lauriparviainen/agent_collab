"""Hermetic tests for CLI/SDK model-catalog discovery.

Every provider probe here runs through a fake async runner and a fake clock, so
nothing touches a real CLI, SDK package, or network.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest import mock

from agent_collab.backends.common.model_discovery import (
    DEFAULT_TTL_SECONDS,
    SCHEMA_VERSION,
    CliResult,
    ModelCatalogCache,
    ModelCatalogObservation,
    ModelDiscoverer,
    SdkUnavailableError,
    compute_source_fingerprint,
    default_cli_runner,
    default_sdk_runner,
    parse_codex_sdk_models,
    parse_agy_models,
    parse_grok_models,
    parse_xai_sdk_models,
)


def _agent(command=None, args=None, env=None, backend_config=None):
    return SimpleNamespace(
        command=command,
        args=args or [],
        env=env or {},
        backend_config=backend_config or {},
    )


def _fixed_clock(value="2026-07-22T00:00:00+00:00"):
    return lambda: value


class _Runner:
    """Fake CLI runner: replays a scripted result or exception per call."""

    def __init__(self, result=None, *, raises=None, delay_event=None):
        self.result = result
        self.raises = raises
        self.delay_event = delay_event
        self.calls = []

    async def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        if self.delay_event is not None:
            await self.delay_event.wait()
        if self.raises is not None:
            raise self.raises
        return self.result


class _SdkRunner:
    """Fake SDK runner: replays one raw provider response or exception."""

    def __init__(self, result=None, *, raises=None, delay_event=None):
        self.result = result
        self.raises = raises
        self.delay_event = delay_event
        self.calls = []

    async def __call__(self, canonical, agent, timeout):
        self.calls.append((canonical, agent, timeout))
        if self.delay_event is not None:
            await self.delay_event.wait()
        if self.raises is not None:
            raise self.raises
        return self.result


AGY_OUTPUT = "gemini-3.6-flash-high\ngemini-3.6-flash-medium\ngemini-3.5-flash-low\n"
GROK_OUTPUT = (
    "You are not authenticated.\n\n"
    "Default model: grok-4.5\n\n"
    "Available models:\n"
    "  * grok-4.5 (default)\n"
    "  * grok-composer-2.5-fast\n"
)


class ParserTests(unittest.TestCase):
    def test_agy_parser_reads_canonical_ids(self):
        self.assertEqual(
            parse_agy_models(AGY_OUTPUT),
            ("gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.5-flash-low"),
        )

    def test_grok_parser_strips_marker_and_default_annotation(self):
        self.assertEqual(parse_grok_models(GROK_OUTPUT), ("grok-4.5", "grok-composer-2.5-fast"))

    def test_grok_parser_ignores_preamble_without_section(self):
        self.assertEqual(parse_grok_models("You are not authenticated.\n"), ())

    def test_parsers_dedupe_preserving_order(self):
        self.assertEqual(parse_agy_models("a\nb\na\n"), ("a", "b"))

    def test_agy_parser_drops_warning_and_notice_lines(self):
        noisy = "WARNING: rate limited\ngemini-3.6-flash-high\nUpdate available!\n"
        self.assertEqual(parse_agy_models(noisy), ("gemini-3.6-flash-high",))

    def test_grok_parser_drops_footer_prose(self):
        out = "Available models:\n  * grok-4.5\n\nVisit https://x.ai for more\n"
        self.assertEqual(parse_grok_models(out), ("grok-4.5",))

    def test_grok_parser_accepts_dash_bullets(self):
        self.assertEqual(parse_grok_models("Available models:\n  - grok-4.5\n"), ("grok-4.5",))

    def test_grok_parser_ignores_a_bulleted_footer_section(self):
        out = "Available models:\n  * grok-4.5\n\nNext steps:\n  * upgrade now\n"
        self.assertEqual(parse_grok_models(out), ("grok-4.5",))

    def test_codex_sdk_parser_uses_model_option_values(self):
        response = SimpleNamespace(
            data=[
                SimpleNamespace(model="gpt-5.6-sol", id="internal-1"),
                SimpleNamespace(model="gpt-5.6-luna", id="internal-2"),
            ]
        )
        self.assertEqual(parse_codex_sdk_models(response), ("gpt-5.6-sol", "gpt-5.6-luna"))

    def test_codex_sdk_parser_tolerates_mapping_shape(self):
        response = {"data": [{"model": "gpt-5.6-sol"}, {"displayName": "noise"}]}
        self.assertEqual(parse_codex_sdk_models(response), ("gpt-5.6-sol",))

    def test_xai_sdk_parser_includes_canonical_names_and_aliases(self):
        response = [
            SimpleNamespace(name="grok-4-0709", aliases=["grok-4", "grok-4.5"]),
            SimpleNamespace(name="grok-4-fast", aliases=["grok-4.5", "grok-fast"]),
        ]
        self.assertEqual(
            parse_xai_sdk_models(response),
            ("grok-4-0709", "grok-4", "grok-4.5", "grok-4-fast", "grok-fast"),
        )

    def test_sdk_parsers_reject_prose_and_malformed_shapes(self):
        self.assertEqual(parse_codex_sdk_models({"data": "not-a-list"}), ())
        self.assertEqual(parse_xai_sdk_models("not-a-model-sequence"), ())


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_config_sensitive(self):
        base = compute_source_fingerprint("antigravity_cli", _agent(command="agy"), "1.1.5")
        self.assertEqual(
            base, compute_source_fingerprint("antigravity_cli", _agent(command="agy"), "1.1.5")
        )
        self.assertNotEqual(
            base, compute_source_fingerprint("antigravity_cli", _agent(command="agy2"), "1.1.5")
        )

    def test_provider_version_change_invalidates_fingerprint(self):
        old = compute_source_fingerprint("antigravity_cli", _agent(command="agy"), "1.1.5")
        new = compute_source_fingerprint("antigravity_cli", _agent(command="agy"), "1.2.0")
        self.assertNotEqual(old, new)

    def test_cli_secret_value_rotation_does_not_invalidate_fingerprint(self):
        # Same key, different secret value -> same fingerprint (a token refresh
        # must not blow away the catalog, which does not depend on the token).
        a = _agent(command="grok", env={"PATH": "/usr/bin", "XAI_API_KEY": "sk-1"})
        b = _agent(command="grok", env={"PATH": "/usr/bin", "XAI_API_KEY": "sk-2"})
        self.assertEqual(
            compute_source_fingerprint("xai_cli", a, "0.2.101"),
            compute_source_fingerprint("xai_cli", b, "0.2.101"),
        )

    def test_sdk_agent_key_rotation_invalidates_account_scoped_fingerprint(self):
        cases = (
            ("xai_sdk", "XAI_API_KEY", "1.17.0"),
            ("codex_sdk", "OPENAI_API_KEY", "0.144.4"),
        )
        for canonical, key, version in cases:
            with self.subTest(canonical=canonical):
                a = _agent(env={key: "account-a-key"})
                b = _agent(env={key: "account-b-key"})
                self.assertNotEqual(
                    compute_source_fingerprint(canonical, a, version),
                    compute_source_fingerprint(canonical, b, version),
                )

    def test_sdk_process_key_rotation_invalidates_account_scoped_fingerprint(self):
        cases = (
            ("xai_sdk", "XAI_API_KEY", "1.17.0"),
            ("codex_sdk", "OPENAI_API_KEY", "0.144.4"),
        )
        for canonical, key, version in cases:
            with self.subTest(canonical=canonical):
                agent = _agent()
                with mock.patch.dict(os.environ, {key: "process-account-a"}):
                    first = compute_source_fingerprint(canonical, agent, version)
                with mock.patch.dict(os.environ, {key: "process-account-b"}):
                    second = compute_source_fingerprint(canonical, agent, version)
                self.assertNotEqual(first, second)

    def test_non_secret_field_change_invalidates_fingerprint(self):
        # Distinct non-secret identity (e.g. a different project) must not collapse
        # to the same cache key.
        a = _agent(command="grok", backend_config={"project": "A"})
        b = _agent(command="grok", backend_config={"project": "B"})
        self.assertNotEqual(
            compute_source_fingerprint("xai_cli", a, "0.2.101"),
            compute_source_fingerprint("xai_cli", b, "0.2.101"),
        )

    def test_fingerprint_never_raises_on_malformed_config(self):
        # backend_config that is not a mapping must not crash fingerprinting.
        bad = _agent(command="grok", backend_config="not-a-dict")
        fp = compute_source_fingerprint("xai_cli", bad, "0.2.101")
        self.assertEqual(len(fp), 64)  # a real sha256 hexdigest, not an exception


class DiscoverTests(unittest.TestCase):
    def _discover(self, canonical, agent, *, runner=None, sdk_runner=None, version="1.0"):
        kwargs = {"now": _fixed_clock()}
        if runner is not None:
            kwargs["runner"] = runner
        if sdk_runner is not None:
            kwargs["sdk_runner"] = sdk_runner
        discoverer = ModelDiscoverer(**kwargs)
        return asyncio.run(discoverer.discover(canonical, agent, version=version))

    def test_successful_probe_yields_ok_complete_catalog(self):
        runner = _Runner(CliResult(returncode=0, stdout=AGY_OUTPUT))
        obs = self._discover("antigravity_cli", _agent(command="agy"), runner=runner)
        self.assertEqual(obs.status, "ok")
        self.assertTrue(obs.complete)
        self.assertEqual(obs.source, "cli")
        self.assertEqual(obs.models[0], "gemini-3.6-flash-high")
        self.assertEqual(obs.last_success_at, obs.checked_at)
        self.assertEqual(runner.calls[0][0], ("agy", "models"))

    def test_agent_command_override_is_used_as_binary(self):
        runner = _Runner(CliResult(returncode=0, stdout=GROK_OUTPUT))
        self._discover("xai_cli", _agent(command="/opt/grok"), runner=runner)
        self.assertEqual(runner.calls[0][0], ("/opt/grok", "models"))

    def test_unknown_backend_is_unsupported_without_probing(self):
        runner = _Runner(CliResult(returncode=0, stdout="x\n"))
        obs = self._discover("claude_cli", _agent(command="claude"), runner=runner)
        self.assertEqual(obs.status, "unsupported")
        self.assertFalse(obs.complete)
        self.assertEqual(obs.reason_code, "no_cli_catalog_command")
        self.assertEqual(runner.calls, [])

    def test_sdk_without_catalog_api_is_unsupported_without_probing(self):
        runner = _SdkRunner([SimpleNamespace(name="should-not-run", aliases=[])])
        obs = self._discover("claude_sdk", _agent(), sdk_runner=runner)
        self.assertEqual(obs.status, "unsupported")
        self.assertEqual(obs.source, "sdk")
        self.assertEqual(obs.reason_code, "no_sdk_catalog_api")
        self.assertEqual(runner.calls, [])

    def test_codex_sdk_success_uses_public_catalog_response(self):
        runner = _SdkRunner(SimpleNamespace(data=[SimpleNamespace(model="gpt-5.6-sol")]))
        obs = self._discover("codex_sdk", _agent(command="codex"), sdk_runner=runner)
        self.assertEqual(obs.status, "ok")
        self.assertTrue(obs.complete)
        self.assertEqual(obs.source, "sdk")
        self.assertEqual(obs.models, ("gpt-5.6-sol",))
        self.assertEqual(runner.calls[0][0], "codex_sdk")

    def test_codex_sdk_paginated_response_is_non_authoritative(self):
        runner = _SdkRunner(
            SimpleNamespace(
                data=[SimpleNamespace(model="gpt-5.6-sol")],
                next_cursor="page-2",
            )
        )
        obs = self._discover("codex_sdk", _agent(command="codex"), sdk_runner=runner)
        self.assertEqual(obs.status, "ok")
        self.assertFalse(obs.complete)
        self.assertEqual(obs.models, ("gpt-5.6-sol",))
        self.assertEqual(obs.reason_code, "catalog_paginated")

    def test_xai_sdk_success_includes_aliases(self):
        runner = _SdkRunner([SimpleNamespace(name="grok-4-0709", aliases=["grok-4.5"])])
        obs = self._discover("xai_sdk", _agent(), sdk_runner=runner)
        self.assertEqual(obs.status, "ok")
        self.assertEqual(obs.models, ("grok-4-0709", "grok-4.5"))

    def test_sdk_timeout_never_raises(self):
        async def slow(_canonical, _agent_config, _timeout):
            await asyncio.sleep(30)

        discoverer = ModelDiscoverer(sdk_runner=slow, now=_fixed_clock(), per_probe_timeout=0.01)
        obs = asyncio.run(discoverer.discover("xai_sdk", _agent(), version="1"))
        self.assertEqual(obs.status, "timeout")
        self.assertEqual(obs.reason_code, "probe_timeout")

    def test_missing_sdk_is_unavailable(self):
        runner = _SdkRunner(raises=SdkUnavailableError("missing"))
        obs = self._discover("codex_sdk", _agent(), sdk_runner=runner)
        self.assertEqual(obs.status, "unavailable")
        self.assertEqual(obs.reason_code, "sdk_not_importable")

    def test_sdk_auth_or_transport_failure_is_error(self):
        runner = _SdkRunner(raises=RuntimeError("unauthenticated"))
        obs = self._discover("xai_sdk", _agent(), sdk_runner=runner)
        self.assertEqual(obs.status, "error")
        self.assertEqual(obs.reason_code, "probe_failed")

    def test_empty_sdk_response_is_error_not_raise(self):
        obs = self._discover("codex_sdk", _agent(), sdk_runner=_SdkRunner({"data": []}))
        self.assertEqual(obs.status, "error")
        self.assertEqual(obs.reason_code, "empty_or_unparseable")

    def test_timeout_never_raises_and_marks_timeout(self):
        runner = _Runner(raises=asyncio.TimeoutError())
        obs = self._discover("xai_cli", _agent(command="grok"), runner=runner)
        self.assertEqual(obs.status, "timeout")
        self.assertFalse(obs.complete)
        self.assertEqual(obs.models, ())

    def test_missing_binary_is_unavailable(self):
        runner = _Runner(raises=OSError("no such file"))
        obs = self._discover("xai_cli", _agent(command="grok"), runner=runner)
        self.assertEqual(obs.status, "unavailable")

    def test_nonzero_exit_is_error(self):
        runner = _Runner(CliResult(returncode=2, stdout="", stderr="boom"))
        obs = self._discover("antigravity_cli", _agent(command="agy"), runner=runner)
        self.assertEqual(obs.status, "error")
        self.assertEqual(obs.reason_code, "nonzero_exit")

    def test_unparseable_output_is_error_not_raise(self):
        runner = _Runner(CliResult(returncode=0, stdout="   \n \n"))
        obs = self._discover("xai_cli", _agent(command="grok"), runner=runner)
        self.assertEqual(obs.status, "error")
        self.assertEqual(obs.reason_code, "empty_or_unparseable")

    def test_parser_exception_falls_back_to_error(self):
        def boom(_text):
            raise RuntimeError("parser drift")

        from agent_collab.backends.common import model_discovery as md

        original = md.CLI_DISCOVERY["xai_cli"]
        md.CLI_DISCOVERY["xai_cli"] = md.CliDiscoverySpec(
            backend_id="xai_cli", default_binary="grok", list_args=("models",), parser=boom
        )
        try:
            runner = _Runner(CliResult(returncode=0, stdout=GROK_OUTPUT))
            obs = self._discover("xai_cli", _agent(command="grok"), runner=runner)
        finally:
            md.CLI_DISCOVERY["xai_cli"] = original
        self.assertEqual(obs.status, "error")

    def test_inflight_dedup_shares_one_probe(self):
        runner_box = {}

        async def drive():
            event = asyncio.Event()
            runner = _Runner(CliResult(returncode=0, stdout=AGY_OUTPUT), delay_event=event)
            runner_box["runner"] = runner
            discoverer = ModelDiscoverer(runner=runner, now=_fixed_clock())
            agent = _agent(command="agy")
            first = asyncio.ensure_future(
                discoverer.discover("antigravity_cli", agent, version="1")
            )
            second = asyncio.ensure_future(
                discoverer.discover("antigravity_cli", agent, version="1")
            )
            await asyncio.sleep(0)  # let both register before releasing the probe
            event.set()
            return await asyncio.gather(first, second)

        first, second = asyncio.run(drive())
        self.assertEqual(first.models, second.models)
        self.assertEqual(len(runner_box["runner"].calls), 1)

    def test_dedup_does_not_share_across_distinct_configs(self):
        runner_box = {}

        async def drive():
            event = asyncio.Event()
            runner = _Runner(CliResult(returncode=0, stdout=AGY_OUTPUT), delay_event=event)
            runner_box["runner"] = runner
            d = ModelDiscoverer(runner=runner, now=_fixed_clock())
            agent = _agent(command="agy")
            # Same backend, different provider version -> different fingerprint.
            a = asyncio.ensure_future(d.discover("antigravity_cli", agent, version="1.1.5"))
            b = asyncio.ensure_future(d.discover("antigravity_cli", agent, version="1.2.0"))
            await asyncio.sleep(0)
            event.set()
            return await asyncio.gather(a, b)

        asyncio.run(drive())
        self.assertEqual(len(runner_box["runner"].calls), 2)  # not shared

    def test_cancellation_propagates_and_is_not_a_timeout(self):
        async def drive():
            event = asyncio.Event()
            runner = _Runner(CliResult(returncode=0, stdout=AGY_OUTPUT), delay_event=event)
            d = ModelDiscoverer(runner=runner, now=_fixed_clock())
            task = asyncio.ensure_future(d.discover("xai_cli", _agent(command="grok"), version="1"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            event.set()  # release the shielded probe so nothing is left pending

        asyncio.run(drive())

    def test_cancelled_originator_does_not_fail_a_peer_waiter(self):
        # A and B share one probe; cancelling A must not tear the probe out from
        # under B (the round-2 dedup finding).
        async def drive():
            event = asyncio.Event()
            runner = _Runner(CliResult(returncode=0, stdout=AGY_OUTPUT), delay_event=event)
            d = ModelDiscoverer(runner=runner, now=_fixed_clock())
            agent = _agent(command="agy")
            a = asyncio.ensure_future(d.discover("antigravity_cli", agent, version="1"))
            b = asyncio.ensure_future(d.discover("antigravity_cli", agent, version="1"))
            await asyncio.sleep(0)  # both register and share the one task
            a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await a
            event.set()  # let the shared probe finish
            return await b, len(runner.calls)

        obs, calls = asyncio.run(drive())
        self.assertEqual(obs.status, "ok")
        self.assertEqual(calls, 1)  # shared; A's cancel did not kill or re-run it


class RealRunnerTests(unittest.TestCase):
    """Exercise the actual ``default_cli_runner`` subprocess + deadline path with
    a controlled local Python child (never a real provider CLI or the network)."""

    def test_success_captures_stdout_and_returncode(self):
        argv = [sys.executable, "-c", "print('gemini-x')"]
        result = asyncio.run(default_cli_runner(argv, 10.0))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "gemini-x")

    def test_nonzero_exit_is_reported(self):
        argv = [sys.executable, "-c", "import sys; sys.exit(3)"]
        result = asyncio.run(default_cli_runner(argv, 10.0))
        self.assertEqual(result.returncode, 3)

    def test_deadline_kills_and_raises_timeout(self):
        # A child that would run far longer than the deadline must be killed and
        # surface as TimeoutError, promptly.
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]

        async def run():
            start = asyncio.get_event_loop().time()
            with self.assertRaises(asyncio.TimeoutError):
                await default_cli_runner(argv, 0.3)
            return asyncio.get_event_loop().time() - start

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 5.0)  # killed near the deadline, not after 30s


class DefaultSdkRunnerTests(unittest.TestCase):
    def test_codex_runner_uses_public_models_api_and_effective_runtime(self):
        captured = {}
        module = ModuleType("openai_codex")

        class CodexConfig:
            def __init__(self, **kwargs):
                captured["config"] = kwargs

        class AsyncCodex:
            def __init__(self, config):
                captured["client_config"] = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def models(self, *, include_hidden):
                captured["include_hidden"] = include_hidden
                return SimpleNamespace(data=[SimpleNamespace(model="gpt-5.6-sol")])

        module.CodexConfig = CodexConfig
        module.AsyncCodex = AsyncCodex
        agent = _agent(command="codex", env={"OPENAI_API_KEY": "secret"})
        with (
            mock.patch.dict(sys.modules, {"openai_codex": module}),
            mock.patch(
                "agent_collab.backends.common.model_discovery.shutil.which",
                return_value="/opt/codex",
            ),
        ):
            response = asyncio.run(default_sdk_runner("codex_sdk", agent, 8.0))
        self.assertEqual(parse_codex_sdk_models(response), ("gpt-5.6-sol",))
        self.assertEqual(captured["config"]["codex_bin"], "/opt/codex")
        self.assertEqual(captured["config"]["env"]["OPENAI_API_KEY"], "secret")
        self.assertFalse(captured["include_hidden"])

    def test_xai_runner_passes_agent_scoped_key_and_closes_client(self):
        captured = {}
        module = ModuleType("xai_sdk")

        class AsyncClient:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.models = SimpleNamespace(list_language_models=self._list)

            async def _list(self):
                return [SimpleNamespace(name="grok-4-0709", aliases=["grok-4.5"])]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                captured["closed"] = True

        module.AsyncClient = AsyncClient
        agent = _agent(env={"XAI_API_KEY": "secret"})
        with mock.patch.dict(sys.modules, {"xai_sdk": module}):
            response = asyncio.run(default_sdk_runner("xai_sdk", agent, 3.0))
        self.assertEqual(parse_xai_sdk_models(response), ("grok-4-0709", "grok-4.5"))
        self.assertEqual(captured["kwargs"], {"timeout": 3.0, "api_key": "secret"})
        self.assertTrue(captured["closed"])


class CacheTests(unittest.TestCase):
    def _observation(
        self, *, status="ok", complete=True, fingerprint="fp", checked="t", models=("m",)
    ):
        return ModelCatalogObservation(
            backend_id="antigravity_cli",
            status=status,
            models=tuple(models),
            source="cli",
            complete=complete,
            checked_at=checked,
            last_attempt_at=checked,
            source_fingerprint=fingerprint,
            last_success_at=checked if status == "ok" else None,
        )

    def test_store_writes_private_0600_file(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            cache.store(self._observation(checked="2026-07-22T00:00:00+00:00", fingerprint="fp"))
            path = cache.path_for("antigravity_cli")
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["backend_id"], "antigravity_cli")

    def test_read_returns_fresh_within_ttl(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock("2026-07-22T01:00:00+00:00"))
            cache.store(self._observation(checked="2026-07-22T00:00:00+00:00", fingerprint="fp"))
            served = cache.read("antigravity_cli", fingerprint="fp")
            self.assertIsNotNone(served)
            self.assertFalse(served.stale)

    def test_read_flags_stale_past_ttl(self):
        with TemporaryDirectory() as tmp:
            # 25h after checked_at (> 24h TTL).
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock("2026-07-23T01:00:00+00:00"))
            cache.store(self._observation(checked="2026-07-22T00:00:00+00:00", fingerprint="fp"))
            served = cache.read("antigravity_cli", fingerprint="fp")
            self.assertIsNotNone(served)
            self.assertTrue(served.stale)
            # TTL flags but never deletes.
            self.assertTrue(cache.path_for("antigravity_cli").exists())

    def test_fingerprint_mismatch_invalidates(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            cache.store(self._observation(fingerprint="old"))
            self.assertIsNone(cache.read("antigravity_cli", fingerprint="new"))

    def test_unknown_schema_version_is_discarded(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            path = cache.path_for("antigravity_cli")
            payload = self._observation(fingerprint="fp").to_dict()
            payload["schema_version"] = SCHEMA_VERSION + 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(cache.read("antigravity_cli", fingerprint="fp"))
            self.assertFalse(path.exists())  # discarded

    def test_corrupt_json_is_discarded(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            path = cache.path_for("antigravity_cli")
            path.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(cache.read("antigravity_cli", fingerprint="fp"))
            self.assertFalse(path.exists())

    def test_failed_probe_does_not_overwrite_last_known_good(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            good = self._observation(models=("gemini-3.6-flash-high",), fingerprint="fp")
            cache.store(good)
            failure = ModelCatalogObservation(
                backend_id="antigravity_cli",
                status="error",
                models=(),
                source="cli",
                complete=False,
                checked_at="later",
                last_attempt_at="later",
                source_fingerprint="fp",
                reason_code="nonzero_exit",
            )
            cache.store(failure)
            served = cache.read("antigravity_cli", fingerprint="fp")
            self.assertIsNotNone(served)
            self.assertEqual(served.observation.status, "ok")
            self.assertEqual(served.observation.models, ("gemini-3.6-flash-high",))

    def test_failed_probe_does_not_overwrite_good_even_on_fingerprint_change(self):
        # Config changed (new fingerprint) and the new probe failed: the old
        # good catalog is preserved so a flap back to the old config recovers it,
        # rather than being blanked by the transient failure.
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            good = self._observation(models=("gemini-3.6-flash-high",), fingerprint="old")
            cache.store(good)
            failure = ModelCatalogObservation(
                backend_id="antigravity_cli",
                status="error",
                models=(),
                source="cli",
                complete=False,
                checked_at="later",
                last_attempt_at="later",
                source_fingerprint="new",
                reason_code="nonzero_exit",
            )
            cache.store(failure)
            # New config sees nothing (mismatch) -> static; old config still good.
            self.assertIsNone(cache.read("antigravity_cli", fingerprint="new"))
            served = cache.read("antigravity_cli", fingerprint="old")
            self.assertIsNotNone(served)
            self.assertEqual(served.observation.models, ("gemini-3.6-flash-high",))

    def test_future_checked_at_counts_as_stale(self):
        # Clock stepped back (or cache moved from a forward-drift machine):
        # negative age must not pin the entry fresh forever.
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock("2026-07-21T00:00:00+00:00"))
            cache.store(self._observation(checked="2026-07-22T00:00:00+00:00", fingerprint="fp"))
            served = cache.read("antigravity_cli", fingerprint="fp")
            self.assertIsNotNone(served)
            self.assertTrue(served.stale)

    def test_failure_persists_when_no_last_known_good(self):
        with TemporaryDirectory() as tmp:
            cache = ModelCatalogCache(Path(tmp), now=_fixed_clock())
            failure = ModelCatalogObservation(
                backend_id="antigravity_cli",
                status="error",
                models=(),
                source="cli",
                complete=False,
                checked_at="2026-07-22T00:00:00+00:00",
                last_attempt_at="2026-07-22T00:00:00+00:00",
                source_fingerprint="fp",
                reason_code="nonzero_exit",
            )
            cache.store(failure)
            served = cache.read("antigravity_cli", fingerprint="fp")
            self.assertIsNotNone(served)
            self.assertEqual(served.observation.status, "error")

    def test_ttl_default_is_24h(self):
        self.assertEqual(DEFAULT_TTL_SECONDS, 24 * 60 * 60)

    def test_unsafe_backend_id_cannot_escape_cache_dir(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cache").mkdir()
            sibling = root / "sessions"
            sibling.mkdir()
            victim = sibling / "keep.json"
            victim.write_text("important", encoding="utf-8")
            cache = ModelCatalogCache(root / "cache", now=_fixed_clock())
            evil = ModelCatalogObservation(
                backend_id="x/../../sessions/keep",
                status="ok",
                models=("m",),
                source="cli",
                complete=True,
                checked_at="2026-07-22T00:00:00+00:00",
                last_attempt_at="2026-07-22T00:00:00+00:00",
                source_fingerprint="fp",
            )
            with self.assertRaises(ValueError):
                cache.path_for("x/../../sessions/keep")
            # A trailing newline must not slip past the anchor either.
            with self.assertRaises(ValueError):
                cache.path_for("antigravity_cli\n")
            with self.assertRaises(ValueError):
                cache.store(evil)
            with self.assertRaises(ValueError):
                cache.read("x/../../sessions/keep", fingerprint="fp")
            # The unrelated file outside the cache tree is untouched.
            self.assertEqual(victim.read_text(), "important")


class RoundTripTests(unittest.TestCase):
    def test_observation_survives_dict_round_trip(self):
        obs = ModelCatalogObservation(
            backend_id="xai_cli",
            status="ok",
            models=("grok-4.5",),
            source="cli",
            complete=True,
            checked_at="2026-07-22T00:00:00+00:00",
            last_attempt_at="2026-07-22T00:00:00+00:00",
            source_fingerprint="fp",
            last_success_at="2026-07-22T00:00:00+00:00",
            reason_code=None,
        )
        self.assertEqual(ModelCatalogObservation.from_dict(obs.to_dict()), obs)

    def test_from_dict_rejects_malformed(self):
        self.assertIsNone(ModelCatalogObservation.from_dict("nope"))
        self.assertIsNone(ModelCatalogObservation.from_dict({"backend_id": "x"}))
        self.assertIsNone(ModelCatalogObservation.from_dict({**self._valid(), "models": [1, 2]}))

    def _valid(self):
        return ModelCatalogObservation(
            backend_id="xai_cli",
            status="ok",
            models=("grok-4.5",),
            source="cli",
            complete=True,
            checked_at="t",
            last_attempt_at="t",
            source_fingerprint="fp",
        ).to_dict()


if __name__ == "__main__":
    unittest.main()
