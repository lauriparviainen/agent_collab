import unittest
from pathlib import Path

from agent_collab.backends.claude_cli import ClaudeCliBackend
from agent_collab.backends.codex_cli import CodexCliBackend
from agent_collab.backends.base import BackendHealth
from agent_collab.config import AgentConfig, CollaborationConfig, WorkflowConfig, builtin_config
from agent_collab.options import (
    StartOptionsError,
    build_session_settings,
    describe_options,
    normalize_start_options,
    resolve_workflow_members,
    validate_start_options,
)


def _config() -> CollaborationConfig:
    return builtin_config()


class BackendQualifiedOptionTests(unittest.TestCase):
    def test_valid_options_are_normalized_per_backend(self):
        validated = validate_start_options(
            _config(),
            "cross-review",
            {
                "codex_cli": {
                    "thinking_level": "xhigh",
                    "sandbox": "workspace-write",
                    "search": False,
                },
                "claude_cli": {"model": "sonnet", "thinking_level": "max"},
            },
        )
        self.assertEqual(validated["codex_cli"]["reasoning_effort"], "xhigh")
        self.assertEqual(validated["claude_cli"]["model"], "sonnet")

    def test_invalid_values_have_backend_qualified_paths(self):
        with self.assertRaises(StartOptionsError) as ctx:
            validate_start_options(
                _config(),
                "cross-review",
                {
                    "codex_cli": {"reasoning_effort": "maximum", "extra": True},
                    "claude_cli": {"thinking_budget_tokens": "large"},
                },
            )
        paths = {detail["path"] for detail in ctx.exception.to_dict()["details"]}
        self.assertIn("backend_options.codex_cli.extra", paths)
        self.assertIn("backend_options.codex_cli.reasoning_effort", paths)
        self.assertIn("backend_options.claude_cli.thinking_budget_tokens", paths)

    def test_unknown_and_unselected_backend_names_are_rejected(self):
        for payload, expected in (
            ({"unknown_backend": {}}, "backend_options.unknown_backend"),
            ({"codex_cli": {"model": "gpt-5"}}, "backend_options.codex_cli"),
        ):
            with self.subTest(payload=payload), self.assertRaises(StartOptionsError) as ctx:
                validate_start_options(_config(), "solo-claude-cli", payload)
            self.assertEqual(ctx.exception.to_dict()["details"][0]["path"], expected)

    def test_defaults_come_from_backend_manifests(self):
        validated = validate_start_options(_config(), "cross-review")
        self.assertEqual(validated["claude_cli"]["model"], "opus")
        self.assertEqual(validated["claude_cli"]["thinking_level"], "high")
        self.assertEqual(validated["codex_cli"]["model"], "gpt-5.6-sol")

    def test_cli_and_sdk_options_can_coexist_for_same_provider(self):
        config = CollaborationConfig(
            agents={
                "cli": AgentConfig(id="cli", type="claude", command="claude", backend="cli"),
                "sdk": AgentConfig(id="sdk", type="claude", backend="sdk"),
            },
            workflows={"mixed": WorkflowConfig(id="mixed", sequence=["cli", "sdk"])},
        )
        normalized = normalize_start_options(
            config,
            "mixed",
            {
                "claude_cli": {"thinking_budget_tokens": 1024},
                "claude_sdk": {"thinking_level": "max"},
            },
            agent_backends={"cli": "cli", "sdk": "sdk"},
        )
        self.assertEqual(normalized.agent_options["cli"]["thinking_budget_tokens"], 1024)
        self.assertEqual(normalized.agent_options["sdk"]["thinking_level"], "max")

    def test_cross_field_conflicts_are_rejected(self):
        for payload, path in (
            (
                {"codex_cli": {"thinking_level": "low", "reasoning_effort": "high"}},
                "backend_options.codex_cli.thinking_level",
            ),
            (
                {"claude_cli": {"thinking_level": "high", "thinking_budget_tokens": 8192}},
                "backend_options.claude_cli.thinking_level",
            ),
        ):
            with self.subTest(payload=payload), self.assertRaises(StartOptionsError) as ctx:
                validate_start_options(_config(), "cross-review", payload)
            self.assertIn(path, {item["path"] for item in ctx.exception.to_dict()["details"]})

    def test_parallel_members_are_all_normalized(self):
        config = _config()
        config.workflows["parallel"] = WorkflowConfig(
            id="parallel", parallel=["claude_cli", "codex_cli"]
        )

        normalized = normalize_start_options(
            config,
            "parallel",
            agent_backends={"claude_cli": "cli", "codex_cli": "cli"},
        )

        self.assertEqual(set(normalized.agent_options), {"claude_cli", "codex_cli"})
        self.assertEqual(set(normalized.backend_options), {"claude_cli", "codex_cli"})


