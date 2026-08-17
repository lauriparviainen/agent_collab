"""Daemon-reload + public-resume live proof used by increment-4 backend tests.

Constructs SessionManager A, completes one interactive turn, persists, drops
that manager, restores SessionManager B on the same isolated home/index, calls
``resume_session``, then posts a delta. The production ``resume`` flag is
stubbed true only for the proof; callers flip the real flag only when this
path actually invoked resume and passed.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional
from unittest import mock

from agent_collab import backends as backend_registry
from agent_collab.backends.base import BackendCapabilities
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.paths import GlobalDataPaths
from agent_collab.session_index import SessionIndex

_REAL_CAPABILITIES_FOR = backend_registry.capabilities_for


def _resume_stub(agent_type, backend_id):
    caps = _REAL_CAPABILITIES_FOR(agent_type, backend_id)
    return BackendCapabilities(
        resume=True,
        interrupt=caps.interrupt,
        tool_gate=caps.tool_gate,
        continuity=caps.continuity,
    )


def _descriptor(state, agent_id: str) -> Mapping[str, Any]:
    sessions = state.agent_sessions or {}
    entry = sessions.get(agent_id) or {}
    if not isinstance(entry, dict):
        return {}
    return entry


async def run_reload_public_resume(
    case,
    workdir: Path,
    *,
    sandbox: str,
    members: Mapping[str, str],
    backend_options: Mapping[str, Mapping[str, Any]],
    agent_id: str,
    codeword: str,
) -> dict[str, Any]:
    """Return proof facts after a public resume on a restored manager.

    The returned dict always includes ``resumed=True`` when
    ``manager.resume_session`` was invoked. A skip or exception before that
    call must not flip a production flag.
    """

    paths = GlobalDataPaths.resolve()
    index_path = paths.session_index_path
    proof: dict[str, Any] = {"resumed": False, "sandbox": sandbox}

    with mock.patch("agent_collab.backends.capabilities_for", side_effect=_resume_stub):
        first = SessionManager(index_path=index_path, default_workdir=workdir)
        state = await first.start_session(
            StartSessionRequest(
                task=(
                    f"For this session the project id is {codeword}. "
                    "Reply exactly STORED without repeating the project id."
                ),
                workflow="solo",
                members=dict(members),
                backend_options={key: dict(value) for key, value in backend_options.items()},
                max_turns=1,
                timeout=180,
                workdir=workdir,
                sandbox=sandbox,
                interactive=True,
                interactive_idle_timeout=300,
            )
        )
        session_id = state.session_id
        proof["session_id"] = session_id
        try:
            first_result = await first.wait_result(session_id, timeout_ms=240_000)
            case.assertTrue(first_result.settled)
            if first_result.status != "awaiting_input":
                case.fail(
                    f"first turn did not park at awaiting_input: "
                    f"status={first_result.status} failure={first_result.failure}"
                )
            live = first.get_session(session_id, detail="full")
            descriptor = _descriptor(live, agent_id)
            cursor = descriptor.get("prompt_event_cursor")
            if not isinstance(cursor, int) or cursor < 0:
                case.fail(f"first turn did not persist prompt_event_cursor: {descriptor}")
            if descriptor.get("last_turn_status") != "completed":
                case.fail(f"first turn last_turn_status={descriptor.get('last_turn_status')!r}")
            if not descriptor.get("provider_session_id"):
                case.fail("first turn produced no provider_session_id; cannot prove resume")
            proof["cursor"] = cursor
            proof["provider_session_id"] = descriptor["provider_session_id"]
            jsonl_path = Path(live.jsonl_path)
            before_lines = jsonl_path.read_text(encoding="utf-8").splitlines()
            if not before_lines:
                case.fail("first turn left an empty transcript")
            crash_record = SessionIndex(index_path).load()[session_id]
        finally:
            await first.stop_session(session_id)

        # Daemon-reload equivalent: restore the pre-stop live record so B
        # treats the session as interrupted-by-restart, not cleanly stopped.
        SessionIndex(index_path).upsert(crash_record)
        second = SessionManager(index_path=index_path, default_workdir=workdir)
        restored = second.get_session(session_id, detail="full")
        case.assertEqual(restored.status, "interrupted")
        restored_cursor = _descriptor(restored, agent_id).get("prompt_event_cursor")
        case.assertEqual(restored_cursor, cursor)
        await second.resume_session(session_id)
        proof["resumed"] = True
        try:
            parked = await second.wait_result(session_id, timeout_ms=60_000)
            if parked.status != "awaiting_input":
                case.fail(
                    f"resume did not re-enter the input loop: "
                    f"status={parked.status} failure={parked.failure}"
                )
            await second.post_message(session_id, "What is the project id? Reply with only the id.")
            second_result = await second.wait_result(session_id, timeout_ms=240_000)
            case.assertTrue(second_result.settled)
            if second_result.status != "awaiting_input":
                later = second.get_session(session_id, detail="full")
                exceptions = [
                    (event.get("raw") or {}).get("exception")
                    for event in second.read_events(session_id, 0).events
                    if event.get("type") == "error"
                ]
                case.fail(
                    f"delta after resume failed: status={second_result.status} "
                    f"failure={second_result.failure} "
                    f"last_turn_status={_descriptor(later, agent_id).get('last_turn_status')} "
                    f"error_exceptions={exceptions}"
                )
            case.assertEqual(len(second_result.answers), 1)
            case.assertIn(codeword, second_result.answers[0]["text"].upper())
            after_lines = jsonl_path.read_text(encoding="utf-8").splitlines()
            case.assertGreater(len(after_lines), len(before_lines))
            case.assertEqual(after_lines[0], before_lines[0])
            case.assertFalse(
                any(
                    '"task"' in line and codeword in line
                    for line in after_lines[len(before_lines) :]
                ),
                "resume re-emitted the original task",
            )
            later = second.get_session(session_id, detail="full")
            later_cursor = _descriptor(later, agent_id).get("prompt_event_cursor")
            if not isinstance(later_cursor, int) or later_cursor < cursor:
                case.fail(
                    f"resumed prompt cursor {later_cursor!r} did not start at persisted {cursor}"
                )
            proof["later_cursor"] = later_cursor
        finally:
            await second.stop_session(session_id)
    return proof


def run_isolated_reload_public_resume(
    case,
    *,
    sandbox: str,
    backend_name: str,
    members: Mapping[str, str],
    extra_env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Isolated-home wrapper for CLI and SDK live reload proofs."""

    codeword = f"SABLE-{secrets.token_hex(4).upper()}"

    async def scenario(workdir: Path) -> dict[str, Any]:
        return await run_reload_public_resume(
            case,
            workdir,
            sandbox=sandbox,
            members=members,
            backend_options={backend_name: case.requested_options()},
            agent_id=backend_name,
            codeword=codeword,
        )

    with (
        tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
        tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
    ):
        workdir = Path(tmp).resolve()
        home_path = Path(home)
        (home_path / "config.toml").write_text(
            f"schema_version = 12\n\n[backends.{backend_name}]\nenabled = true\n",
            encoding="utf-8",
        )
        overrides = {"AGENT_COLLAB_HOME": str(home_path)}
        if extra_env:
            overrides.update(extra_env)
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            proof = asyncio.run(scenario(workdir))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    case.assertTrue(proof["resumed"], "public resume was never invoked")
    return proof
