import tempfile
import unittest
from pathlib import Path

from agent_collab.backends.base import (
    CREDENTIALS_OK,
    HEALTH_OK,
    BackendHealth,
)
from agent_collab.config import AgentConfig, builtin_config, load_config
from agent_collab.options import (
    StartOptionsError,
    apply_agent_options,
    build_session_settings,
    describe_options,
    validate_start_backends,
    validate_start_options,
)


def _ok_health(backend):
    return BackendHealth(status=HEALTH_OK, credentials=CREDENTIALS_OK, version="1.0.0", checked_at="t")


def _write_config(root: Path, text: str) -> None:
    path = root / ".agent-collab" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class StartOptionsTests(unittest.TestCase):
    def test_valid_codex_and_claude_options_pass(self):
        config = builtin_config()

        validated = validate_start_options(
            config,
            "cross-review",
            codex_options={"thinking_level": "xhigh", "sandbox": "workspace-write", "search": False},
            claude_options={"model": "sonnet", "thinking_level": "max"},
        )

        self.assertEqual(validated["codex_options"]["thinking_level"], "xhigh")
        self.assertEqual(validated["codex_options"]["reasoning_effort"], "xhigh")
        self.assertEqual(validated["claude_options"]["model"], "sonnet")
        self.assertEqual(validated["claude_options"]["thinking_level"], "max")

    def test_unknown_wrong_type_and_unsupported_values_are_reported_together(self):
        config = builtin_config()

        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_options(
                config,
                "cross-review",
                codex_options={"reasoning_effort": "maximum", "extra": True},
                claude_options={"thinking_budget_tokens": "large"},
            )

        details = ctx.exception.to_dict()["details"]
        by_path = {detail["path"]: detail["message"] for detail in details}
        self.assertIn("codex_options.extra", by_path)
        self.assertIn("codex_options.reasoning_effort", by_path)
        self.assertIn("maximum", by_path["codex_options.reasoning_effort"])
        self.assertIn("claude_options.thinking_budget_tokens", by_path)
        self.assertIn("integer", by_path["claude_options.thinking_budget_tokens"])

    def test_workflow_inapplicable_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            _write_config(
                root,
                """
[workflows.claude-only]
sequence = ["claude"]
""",
            )
            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})

            with self.assertRaises(StartOptionsError) as ctx:
                validate_start_options(config, "claude-only", codex_options={"model": "gpt-5-codex"})

        details = ctx.exception.to_dict()["details"]
        self.assertEqual(details[0]["path"], "codex_options")
        self.assertIn("does not apply", details[0]["message"])

    def test_configured_allowed_values_tighten_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            _write_config(
                root,
                """
[agents.codex.options]
model.allowed = ["gpt-5-codex"]
reasoning_effort.allowed = ["low", "medium"]
""",
            )
            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})

            with self.assertRaises(StartOptionsError) as ctx:
                validate_start_options(config, "cross-review", codex_options={"model": "gpt-5", "reasoning_effort": "high"})

        messages = {detail["path"]: detail["message"] for detail in ctx.exception.to_dict()["details"]}
        self.assertIn("gpt-5-codex", messages["codex_options.model"])
        self.assertIn("low, medium", messages["codex_options.reasoning_effort"])

    def test_configured_defaults_are_returned_from_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            _write_config(
                root,
                """
[agents.claude.options]
model.default = "opus"
model.allowed = ["sonnet", "opus"]
thinking_level.default = "high"
thinking_level.allowed = ["low", "medium", "high", "xhigh", "max"]
""",
            )
            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})

            validated = validate_start_options(config, "cross-review")

        self.assertEqual(validated["claude_options"]["model"], "opus")
        self.assertEqual(validated["claude_options"]["thinking_level"], "high")

    def test_build_session_settings_reflects_effective_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            _write_config(
                root,
                """
[agents.claude.options]
model.default = "opus"
thinking_level.default = "high"

[agents.codex.options]
thinking_level.default = "high"
""",
            )
            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})
            normalized = validate_start_options(config, "cross-review", claude_options={"model": "sonnet"})

            settings = build_session_settings(config, "cross-review", normalized)

        self.assertEqual(settings["workflow"], {"name": "cross-review", "sequence": ["claude", "codex", "claude"]})
        claude = settings["agents"]["claude"]
        codex = settings["agents"]["codex"]
        self.assertEqual(claude["type"], "claude")
        self.assertEqual(claude["model"], "sonnet")
        self.assertEqual(claude["thinking_level"], "high")
        self.assertEqual(codex["thinking_level"], "high")
        self.assertIn("--model", claude["command_preview"])
        self.assertIn("sonnet", claude["command_preview"])
        self.assertEqual(
            claude["command_preview"],
            apply_agent_options(
                [config.agents["claude"].command] + list(config.agents["claude"].args),
                config.agents["claude"],
                normalized["claude_options"],
            ),
        )

    def test_build_session_settings_omits_unavailable_fields(self):
        config = builtin_config()
        normalized = validate_start_options(config, "single-claude")

        settings = build_session_settings(config, "single-claude", normalized)

        claude = settings["agents"]["claude"]
        self.assertEqual(set(settings["agents"]), {"claude"})
        self.assertNotIn("model", claude)
        self.assertNotIn("sandbox", claude)
        self.assertEqual(claude["command_preview"][0], "claude")

    def test_build_session_settings_command_preview_has_no_prompt(self):
        config = builtin_config()
        normalized = validate_start_options(config, "cross-review")

        settings = build_session_settings(config, "cross-review", normalized)

        for agent in settings["agents"].values():
            for part in agent.get("command_preview", []):
                self.assertNotIn("TASK", part)

    def test_describe_options_returns_workflows_agents_and_schemas(self):
        config = builtin_config()

        payload = describe_options(config, Path("."))

        self.assertIn("agents", payload)
        self.assertIn("workflows", payload)
        self.assertIn("codex_options", payload)
        self.assertIn("claude_options", payload)
        self.assertIn("reasoning_effort", payload["codex_options"]["properties"])
        self.assertIn("thinking_level", payload["codex_options"]["properties"])
        self.assertIn("thinking_level", payload["claude_options"]["properties"])
        self.assertIn("thinking_budget_tokens", payload["claude_options"]["properties"])

    def test_describe_options_includes_backends_section(self):
        config = builtin_config()

        payload = describe_options(config, Path("."), health=_ok_health)

        self.assertIn("backends", payload)
        self.assertIn("antigravity_options", payload)
        for agent_type in ("claude", "codex"):
            section = payload["backends"][agent_type]
            self.assertEqual(section["default"], "cli")
            self.assertEqual(section["backends"], ["cli"])
            entry = section["entries"]["cli"]
            self.assertTrue(entry["available"])
            self.assertEqual(
                entry["capabilities"], {"resume": False, "interrupt": False, "tool_gate": False}
            )
            self.assertEqual(entry["health"]["status"], "ok")

    def test_build_session_settings_records_backend_and_capabilities(self):
        config = builtin_config()
        normalized = validate_start_options(config, "single-claude")
        selection = validate_start_backends(config, "single-claude")

        settings = build_session_settings(
            config, "single-claude", normalized, agent_backends=selection.agent_backends
        )

        claude = settings["agents"]["claude"]
        self.assertEqual(claude["backend"], "cli")
        self.assertEqual(
            claude["capabilities"], {"resume": False, "interrupt": False, "tool_gate": False}
        )
        self.assertEqual(claude["command_preview"][0], "claude")

    def test_describe_options_reports_configured_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            _write_config(
                root,
                """
[agents.claude.options]
model.default = "opus"
thinking_level.default = "high"
""",
            )
            config = load_config(root, env={"AGENT_COLLAB_HOME": str(home)})

            payload = describe_options(config, root)

        self.assertEqual(payload["claude_options"]["properties"]["model"]["default"], "opus")
        self.assertEqual(payload["claude_options"]["properties"]["thinking_level"]["default"], "high")

    def test_apply_agent_options_maps_to_explicit_flags(self):
        agent = AgentConfig(
            id="claude",
            type="claude",
            command="claude",
            args=["-p", "--model", "sonnet", "--output-format", "stream-json"],
        )

        command = apply_agent_options(
            ["claude"] + agent.args,
            agent,
            {"model": "opus", "permission_mode": "default", "thinking_level": "high"},
        )

        self.assertEqual(command.count("--model"), 1)
        self.assertIn("--permission-mode", command)
        self.assertIn("--effort", command)
        self.assertIn("high", command)
        self.assertIn("opus", command)
        self.assertNotIn("sonnet", command)

    def test_apply_agent_options_uses_configured_defaults(self):
        agent = AgentConfig(
            id="claude",
            type="claude",
            command="claude",
            args=["-p", "--output-format", "stream-json"],
            options={
                "model": {"default": "opus"},
                "thinking_level": {"default": "high"},
            },
        )

        command = apply_agent_options(["claude"] + agent.args, agent, {})

        self.assertIn("--model", command)
        self.assertIn("opus", command)
        self.assertIn("--effort", command)
        self.assertIn("high", command)

    def test_apply_codex_thinking_level_uses_config_override(self):
        agent = AgentConfig(
            id="codex",
            type="codex",
            command="codex",
            args=["exec", "--json", "--reasoning-effort", "low", "-c", 'model_reasoning_effort="medium"'],
        )

        command = apply_agent_options(["codex"] + agent.args, agent, {"thinking_level": "high"})

        self.assertNotIn("--reasoning-effort", command)
        self.assertEqual(command.count("-c"), 1)
        self.assertIn('model_reasoning_effort="high"', command)

    def test_conflicting_thinking_aliases_are_rejected(self):
        config = builtin_config()

        with self.assertRaises(StartOptionsError) as codex_ctx:
            validate_start_options(
                config,
                "cross-review",
                codex_options={"thinking_level": "low", "reasoning_effort": "high"},
            )
        codex_messages = {detail["path"]: detail["message"] for detail in codex_ctx.exception.to_dict()["details"]}
        self.assertIn("codex_options.thinking_level", codex_messages)

        with self.assertRaises(StartOptionsError) as claude_ctx:
            validate_start_options(
                config,
                "cross-review",
                claude_options={"thinking_level": "high", "thinking_budget_tokens": 8192},
            )
        claude_messages = {detail["path"]: detail["message"] for detail in claude_ctx.exception.to_dict()["details"]}
        self.assertIn("claude_options.thinking_level", claude_messages)


if __name__ == "__main__":
    unittest.main()