class DescribeOptionsTests(unittest.TestCase):
    def test_describe_exposes_one_schema_per_backend(self):
        payload = describe_options(_config())
        schema = payload["backend_options"]
        self.assertFalse(schema["additionalProperties"])
        properties = schema["properties"]
        self.assertEqual(
            set(properties),
            {
                "claude_cli",
                "claude_sdk",
                "codex_cli",
                "codex_sdk",
                "antigravity_cli",
                "antigravity_sdk",
                "xai_cli",
                "xai_sdk",
            },
        )
        self.assertIn("profile", properties["codex_cli"]["properties"])
        self.assertNotIn("profile", properties["codex_sdk"]["properties"])
        self.assertEqual(properties["claude_cli"]["properties"]["model"]["default"], "opus")

    def test_backend_health_and_capabilities_remain_discoverable(self):
        payload = describe_options(_config())
        for provider in ("claude", "codex", "antigravity", "xai"):
            section = payload["backends"][provider]
            self.assertEqual(section["backends"], ["cli", "sdk"])
            for entry in section["entries"].values():
                self.assertIn("health", entry)
                self.assertIn("capabilities", entry)

    def test_discovery_reports_canonical_effective_agent_and_workflow_backends(self):
        payload = describe_options(_config())
        self.assertEqual(payload["discovery"]["protocol_version"], 1)
        self.assertEqual(
            set(payload["canonical_backends"]),
            set(payload["backend_options"]["properties"]),
        )
        agents = {item["id"]: item for item in payload["agents"]}
        self.assertEqual(agents["claude_cli"]["canonical_backend"], "claude_cli")
        # Derived agents always carry the backend selected by their canonical
        # backend section, so the selection source is the agent configuration.
        self.assertEqual(agents["claude_cli"]["selection_source"], "agent_config")
        workflow = next(item for item in payload["workflows"] if item["id"] == "cross-review")
        self.assertEqual(
            workflow["selected_canonical_backends"],
            ["claude_cli", "codex_cli"],
        )
        self.assertEqual(workflow["effective_agents"][0]["canonical_backend"], "claude_cli")

    def test_disabled_backend_stays_registered_but_is_not_selection_eligible(self):
        from agent_collab.config import BackendPolicyConfig

        config = _config()
        config.backends["claude_cli"] = BackendPolicyConfig("claude_cli", False)
        calls = []
        payload = describe_options(
            config, health=lambda backend: calls.append(backend) or BackendHealth()
        )
        entry = payload["canonical_backends"]["claude_cli"]
        self.assertFalse(entry["policy"]["enabled"])
        self.assertEqual(entry["probe"]["status"], "not_run")
        self.assertNotIn("claude_cli", [f"{item.agent_type}_{item.id}" for item in calls])
        workflow = next(item for item in payload["workflows"] if item["id"] == "cross-review")
        self.assertFalse(workflow["start_eligible"])

    def test_parallel_workflow_exposes_members_and_requires_all_eligible(self):
        from agent_collab.backends.base import HEALTH_UNAVAILABLE
        from agent_collab.config import merge_config_data

        config = _config()
        config.workflows["parallel"] = WorkflowConfig(
            id="parallel", parallel=["claude_cli", "codex_cli"]
        )
        merge_config_data(config, {"backends": {"codex_cli": {"enabled": False}}})

        # Probes are stubbed so eligibility comes from the disabled backend,
        # not from whichever provider CLIs happen to exist on this machine.
        payload = describe_options(
            config,
            health=lambda backend: BackendHealth(status=HEALTH_UNAVAILABLE, reason="stubbed"),
        )

        workflow = next(item for item in payload["workflows"] if item["id"] == "parallel")
        self.assertEqual(workflow["sequence"], ["claude_cli", "codex_cli"])
        self.assertEqual(workflow["parallel"], ["claude_cli", "codex_cli"])
        self.assertEqual(
            [item["agent_id"] for item in workflow["effective_agents"]],
            ["claude_cli", "codex_cli"],
        )
        self.assertEqual(
            workflow["effective_agents"][1],
            {
                "agent_id": "codex_cli",
                "canonical_backend": "codex_cli",
                "selection_source": "backend_disabled",
            },
        )
        self.assertFalse(workflow["start_eligible"])
        self.assertIn("backend_disabled", workflow["ineligible_reasons"])


