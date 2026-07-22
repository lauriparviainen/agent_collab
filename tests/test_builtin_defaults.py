import tempfile
import unittest
from datetime import time, timedelta
from pathlib import Path

from agent_collab import backends
from agent_collab.config import (
    BACKEND_DEFAULTS_FILENAME,
    BACKEND_DEFAULTS_ROOT,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    _compose_builtin_config_data,
    _load_builtin_config,
    _parse_toml_subset,
    builtin_config,
    load_toml_file,
)


CENTRAL_DEFAULTS = """\
schema_version = 9

[system]
timezone = "local"

[usage_windows]
days = ["sat"]
work_time = { start = "10:00", end = "12:00" }
interval = "7h"
jitter = "11m"

[workflows.solo]
sequence = ["claude_cli"]
"""


def _fragment(
    canonical="claude_cli",
    *,
    enabled="true",
    options='model = "opus"\nthinking_level = "high"\npermission_mode = "default"',
    target_id="claude_cli_sonnet",
    target_enabled="false",
    target_backend=None,
    target_options='thinking_level = "low"\npermission_mode = "plan"',
    extra_backend="",
    extra_usage="",
):
    target_backend = canonical if target_backend is None else target_backend
    command = 'command = "claude"\nargs = ["-p"]\n' if canonical == "claude_cli" else ""
    target = ""
    if target_id is not None:
        target = f"""
[usage_windows.targets.{target_id}]
enabled = {target_enabled}
backend = "{target_backend}"
model = "sonnet"
"""
        if target_options:
            target += f"""
[usage_windows.targets.{target_id}.options]
{target_options}
"""
    return f"""\
[backends.{canonical}]
enabled = {enabled}
{command}{extra_backend}
[backends.{canonical}.options]
{options}
{target}{extra_usage}
"""


