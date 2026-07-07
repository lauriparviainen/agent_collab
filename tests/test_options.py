import tempfile
import unittest
from pathlib import Path

from agent_collab.config import AgentConfig, builtin_config, load_config
from agent_collab.options import StartOptionsError, apply_agent_options, describe_options, validate_start_options


def _write_config(root: Path, text: str) -> None:
    path = root / ".agent-collab" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class StartOptionsTests(unittest.TestCase):
    def test_valid_codex_and_claude_options_pass(self):
        config = builtin_config()

        validated = validate_start_options(
            config,
            "codex-leads",
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
                "claude-leads",
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

    def test_mode_inapplicable_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            _write_config(
                root,
                """
[modes.claude-only]
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
                validate_start_options(config, "codex-leads", codex_options={"model": "gpt-5", "reasoning_effort": "high"})

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

            validated = validate_start_options(config, "claude-leads")

        self.assertEqual(validated["claude_options"]["model"], "opus")
        self.assertEqual(validated["claude_options"]["thinking_level"], "high")

    def test_describe_options_returns_modes_agents_and_schemas(self):
        config = builtin_config()

        payload = describe_options(config, Path("."))

        self.assertIn("agents", payload)
        self.assertIn("modes", payload)
        self.assertIn("codex_options", payload)
        self.assertIn("claude_options", payload)
        self.assertIn("reasoning_effort", payload["codex_options"]["properties"])
        self.assertIn("thinking_level", payload["codex_options"]["properties"])
        self.assertIn("thinking_level", payload["claude_options"]["properties"])
        self.assertIn("thinking_budget_tokens", payload["claude_options"]["properties"])

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
                "codex-leads",
                codex_options={"thinking_level": "low", "reasoning_effort": "high"},
            )
        codex_messages = {detail["path"]: detail["message"] for detail in codex_ctx.exception.to_dict()["details"]}
        self.assertIn("codex_options.thinking_level", codex_messages)

        with self.assertRaises(StartOptionsError) as claude_ctx:
            validate_start_options(
                config,
                "claude-leads",
                claude_options={"thinking_level": "high", "thinking_budget_tokens": 8192},
            )
        claude_messages = {detail["path"]: detail["message"] for detail in claude_ctx.exception.to_dict()["details"]}
        self.assertIn("claude_options.thinking_level", claude_messages)


if __name__ == "__main__":
    unittest.main()
