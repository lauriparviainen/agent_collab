"""Backend health helper tests."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_collab import backends
from agent_collab.backends.base import (
    CREDENTIALS_MISSING,
    CREDENTIALS_OK,
    CREDENTIALS_UNKNOWN,
    HEALTH_OK,
    HEALTH_UNAVAILABLE,
    BackendCapabilities,
    BackendHealth,
)
from agent_collab.backends.common.health import (
    HealthCache,
    antigravity_credentials,
    probe_cli_backend,
    probe_sdk_backend,
)
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig
from agent_collab.options import StartOptionsError, validate_start_backends


class _WhichFake:
    def __init__(self, present):
        self.present = present

    def __call__(self, binary):
        return f"/usr/bin/{binary}" if self.present else None


class _CliProbeBackend:
    """Minimal backend whose probe reads a mutable fake PATH."""

    id = "cli"
    agent_type = "antigravity"

    def __init__(self, which):
        self._which = which
        self.capabilities = BackendCapabilities()

    def probe(self):
        return probe_cli_backend("agy", which=self._which, run_version=None, credentials=None)


class CliProbeTests(unittest.TestCase):
    def test_missing_binary_is_unavailable_with_reason(self):
        health = probe_cli_backend("agy", which=_WhichFake(False), run_version=None, now=lambda: "t")
        self.assertEqual(health.status, HEALTH_UNAVAILABLE)
        self.assertIn("agy", health.reason)
        self.assertIn("not found", health.reason)
        self.assertEqual(health.checked_at, "t")

    def test_missing_binary_never_runs_version_or_credentials(self):
        calls = []
        probe_cli_backend(
            "agy",
            which=_WhichFake(False),
            run_version=lambda *a: calls.append("version"),
            credentials=lambda: calls.append("creds") or CREDENTIALS_OK,
        )
        # No model call, no version subprocess, no credential read when absent.
        self.assertEqual(calls, [])

    def test_present_binary_reports_ok_with_version_and_credentials(self):
        health = probe_cli_backend(
            "agy",
            which=_WhichFake(True),
            run_version=lambda binary, path: "1.1.0",
            credentials=lambda: CREDENTIALS_OK,
        )
        self.assertEqual(health.status, HEALTH_OK)
        self.assertEqual(health.version, "1.1.0")
        self.assertEqual(health.credentials, CREDENTIALS_OK)


class SdkProbeTests(unittest.TestCase):
    def test_absent_module_is_unavailable_with_extra_hint(self):
        health = probe_sdk_backend(
            "google.antigravity",
            find_spec=lambda name: None,
            extra_hint="install the antigravity-sdk extra",
        )
        self.assertEqual(health.status, HEALTH_UNAVAILABLE)
        self.assertIn("google.antigravity", health.reason)
        self.assertIn("antigravity-sdk", health.reason)

    def test_import_error_is_treated_as_unavailable(self):
        def boom(name):
            raise ModuleNotFoundError("No module named 'google'")

        health = probe_sdk_backend("google.antigravity", find_spec=boom)
        self.assertEqual(health.status, HEALTH_UNAVAILABLE)

    def test_present_module_reports_ok_with_version(self):
        health = probe_sdk_backend(
            "google.antigravity",
            find_spec=lambda name: object(),
            package_version=lambda: "0.1.5",
        )
        self.assertEqual(health.status, HEALTH_OK)
        self.assertEqual(health.version, "0.1.5")


class AntigravityCredentialsTests(unittest.TestCase):
    def test_token_file_present_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "antigravity-cli").mkdir()
            (base / "antigravity-cli" / "antigravity-oauth-token").write_text("tok", encoding="utf-8")
            self.assertEqual(antigravity_credentials(base), CREDENTIALS_OK)

    def test_active_account_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "google_accounts.json").write_text(json.dumps({"active": "me@x"}), encoding="utf-8")
            self.assertEqual(antigravity_credentials(base), CREDENTIALS_OK)

    def test_no_token_and_no_account_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(antigravity_credentials(Path(tmp)), CREDENTIALS_MISSING)

    def test_accounts_without_active_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "google_accounts.json").write_text(json.dumps({"old": "x"}), encoding="utf-8")
            self.assertEqual(antigravity_credentials(base), CREDENTIALS_MISSING)

    def test_unreadable_accounts_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "google_accounts.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(antigravity_credentials(base), CREDENTIALS_UNKNOWN)


class HealthCacheTests(unittest.TestCase):
    def test_installing_agy_flips_status_on_fresh_probe_and_after_ttl(self):
        clock = [0.0]
        cache = HealthCache(ttl_seconds=60.0, clock=lambda: clock[0])
        which = _WhichFake(present=False)
        backend = _CliProbeBackend(which)

        first = cache.health(backend)
        self.assertEqual(first.status, HEALTH_UNAVAILABLE)

        # agy is installed on PATH; no daemon restart.
        which.present = True

        # Within TTL the cached (stale) unavailable is returned...
        clock[0] = 30.0
        self.assertEqual(cache.health(backend).status, HEALTH_UNAVAILABLE)

        # ...but a fresh probe (the start path) flips immediately.
        self.assertEqual(cache.health(backend, fresh=True).status, HEALTH_OK)

        # ...and after the TTL elapses the cached path re-probes too.
        clock[0] = 200.0
        self.assertEqual(cache.health(backend).status, HEALTH_OK)

    def test_observation_reports_cache_hit_age_and_ttl(self):
        clock = [10.0]
        cache = HealthCache(ttl_seconds=60.0, clock=lambda: clock[0])
        backend = _CliProbeBackend(_WhichFake(True))
        first = cache.observe(backend)
        clock[0] = 25.0
        second = cache.observe(backend)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.age_seconds, 15.0)
        self.assertEqual(second.ttl_seconds, 60.0)


class _GatingBackend:
    id = "cli"
    agent_type = "antigravity"
    capabilities = BackendCapabilities()

    def __init__(self, block_on_unavailable=True, checks_credentials=True):
        self.block_on_unavailable = block_on_unavailable
        self.checks_credentials = checks_credentials

    def probe(self):  # pragma: no cover - health is injected in gating tests
        return BackendHealth()

    def option_schema(self, agent):
        return {}

    def normalize_options(self, agent, requested):
        return dict(requested)

    def settings_summary(self, agent, options):
        return {"backend": self.id, "options": dict(options)}

    def command_preview(self, agent, options, workdir=None):
        return None

    def create_runner(self, agent, verbose, options):  # pragma: no cover
        raise NotImplementedError


def _antigravity_config():
    return CollaborationConfig(
        agents={"ag": AgentConfig(id="ag", type="antigravity", command="agy")},
        workflows={"solo": WorkflowConfig(id="solo", sequence=["ag"])},
    )


class StartHealthGatingTests(unittest.TestCase):
    def setUp(self):
        self._original_backend = backends.get_backend("antigravity", "cli")
        backends.register(_GatingBackend())

    def tearDown(self):
        backends.unregister("antigravity", "cli")
        backends.register(self._original_backend)

    def test_unavailable_backend_rejects_start_with_reason(self):
        config = _antigravity_config()
        health = lambda at, bid: BackendHealth(status=HEALTH_UNAVAILABLE, reason="agy: command not found on PATH")
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(config, "solo", health=health)
        message = ctx.exception.to_dict()["details"][0]["message"]
        self.assertIn("unavailable", message)
        self.assertIn("command not found", message)

    def test_unavailable_backend_preserves_structured_probe_remediation(self):
        config = _antigravity_config()
        status = BackendHealth(
            status=HEALTH_UNAVAILABLE,
            reason="native runtime incompatible",
            checked_at="t",
            reason_codes=("native_runtime_incompatible",),
            remediation=({"code": "use_compatible_native_runtime", "message": "Use a compatible host."},),
        )
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(config, "solo", health=lambda *args: status)
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["code"], "native_runtime_incompatible")
        self.assertEqual(detail["canonical_backend"], "antigravity_cli")
        self.assertEqual(detail["checked_at"], "t")
        self.assertEqual(detail["remediation"][0]["code"], "use_compatible_native_runtime")

    def test_disabled_backend_rejects_before_probe_with_structured_detail(self):
        from agent_collab.config import BackendPolicyConfig

        config = _antigravity_config()
        config.backends["antigravity_cli"] = BackendPolicyConfig("antigravity_cli", False)
        calls = []
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(
                config,
                "solo",
                health=lambda *args: calls.append(args) or BackendHealth(status=HEALTH_OK),
            )
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["code"], "backend_disabled")
        self.assertEqual(detail["canonical_backend"], "antigravity_cli")
        self.assertEqual(calls, [])

    def test_missing_credentials_reject_with_sign_in_hint(self):
        config = _antigravity_config()
        health = lambda at, bid: BackendHealth(status=HEALTH_OK, credentials=CREDENTIALS_MISSING, reason="no token")
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_backends(config, "solo", health=health)
        message = ctx.exception.to_dict()["details"][0]["message"]
        self.assertIn("credentials", message)
        self.assertIn("sign in", message)

    def test_unknown_credentials_warn_but_do_not_block(self):
        config = _antigravity_config()
        health = lambda at, bid: BackendHealth(status=HEALTH_OK, credentials=CREDENTIALS_UNKNOWN)
        selection = validate_start_backends(config, "solo", health=health)
        self.assertEqual(selection.agent_backends, {"ag": "cli"})
        self.assertTrue(selection.warnings)
        self.assertIn("credentials", selection.warnings[0]["message"])

    def test_unknown_status_warns_but_does_not_block(self):
        config = _antigravity_config()
        health = lambda at, bid: BackendHealth(status="unknown", reason="probe indeterminate")
        selection = validate_start_backends(config, "solo", health=health)
        self.assertEqual(selection.agent_backends, {"ag": "cli"})
        self.assertTrue(selection.warnings)
        self.assertIn("availability is unknown", selection.warnings[0]["message"])

    def test_available_backend_with_ok_credentials_is_clean(self):
        config = _antigravity_config()
        health = lambda at, bid: BackendHealth(status=HEALTH_OK, credentials=CREDENTIALS_OK)
        selection = validate_start_backends(config, "solo", health=health)
        self.assertEqual(selection.warnings, [])

    def test_non_blocking_backend_is_never_probed_or_gated(self):
        # Rebuild the registry entry as a non-gating backend (claude/codex-like):
        # even an "unavailable" health must not be probed or block the start.
        backends.unregister("antigravity", "cli")
        backends.register(_GatingBackend(block_on_unavailable=False, checks_credentials=False))
        probed = []

        def health(at, bid):
            probed.append((at, bid))
            return BackendHealth(status=HEALTH_UNAVAILABLE, reason="should not matter")

        selection = validate_start_backends(_antigravity_config(), "solo", health=health)
        self.assertEqual(selection.agent_backends, {"ag": "cli"})
        self.assertEqual(probed, [])  # skipped entirely, no probe


if __name__ == "__main__":
    unittest.main()
