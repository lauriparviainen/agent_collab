"""Production BackendCapabilities matrix.

Pins the leftover 2026-08-18 table in
``doc/tasks_open/sdk-session-control.md`` (Production capabilities). Future
flag flips must change this test deliberately.
"""

from __future__ import annotations

import unittest

from agent_collab import backends


# doc/tasks_open/sdk-session-control.md — Production capabilities (leftover 2026-08-18).
EXPECTED = {
    "claude_sdk": {
        "continuity": True,
        "resume": True,
        "interrupt": True,
        "tool_gate": True,
    },
    "antigravity_sdk": {
        "continuity": True,
        "resume": True,
        "interrupt": True,
        "tool_gate": True,
    },
    "codex_sdk": {
        "continuity": True,
        "resume": True,
        "interrupt": True,
        "tool_gate": False,
    },
    "xai_sdk": {
        "continuity": True,
        "resume": False,
        "interrupt": False,
        "tool_gate": False,
    },
    "claude_cli": {
        "continuity": False,
        "resume": False,
        "interrupt": False,
        "tool_gate": False,
    },
    "codex_cli": {
        "continuity": False,
        "resume": False,
        "interrupt": False,
        "tool_gate": False,
    },
    "xai_cli": {
        "continuity": False,
        "resume": False,
        "interrupt": False,
        "tool_gate": False,
    },
    "antigravity_cli": {
        "continuity": False,
        "resume": False,
        "interrupt": False,
        "tool_gate": False,
    },
}


class ProductionCapabilityMatrixTests(unittest.TestCase):
    def test_every_registered_backend_matches_the_leftover_table(self):
        names = backends.registered_backend_names()
        self.assertEqual(sorted(names), sorted(EXPECTED))
        for agent_type in backends.registered_agent_types():
            for backend_id in backends.registered_backends(agent_type):
                name = backends.backend_name(agent_type, backend_id)
                self.assertEqual(
                    backends.capabilities_for(agent_type, backend_id).to_dict(),
                    EXPECTED[name],
                    name,
                )


if __name__ == "__main__":
    unittest.main()