class BuiltinDefaultsCompositionTests(unittest.TestCase):
    def _fixture(self, names=("claude_cli",), fragments=None, central=CENTRAL_DEFAULTS):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        central_path = root / "default_config.toml"
        central_path.write_text(central, encoding="utf-8")
        backend_root = root / "backends"
        for name in names:
            path = backend_root / name / BACKEND_DEFAULTS_FILENAME
            path.parent.mkdir(parents=True, exist_ok=True)
            text = (fragments or {}).get(name, _fragment(name))
            path.write_text(text, encoding="utf-8")
        return temp, central_path, backend_root

    def test_packaged_fragment_set_matches_registered_backends(self):
        registered = set(backends.registered_backend_names())
        packaged = {
            path.parent.name
            for path in BACKEND_DEFAULTS_ROOT.glob(f"*/{BACKEND_DEFAULTS_FILENAME}")
        }
        self.assertEqual(packaged, registered)
        central = load_toml_file(DEFAULT_CONFIG_PATH)
        self.assertNotIn("backends", central)
        self.assertNotIn("targets", central["usage_windows"])

    def test_effective_packaged_defaults_are_unchanged(self):
        config = builtin_config()
        self.assertEqual(
            {name: section.enabled for name, section in config.backends.items()},
            {
                "antigravity_cli": True,
                "antigravity_sdk": False,
                "claude_cli": True,
                "claude_sdk": False,
                "codex_cli": True,
                "codex_sdk": False,
                "xai_cli": True,
                "xai_sdk": False,
            },
        )
        self.assertEqual(len(config.usage_windows.targets), 8)
        self.assertTrue(all(not target.enabled for target in config.usage_windows.targets.values()))
        self.assertEqual(config.backends["claude_cli"].default_options["model"], "opus")
        self.assertEqual(config.backends["codex_cli"].default_options["sandbox"], "read-only")
        self.assertEqual(config.backends["antigravity_cli"].default_options["mode"], "plan")
        self.assertEqual(config.backends["xai_cli"].default_options["model"], "grok-4.5")
        expected_backends = {
            "antigravity_cli": (
                "agy",
                ["-p"],
                {"model": "Gemini 3.6 Flash (High)", "mode": "plan"},
            ),
            "antigravity_sdk": (None, [], {"model": "Gemini 3.6 Flash (High)"}),
            "claude_cli": (
                "claude",
                ["-p", "--output-format", "stream-json", "--verbose"],
                {"model": "opus", "thinking_level": "high", "permission_mode": "default"},
            ),
            "claude_sdk": (
                None,
                [],
                {"model": "opus", "thinking_level": "high", "permission_mode": "default"},
            ),
            "codex_cli": (
                "codex",
                ["exec", "--json"],
                {"model": "gpt-5.6-sol", "thinking_level": "high", "sandbox": "read-only"},
            ),
            "codex_sdk": (
                None,
                [],
                {"model": "gpt-5.6-sol", "thinking_level": "high", "sandbox": "read-only"},
            ),
            "xai_cli": (
                "grok",
                ["--no-auto-update", "--output-format", "streaming-json", "-p"],
                {
                    "model": "grok-4.5",
                    "thinking_level": "high",
                    "permission_mode": "bypassPermissions",
                    "sandbox": "read-only",
                },
            ),
            "xai_sdk": (None, [], {"model": "grok-4.5", "thinking_level": "high"}),
        }
        self.assertEqual(
            {
                name: (section.command, section.args, section.default_options)
                for name, section in config.backends.items()
            },
            expected_backends,
        )
        expected_targets = {
            "antigravity_cli_flash_low": (
                "antigravity_cli",
                "Gemini 3.5 Flash (Low)",
                {"mode": "plan", "sandbox": True},
            ),
            "antigravity_sdk_flash_low": (
                "antigravity_sdk",
                "Gemini 3.5 Flash (Low)",
                {},
            ),
            "claude_cli_sonnet": (
                "claude_cli",
                "sonnet",
                {"thinking_level": "low", "permission_mode": "plan"},
            ),
            "claude_sdk_sonnet": (
                "claude_sdk",
                "sonnet",
                {"thinking_level": "low", "permission_mode": "plan"},
            ),
            "codex_cli_luna": (
                "codex_cli",
                "gpt-5.6-luna",
                {"thinking_level": "low", "sandbox": "read-only"},
            ),
            "codex_sdk_luna": (
                "codex_sdk",
                "gpt-5.6-luna",
                {"thinking_level": "low", "sandbox": "read-only"},
            ),
            "xai_cli_grok_4_5": (
                "xai_cli",
                "grok-4.5",
                {"thinking_level": "low", "sandbox": "read-only", "provider_max_turns": 1},
            ),
            "xai_sdk_grok_4_5": (
                "xai_sdk",
                "grok-4.5",
                {"thinking_level": "low"},
            ),
        }
        self.assertEqual(
            {
                target_id: (target.backend, target.model, target.options)
                for target_id, target in config.usage_windows.targets.items()
            },
            expected_targets,
        )
        self.assertEqual(config.loaded_paths, [])

    def test_target_insertion_preserves_central_schedule(self):
        temp, central, root = self._fixture()
        with temp:
            config = _load_builtin_config(central, root, ["claude_cli"])
        self.assertEqual(config.usage_windows.days, ["sat"])
        self.assertEqual(config.usage_windows.work_time.start, time(10, 0))
        self.assertEqual(config.usage_windows.work_time.end, time(12, 0))
        self.assertEqual(config.usage_windows.interval, timedelta(hours=7))
        self.assertEqual(config.usage_windows.jitter, timedelta(minutes=11))
        self.assertIn("claude_cli_sonnet", config.usage_windows.targets)

    def test_composition_order_is_deterministic(self):
        fragments = {
            "claude_cli": _fragment("claude_cli", target_id="shared_a"),
            "codex_cli": _fragment(
                "codex_cli",
                options='model = "gpt-5.6-sol"\nthinking_level = "high"\nsandbox = "read-only"',
                target_id="shared_b",
                target_options='thinking_level = "low"\nsandbox = "read-only"',
            ),
        }
        temp, central, root = self._fixture(("claude_cli", "codex_cli"), fragments=fragments)
        with temp:
            first = _compose_builtin_config_data(central, root, ["codex_cli", "claude_cli"])[0]
            second = _compose_builtin_config_data(central, root, ["claude_cli", "codex_cli"])[0]
        self.assertEqual(first, second)
        self.assertEqual(list(first["backends"]), ["claude_cli", "codex_cli"])
        self.assertEqual(list(first["usage_windows"]["targets"]), ["shared_a", "shared_b"])

    def test_missing_fragment_names_backend_and_expected_path(self):
        temp, central, root = self._fixture(names=())
        with temp, self.assertRaises(ConfigError) as ctx:
            _compose_builtin_config_data(central, root, ["claude_cli"])
        self.assertIn("claude_cli", str(ctx.exception))
        self.assertIn(str(root / "claude_cli" / BACKEND_DEFAULTS_FILENAME), str(ctx.exception))

    def test_orphaned_fragment_is_rejected(self):
        temp, central, root = self._fixture()
        orphan = root / "orphan_cli" / BACKEND_DEFAULTS_FILENAME
        orphan.parent.mkdir(parents=True)
        orphan.write_text(_fragment("orphan_cli", target_id=None), encoding="utf-8")
        with temp, self.assertRaises(ConfigError) as ctx:
            _compose_builtin_config_data(central, root, ["claude_cli"])
        self.assertIn(str(orphan), str(ctx.exception))
        self.assertIn("unregistered backend", str(ctx.exception))

    def test_directory_and_declared_canonical_must_match(self):
        fragment = _fragment("codex_cli", target_id=None)
        temp, central, root = self._fixture(fragments={"claude_cli": fragment})
        source = root / "claude_cli" / BACKEND_DEFAULTS_FILENAME
        with temp, self.assertRaises(ConfigError) as ctx:
            _compose_builtin_config_data(central, root, ["claude_cli"])
        self.assertIn(str(source), str(ctx.exception))
        self.assertIn("directory is 'claude_cli'", str(ctx.exception))

    def test_scalar_backend_shapes_are_rejected_with_source(self):
        cases = ("backends = 1\n", "backends = { claude_cli = 1 }\n")
        for fragment in cases:
            with self.subTest(fragment=fragment):
                temp, central, root = self._fixture(fragments={"claude_cli": fragment})
                source = root / "claude_cli" / BACKEND_DEFAULTS_FILENAME
                with temp, self.assertRaises(ConfigError) as ctx:
                    _compose_builtin_config_data(central, root, ["claude_cli"])
                self.assertIn(str(source), str(ctx.exception))
                self.assertIn("must be a table", str(ctx.exception))

    def test_central_backend_or_target_tables_are_rejected(self):
        additions = (
            "\n[backends.claude_cli]\nenabled = true\n",
            "\n[usage_windows.targets.central]\nenabled = false\n",
        )
        for addition in additions:
            with self.subTest(addition=addition):
                temp, central, root = self._fixture(central=CENTRAL_DEFAULTS + addition)
                with temp, self.assertRaisesRegex(ConfigError, "central defaults must not"):
                    _compose_builtin_config_data(central, root, ["claude_cli"])

    def test_fragment_contract_rejects_unsafe_or_misowned_data(self):
        cases = {
            "missing enabled": _fragment().replace("enabled = true\n", "", 1),
            "enabled target": _fragment(target_enabled="true"),
            "wrong target owner": _fragment(target_backend="codex_cli"),
            "nested agents": _fragment(extra_backend="agents = {}\n"),
            "shared schedule": _fragment(extra_usage='\n[usage_windows]\ninterval = "1h"\n'),
        }
        for label, fragment in cases.items():
            with self.subTest(case=label):
                temp, central, root = self._fixture(fragments={"claude_cli": fragment})
                expected_path = root / "claude_cli" / BACKEND_DEFAULTS_FILENAME
                with temp, self.assertRaises(ConfigError) as ctx:
                    _load_builtin_config(central, root, ["claude_cli"])
                self.assertIn(str(expected_path), str(ctx.exception))

    def test_invalid_shipped_options_are_source_qualified_for_disabled_backend(self):
        fragment = _fragment(
            "claude_sdk",
            enabled="false",
            options='model = "opus"\nthinking_level = "impossible"',
            target_id=None,
        )
        central = CENTRAL_DEFAULTS.replace("claude_cli", "claude_sdk")
        temp, central_path, root = self._fixture(
            ("claude_sdk",), fragments={"claude_sdk": fragment}, central=central
        )
        source = root / "claude_sdk" / BACKEND_DEFAULTS_FILENAME
        with temp, self.assertRaises(ConfigError) as ctx:
            _load_builtin_config(central_path, root, ["claude_sdk"])
        message = str(ctx.exception)
        self.assertIn(str(source), message)
        self.assertIn("backends.claude_sdk.options.thinking_level", message)

    def test_invalid_disabled_target_options_are_source_qualified(self):
        fragment = _fragment(target_options='thinking_level = "impossible"')
        temp, central, root = self._fixture(fragments={"claude_cli": fragment})
        source = root / "claude_cli" / BACKEND_DEFAULTS_FILENAME
        with temp, self.assertRaises(ConfigError) as ctx:
            _load_builtin_config(central, root, ["claude_cli"])
        message = str(ctx.exception)
        self.assertIn(str(source), message)
        self.assertIn("usage_windows.targets.claude_cli_sonnet.options.thinking_level", message)

    def test_duplicate_target_ids_name_both_fragment_sources(self):
        fragments = {
            "claude_cli": _fragment("claude_cli", target_id="duplicate"),
            "codex_cli": _fragment(
                "codex_cli",
                options='model = "gpt-5.6-sol"\nsandbox = "read-only"',
                target_id="duplicate",
                target_options='thinking_level = "low"\nsandbox = "read-only"',
            ),
        }
        temp, central, root = self._fixture(("claude_cli", "codex_cli"), fragments=fragments)
        with temp, self.assertRaises(ConfigError) as ctx:
            _compose_builtin_config_data(central, root, ["claude_cli", "codex_cli"])
        message = str(ctx.exception)
        self.assertIn(str(root / "claude_cli" / BACKEND_DEFAULTS_FILENAME), message)
        self.assertIn(str(root / "codex_cli" / BACKEND_DEFAULTS_FILENAME), message)


class FallbackTomlValidationTests(unittest.TestCase):
    def test_duplicate_keys_and_tables_include_source(self):
        cases = (
            "value = 1\nvalue = 2\n",
            "[table]\nvalue = 1\n[table]\nother = 2\n",
            "value = { item = 1, item = 2 }\n",
            "value = { item = 1 }\n[value]\nother = 2\n",
            "value = { item = 1 }\n[value.child]\nother = 2\n",
            "value.item = 1\n[value]\nother = 2\n",
            (
                '[backends.claude_cli]\noptions = { model = "opus" }\n'
                'options.permission_mode = "plan"\n'
            ),
            ('[backends.claude_cli]\ncommand = "claude"\n[backends]\nclaude_cli.enabled = true\n'),
            (
                '[backends.claude_cli.options]\nmodel = "opus"\n[backends]\n'
                'claude_cli.options.thinking_level = "high"\n'
            ),
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ConfigError) as ctx:
                _parse_toml_subset(text, source="fragment.toml")
            self.assertIn("fragment.toml", str(ctx.exception))
            self.assertRegex(str(ctx.exception), "duplicate TOML|redefines TOML")


if __name__ == "__main__":
    unittest.main()