class WorkflowMemberSelectionTests(unittest.TestCase):
    """Start-time member selection: slot semantics, enablement, distinctness."""

    @staticmethod
    def _config_with_xai() -> CollaborationConfig:
        from agent_collab.config import merge_config_data

        config = _config()
        # xai ships disabled in the built-in config; the substitution tests
        # need a third enabled agent.
        merge_config_data(config, {"backends": {"xai_cli": {"enabled": True}}})
        return config

    def test_absent_or_empty_or_identity_selection_is_a_no_op(self):
        config = _config()
        self.assertIsNone(resolve_workflow_members(config, "cross-review", None))
        self.assertIsNone(resolve_workflow_members(config, "cross-review", {}))
        self.assertIsNone(
            resolve_workflow_members(config, "cross-review", {"claude_cli": "claude_cli"})
        )

    def test_sequence_substitution_keeps_slot_identity(self):
        # cross-review is [claude, codex, claude]: substituting the lead slot
        # must reprise in both of its positions.
        effective = resolve_workflow_members(
            self._config_with_xai(), "cross-review", {"claude_cli": "xai_cli"}
        )
        self.assertEqual(effective.sequence, ["xai_cli", "codex_cli", "xai_cli"])
        self.assertIsNone(effective.parallel)

    def test_parallel_substitution_replaces_one_group_member(self):
        effective = resolve_workflow_members(
            self._config_with_xai(), "dual-review", {"codex_cli": "xai_cli"}
        )
        self.assertEqual(effective.parallel, ["claude_cli", "xai_cli"])
        self.assertEqual(effective.sequence, [])

    def test_unknown_slot_and_unknown_agent_have_field_paths(self):
        with self.assertRaises(StartOptionsError) as ctx:
            resolve_workflow_members(
                _config(),
                "dual-review",
                {"nope_cli": "claude_cli", "codex_cli": "missing_agent"},
            )
        details = {item["path"]: item["message"] for item in ctx.exception.to_dict()["details"]}
        self.assertIn("unknown slot", details["members.nope_cli"])
        self.assertIn("unknown agent", details["members.codex_cli"])

    def test_disabled_agent_is_rejected_with_field_path(self):
        from agent_collab.config import merge_config_data

        config = _config()
        merge_config_data(config, {"backends": {"xai_cli": {"enabled": False}}})
        with self.assertRaises(StartOptionsError) as ctx:
            resolve_workflow_members(config, "dual-review", {"codex_cli": "xai_cli"})
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "members.codex_cli")
        self.assertIn("disabled backend", detail["message"])

    def test_parallel_duplicate_members_are_rejected(self):
        with self.assertRaises(StartOptionsError) as ctx:
            resolve_workflow_members(_config(), "dual-review", {"codex_cli": "claude_cli"})
        detail = ctx.exception.to_dict()["details"][0]
        self.assertEqual(detail["path"], "members.codex_cli")
        self.assertIn("must be distinct", detail["message"])

    def test_substitution_can_replace_a_disabled_configured_member(self):
        from agent_collab.config import merge_config_data

        config = self._config_with_xai()
        merge_config_data(config, {"backends": {"codex_cli": {"enabled": False}}})
        effective = resolve_workflow_members(config, "dual-review", {"codex_cli": "xai_cli"})
        self.assertEqual(effective.parallel, ["claude_cli", "xai_cli"])

    def test_non_object_selection_is_rejected(self):
        with self.assertRaises(StartOptionsError) as ctx:
            resolve_workflow_members(_config(), "dual-review", ["claude_cli"])
        self.assertEqual(ctx.exception.to_dict()["details"][0]["path"], "members")

    def test_describe_options_advertises_per_slot_member_selection(self):
        payload = describe_options(self._config_with_xai())
        self.assertTrue(payload["discovery"]["start"]["accepts_member_selection"])
        workflows = {item["id"]: item for item in payload["workflows"]}

        cross = workflows["cross-review"]["member_selection"]
        self.assertEqual(cross["start_field"], "members")
        self.assertFalse(cross["distinct_members"])
        # [a, b, a] collapses into two slots with the configured defaults.
        self.assertEqual(
            [(slot["slot"], slot["default"]) for slot in cross["slots"]],
            [("claude_cli", "claude_cli"), ("codex_cli", "codex_cli")],
        )
        for slot in cross["slots"]:
            self.assertTrue(slot["default_eligible"])
            self.assertIn("xai_cli", slot["eligible_members"])

        dual = workflows["dual-review"]["member_selection"]
        self.assertTrue(dual["distinct_members"])
        self.assertEqual(len(dual["slots"]), 2)

    def test_describe_options_marks_disabled_default_ineligible(self):
        from agent_collab.config import merge_config_data

        config = _config()
        merge_config_data(config, {"backends": {"codex_cli": {"enabled": False}}})
        payload = describe_options(config)
        dual = next(item for item in payload["workflows"] if item["id"] == "dual-review")
        slots = {slot["slot"]: slot for slot in dual["member_selection"]["slots"]}
        self.assertFalse(slots["codex_cli"]["default_eligible"])
        self.assertTrue(slots["claude_cli"]["default_eligible"])
        self.assertNotIn("codex_cli", slots["codex_cli"]["eligible_members"])


