"""Selection, workspace isolation, and assertions for live backend tests."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta, timezone
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from agent_collab import backends
from agent_collab.backends.base import CREDENTIALS_MISSING, HEALTH_UNAVAILABLE
from agent_collab.config import AgentConfig, builtin_config, load_user_config
from agent_collab.daemon import SessionManager
from agent_collab.paths import AgentCollabHome, GlobalDataPaths
from agent_collab.usage_windows import UsageWindowScheduler, anchors_for_day

PROVIDERS = {"claude", "codex", "antigravity", "xai"}
BACKENDS = {"cli", "sdk"}
BACKEND_NAMES = {f"{provider}_{backend}" for provider in PROVIDERS for backend in BACKENDS}
_selected_backend_names: Set[str] = set(BACKEND_NAMES)
_explicit_backend_names: Set[str] = set()
STRICT = False
REPO_ROOT = Path(__file__).resolve().parents[1]

# Live tests verify transport/event fidelity, not frontier-model quality. Keep
# their paid calls on each provider's fast, economical tier by default.
DEFAULT_LIVE_OPTIONS: Dict[str, Dict[str, Any]] = {
    "claude": {"model": "sonnet", "thinking_level": "low"},
    "codex": {"model": "gpt-5.6-luna", "thinking_level": "low"},
    "antigravity": {"model": "gemini-3.5-flash-low"},
    "xai": {"model": "grok-4.5", "thinking_level": "low"},
}


def configure(
    backend_names: Optional[Iterable[str]] = None,
    *,
    strict: bool = False,
) -> None:
    global _selected_backend_names, _explicit_backend_names, STRICT
    selected_names = set(backend_names or BACKEND_NAMES)
    _selected_backend_names = selected_names
    _explicit_backend_names = set(backend_names or ())
    STRICT = strict


def selected(provider: str, backend_id: str) -> bool:
    return f"{provider}_{backend_id}" in _selected_backend_names


def missing_reason(provider: str, backend_id: str, reason: str) -> str:
    name = f"{provider}_{backend_id}"
    strict = STRICT and name in _explicit_backend_names
    return f"[strict-missing] {name}: {reason}" if strict else f"[missing] {name}: {reason}"


async def _collect(runner: Any, prompt: str, workdir: Path) -> tuple[list, Any]:
    events = []

    async def emit(event: Any) -> None:
        events.append(event)

    outcome = await runner.run_turn(prompt, workdir, emit)
    return events, outcome


class LiveBackendTestCase(unittest.TestCase):
    provider = ""
    backend_id = ""
    prompt = "Reply with the single word: ready."

    def setUp(self) -> None:
        if not selected(self.provider, self.backend_id):
            self.skipTest("[unselected] backend not selected")
        backend = backends.get_backend(self.provider, self.backend_id)
        health = backend.probe()
        if health.status == HEALTH_UNAVAILABLE:
            self.skipTest(
                missing_reason(self.provider, self.backend_id, health.reason or "unavailable")
            )
        if health.credentials == CREDENTIALS_MISSING:
            self.skipTest(
                missing_reason(
                    self.provider, self.backend_id, health.reason or "credentials missing"
                )
            )

    def requested_options(self) -> Dict[str, Any]:
        options = dict(DEFAULT_LIVE_OPTIONS[self.provider])
        prefix = f"AGENT_COLLAB_IT_{self.provider.upper()}"
        if os.environ.get(f"{prefix}_MODEL"):
            options["model"] = os.environ[f"{prefix}_MODEL"]
        if "thinking_level" in options and os.environ.get(f"{prefix}_THINKING_LEVEL"):
            options["thinking_level"] = os.environ[f"{prefix}_THINKING_LEVEL"]
        return options

    def agent_backend_config(self) -> Dict[str, Any]:
        """Return live-test-only static backend configuration."""

        return {}

    def environment_overrides(self) -> Dict[str, str]:
        """Return live-test-only environment values without logging them."""

        return {}

    def prepare_workdir(self, workdir: Path) -> None:
        """Optionally prepare the disposable workspace before the provider turn."""

        return None

    def run_live(self, prompt: Optional[str] = None) -> list:
        config = builtin_config()
        source = config.agents[self.provider]
        backend_config = dict(source.backend_config)
        backend_config.update(self.agent_backend_config())
        agent = AgentConfig(
            id=source.id,
            type=source.type,
            command=source.command,
            args=list(source.args),
            enabled=True,
            env=dict(source.env),
            cwd=source.cwd,
            backend=self.backend_id,
            backend_config=backend_config,
        )
        backend = backends.get_backend(self.provider, self.backend_id)
        options = backend.normalize_options(agent, self.requested_options())
        runner = backend.create_runner(agent, True, options)
        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            workdir = Path(tmp).resolve()
            self.assertNotEqual(workdir, REPO_ROOT)
            self.assertNotIn(REPO_ROOT, workdir.parents)
            self.prepare_workdir(workdir)
            overrides = dict(self.environment_overrides())
            overrides["AGENT_COLLAB_HOME"] = home
            previous = {key: os.environ.get(key) for key in overrides}
            os.environ.update(overrides)
            try:
                events, outcome = asyncio.run(_collect(runner, prompt or self.prompt, workdir))
                self.assertEqual(
                    outcome.outcome,
                    "completed",
                    f"backend turn did not complete: {outcome.to_dict()}",
                )
                return events
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def assert_message(self, events: list) -> None:
        errors = [event.text for event in events if event.type == "error"]
        self.assertFalse(errors, f"backend emitted {len(errors)} error event(s)")
        self.assertTrue(
            any(event.source == self.provider and event.type == "message" for event in events),
            f"no provider message; event kinds={[(event.source, event.type) for event in events]}",
        )

    def assert_session_kind(self, events: list, kind: str) -> None:
        self.assertTrue(
            any((event.raw or {}).get("provider_session_kind") == kind for event in events),
            f"no {kind} identity; event kinds={[(event.source, event.type) for event in events]}",
        )

    def test_usage_window_visible_session(self) -> None:
        """Credentialed opt-in check of the scheduler's normal-session boundary."""

        if os.environ.get("AGENT_COLLAB_IT_USAGE_WINDOWS") != "1":
            self.skipTest(
                "[unselected] set AGENT_COLLAB_IT_USAGE_WINDOWS=1 for a paid scheduled call"
            )
        canonical = f"{self.provider}_{self.backend_id}"
        packaged = next(
            target
            for target in builtin_config().usage_windows.targets.values()
            if target.backend == canonical
        )
        options = {**packaged.options, **self.requested_options()}
        model = str(options.pop("model"))
        with tempfile.TemporaryDirectory(prefix="agent-collab-it-usage-home-") as temp:
            home_path = Path(temp).resolve()
            home = AgentCollabHome(home_path, home_path / "config.toml")
            paths = GlobalDataPaths.from_home(home)
            paths.ensure_dirs()
            lines = [
                "schema_version = 9",
                "",
                f"[backends.{canonical}]",
                "enabled = true",
            ]
            for key, value in self.agent_backend_config().items():
                lines.append(f"{key} = {json.dumps(value)}")
            lines.extend(
                [
                    "",
                    "[usage_windows.targets.integration_live]",
                    "enabled = true",
                    f"backend = {json.dumps(canonical)}",
                    f"model = {json.dumps(model)}",
                ]
            )
            if options:
                lines.extend(["", "[usage_windows.targets.integration_live.options]"])
                for key, value in options.items():
                    lines.append(f"{key} = {json.dumps(value)}")
            home.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            home.config_path.chmod(0o600)
            overrides = dict(self.environment_overrides())
            overrides["AGENT_COLLAB_HOME"] = str(home_path)
            previous = {key: os.environ.get(key) for key in overrides}
            os.environ.update(overrides)
            try:
                config = load_user_config()
                target = config.usage_windows.targets["integration_live"]
                zone = timezone.utc
                anchor = anchors_for_day(
                    date.today(),
                    config.usage_windows.work_time,
                    timedelta(hours=5),
                    zone,
                )[0]
                manager = SessionManager(
                    default_log_dir=paths.session_dir,
                    index_path=paths.session_index_path,
                )
                scheduler = UsageWindowScheduler(
                    config=config,
                    manager=manager,
                    paths=paths,
                    logger=lambda _message: None,
                )
                paths.usage_window_workdir.mkdir(parents=True, mode=0o700)
                result = asyncio.run(
                    scheduler._invoke_session(
                        target,
                        anchor,
                        f"uw-integration_live-{date.today().strftime('%Y%m%d')}-0900",
                    )
                )
                self.assertEqual(result["outcome"], "completed")
                sessions = manager.list_sessions()
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0].workflow, "usage-window")
                self.assertEqual(Path(sessions[0].workdir), paths.usage_window_workdir)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