class CommandMappingTests(unittest.TestCase):
    def test_claude_cli_manifest_options_map_to_flags(self):
        agent = AgentConfig(
            id="claude",
            type="claude",
            command="claude",
            args=["-p", "--output-format", "stream-json", "--model", "old"],
        )
        backend = ClaudeCliBackend()
        options = backend.normalize_options(
            agent,
            {"model": "sonnet", "thinking_level": "high", "permission_mode": "acceptEdits"},
        )
        command = backend.build_command(agent, options)
        self.assertEqual(command.count("--model"), 1)
        self.assertIn("sonnet", command)
        self.assertIn("--effort", command)
        self.assertIn("--permission-mode", command)

    def test_codex_cli_manifest_options_map_to_config_and_flags(self):
        agent = AgentConfig(id="codex", type="codex", command="codex", args=["exec", "--json"])
        backend = CodexCliBackend()
        options = backend.normalize_options(
            agent, {"thinking_level": "high", "sandbox": "read-only", "search": True}
        )
        command = backend.build_command(agent, options)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("--sandbox", command)
        self.assertIn("--search", command)

    def test_configured_agent_options_are_session_defaults(self):
        config = CollaborationConfig(
            agents={
                "claude": AgentConfig(
                    id="claude",
                    type="claude",
                    command="claude",
                    options={"model": "sonnet"},
                )
            },
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        self.assertEqual(validate_start_options(config, "solo")["claude_cli"]["model"], "sonnet")
        overridden = validate_start_options(config, "solo", {"claude_cli": {"model": "opus"}})
        self.assertEqual(overridden["claude_cli"]["model"], "opus")

    def test_configured_raw_budget_replaces_manifest_thinking_default(self):
        config = CollaborationConfig(
            agents={
                "claude": AgentConfig(
                    id="claude",
                    type="claude",
                    command="claude",
                    options={"thinking_budget_tokens": 2048},
                )
            },
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        options = validate_start_options(config, "solo")["claude_cli"]
        self.assertEqual(options["thinking_budget_tokens"], 2048)
        self.assertNotIn("thinking_level", options)


class SessionSettingsTests(unittest.TestCase):
    def test_settings_use_backend_preview_without_prompt(self):
        config = _config()
        normalized = normalize_start_options(config, "cross-review")
        settings = build_session_settings(
            config,
            "cross-review",
            normalized.backend_options,
            agent_backends={"claude_cli": "cli", "codex_cli": "cli"},
            agent_options=normalized.agent_options,
            workdir=Path("."),
        )
        for entry in settings["agents"].values():
            self.assertIn("command_preview", entry)
            self.assertNotIn("Task", entry["command_preview"])

    def test_sdk_settings_have_no_command_preview(self):
        config = CollaborationConfig(
            agents={"claude": AgentConfig(id="claude", type="claude", backend="sdk")},
            workflows={"solo": WorkflowConfig(id="solo", sequence=["claude"])},
        )
        normalized = normalize_start_options(config, "solo", agent_backends={"claude": "sdk"})
        settings = build_session_settings(
            config,
            "solo",
            normalized.backend_options,
            agent_backends={"claude": "sdk"},
            agent_options=normalized.agent_options,
        )
        self.assertNotIn("command_preview", settings["agents"]["claude"])

    def test_settings_carry_backend_declared_brand_color(self):
        config = _config()
        normalized = normalize_start_options(config, "cross-review")
        settings = build_session_settings(
            config,
            "cross-review",
            normalized.backend_options,
            agent_backends={"claude_cli": "cli", "codex_cli": "cli"},
            agent_options=normalized.agent_options,
            workdir=Path("."),
        )
        self.assertEqual(settings["agents"]["claude_cli"]["brand_color"], "#D97757")
        self.assertEqual(settings["agents"]["codex_cli"]["brand_color"], "#10A37F")

    def test_parallel_settings_cover_every_member_and_preserve_flat_sequence(self):
        config = _config()
        config.workflows["parallel"] = WorkflowConfig(
            id="parallel", parallel=["claude_cli", "codex_cli"]
        )
        normalized = normalize_start_options(
            config,
            "parallel",
            agent_backends={"claude_cli": "cli", "codex_cli": "cli"},
        )

        settings = build_session_settings(
            config,
            "parallel",
            normalized.backend_options,
            agent_backends={"claude_cli": "cli", "codex_cli": "cli"},
            agent_options=normalized.agent_options,
            workdir=Path("."),
        )

        self.assertEqual(set(settings["agents"]), {"claude_cli", "codex_cli"})
        self.assertEqual(settings["workflow"]["sequence"], ["claude_cli", "codex_cli"])
        self.assertEqual(settings["workflow"]["parallel"], ["claude_cli", "codex_cli"])


class BrandColorRegistryTests(unittest.TestCase):
    def test_xai_uses_a_terminal_safe_monochrome_brand_color(self):
        from agent_collab import backends as backend_registry

        self.assertEqual(backend_registry.get_backend("xai", "cli").brand_color, "#A0A0A0")
        self.assertEqual(backend_registry.get_backend("xai", "sdk").brand_color, "#A0A0A0")

    def test_every_backend_declares_a_provider_uniform_brand_color(self):
        import re

        from agent_collab import backends as backend_registry

        by_provider = {}
        for agent_type in backend_registry.registered_agent_types():
            for backend_id in backend_registry.registered_backends(agent_type):
                backend = backend_registry.get_backend(agent_type, backend_id)
                color = backend.brand_color
                self.assertRegex(color, re.compile(r"^#[0-9A-Fa-f]{6}$"), (agent_type, backend_id))
                by_provider.setdefault(agent_type, set()).add(color)
        # Brand belongs to the provider: a provider's cli and sdk backends
        # must declare the identical hue.
        for provider, colors in by_provider.items():
            self.assertEqual(len(colors), 1, f"{provider} declares {colors}")


if __name__ == "__main__":
    unittest.main()
