import asyncio
from dataclasses import replace
import os
from pathlib import Path
import secrets
import tempfile

from agent_collab.config import builtin_config
from agent_collab.daemon import SessionManager, StartSessionRequest
from agent_collab.options import StartOptionsError
from integration_tests.harness import LiveBackendTestCase, REPO_ROOT


class CodexSdkLiveTests(LiveBackendTestCase):
    provider = "codex"
    backend_id = "sdk"

    def live_agent(self):
        return replace(
            builtin_config().agents["codex_cli"],
            id="codex_sdk",
            backend="sdk",
            command=None,
            enabled=True,
        )

    def test_provider_memory_across_interactive_turns(self):
        codeword = f"SABLE-{secrets.token_hex(4).upper()}"

        async def scenario(workdir):
            manager = SessionManager()
            state = await manager.start_session(
                StartSessionRequest(
                    task=(
                        f"Memorize the codeword {codeword}. Reply exactly STORED "
                        "without repeating the codeword."
                    ),
                    workflow="solo",
                    members={"claude_cli": "codex_sdk"},
                    backend_options={"codex_sdk": self.requested_options()},
                    max_turns=1,
                    timeout=180,
                    workdir=workdir,
                    interactive=True,
                    interactive_idle_timeout=300,
                )
            )
            try:
                first = await manager.wait_result(state.session_id, timeout_ms=240_000)
                self.assertTrue(first.settled)
                if first.status != "awaiting_input":
                    events = manager.read_events(
                        state.session_id,
                        0,
                        tool_output="full",
                    ).events
                    errors = [event["text"] for event in events if event.get("type") == "error"]
                    self.fail(f"first turn failed: {first.failure}; errors={errors}")

                await manager.post_message(
                    state.session_id,
                    "Reply with only the codeword you were asked to remember.",
                )
                second = await manager.wait_result(state.session_id, timeout_ms=240_000)
                self.assertTrue(second.settled)
                self.assertEqual(second.status, "awaiting_input")
                self.assertEqual(len(second.answers), 1)
                self.assertIn(codeword, second.answers[0]["text"].upper())

                events = manager.read_events(state.session_id, 0, tool_output="full").events
                thread_ids = [
                    event["raw"]["provider_session_id"]
                    for event in events
                    if isinstance(event.get("raw"), dict)
                    and event["raw"].get("provider_session_kind") == "thread"
                ]
                self.assertGreaterEqual(len(thread_ids), 2)
                self.assertEqual(len(set(thread_ids)), 1)
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["codex_sdk"]["provider_session_id"],
                    thread_ids[0],
                )
            finally:
                await manager.stop_session(state.session_id)

        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            home_path = Path(home)
            (home_path / "config.toml").write_text(
                (
                    "schema_version = 10\n\n"
                    "[backends.codex_sdk]\n"
                    "enabled = true\n"
                    'command = "codex"\n'
                ),
                encoding="utf-8",
            )
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            try:
                asyncio.run(scenario(Path(tmp).resolve()))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_COLLAB_HOME", None)
                else:
                    os.environ["AGENT_COLLAB_HOME"] = previous

    def test_tool_gate_park_deny_worker(self):
        self._run_tool_gate_park(sandbox="read-only", decision="deny")

    def test_tool_gate_park_approve_worker(self):
        self._run_tool_gate_park(sandbox="read-only", decision="approve")

    def test_tool_gate_park_clock_exclusion_worker(self):
        self._run_tool_gate_park(
            sandbox="read-only",
            decision="deny",
            clock_exclusion=True,
        )

    def test_tool_gate_park_deny_in_process(self):
        self._run_tool_gate_park(sandbox="none", decision="deny")

    def test_tool_gate_park_approve_in_process(self):
        self._run_tool_gate_park(sandbox="none", decision="approve")

    def test_tool_gate_park_clock_exclusion_in_process(self):
        self._run_tool_gate_park(
            sandbox="none",
            decision="deny",
            clock_exclusion=True,
        )

    def _run_tool_gate_park(self, *, sandbox, decision, clock_exclusion=False):
        token = f"PARK-{secrets.token_hex(4).upper()}"
        timeout = 20 if clock_exclusion else 180
        park_hold_s = 25.0 if clock_exclusion else 0.0

        async def scenario(workdir):
            marker = workdir / "PARK.txt"
            manager = SessionManager()
            try:
                state = await manager.start_session(
                    StartSessionRequest(
                        task=self._park_prompt(token),
                        workflow="solo",
                        members={"claude_cli": "codex_sdk"},
                        backend_options={"codex_sdk": self.requested_options()},
                        max_turns=1,
                        timeout=timeout,
                        workdir=workdir,
                        sandbox=sandbox,
                    )
                )
            except StartOptionsError as exc:
                codes = [detail.get("code") for detail in exc.details]
                self.fail(
                    f"worker start failed structurally sandbox={sandbox} codes={codes} error={exc}"
                )
            try:
                parked = await self._wait_parked(manager, state.session_id)
                pending = parked.pending_approvals[0]
                request_id = pending["request_id"]
                tool_name = pending.get("tool_name")
                self.assertTrue(request_id, "parked approval missing request_id")
                events_at_park = self._session_events(manager, state.session_id)
                self._assert_no_tool_result_before_resolved(events_at_park)
                if clock_exclusion:
                    await asyncio.sleep(park_hold_s)
                    still = await manager.wait_result(state.session_id, timeout_ms=0)
                    if still.status != "awaiting_approval" or not still.pending_approvals:
                        self.fail(
                            "session left park during clock-exclusion hold; "
                            f"status={still.status} kinds={self._event_kinds(manager, state.session_id)}"
                        )
                decided = await manager.resolve_approval(state.session_id, request_id, decision)
                expected = "approved" if decision == "approve" else "denied"
                self.assertIn(
                    decided.get("outcome"),
                    {expected, "auto_denied"} if decision == "deny" else {expected},
                    f"unexpected decision outcome={decided.get('outcome')} "
                    f"request_id={request_id} tool_name={tool_name}",
                )
                extra_decision = "approve" if decision == "approve" else "deny"
                final = await self._wait_settled_after_decision(
                    manager,
                    state.session_id,
                    extra_decision=extra_decision,
                )
                self._assert_not_timed_out(final)
                events = self._session_events(manager, state.session_id)
                self._assert_approval_event_order(events, request_id, decided.get("outcome"))
                if decision == "deny":
                    self._assert_deny_did_not_execute(events, request_id)
                    if sandbox == "none":
                        self.assertFalse(
                            marker.exists(),
                            "in-process deny wrote PARK.txt",
                        )
                else:
                    self._assert_approve_resolved(events, request_id)
                    if sandbox == "none":
                        self._assert_in_process_approve_executed(marker, events)
                if sandbox == "read-only":
                    self.assertIn(final.status, {"done", "awaiting_input"})
            finally:
                await manager.stop_session(state.session_id)

        self._run_isolated_session(scenario, isolate_codex_home=(sandbox == "read-only"))

    def _park_prompt(self, token):
        return (
            f"Create a file named PARK.txt in the current working directory. "
            f"The file must contain exactly this token and nothing else: {token}. "
            "You must use a shell command or write tool to create PARK.txt "
            "(for example, write the token with a command). "
            "Do not only reply in chat. Do not use a read-only tool. "
            "Call a command-execution or file-change tool now."
        )

    def _run_isolated_session(self, scenario, *, isolate_codex_home=False):
        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            workdir = Path(tmp).resolve()
            self.assertNotEqual(workdir, REPO_ROOT)
            self.assertNotIn(REPO_ROOT, workdir.parents)
            home_path = Path(home)
            (home_path / "config.toml").write_text(
                ("schema_version = 12\n\n[backends.codex_sdk]\nenabled = true\n"),
                encoding="utf-8",
            )
            previous = {
                "AGENT_COLLAB_HOME": os.environ.get("AGENT_COLLAB_HOME"),
                "CODEX_HOME": os.environ.get("CODEX_HOME"),
            }
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            if isolate_codex_home:
                isolated_codex = home_path / "codex-home"
                self._stage_isolated_codex_home(isolated_codex)
                os.environ["CODEX_HOME"] = str(isolated_codex)
            try:
                asyncio.run(scenario(workdir))
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _stage_isolated_codex_home(self, dest):
        """Owner-only CODEX_HOME so worker plan resolution can mount state.

        Ambient Codex state may be group-writable, which
        ``resolve_state_root`` rejects. Copy only auth material; do not reuse
        the ambient directory.
        """

        dest.mkdir(mode=0o700)
        dest.chmod(0o700)
        source = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        auth = source / "auth.json"
        if not auth.is_file() or auth.is_symlink():
            self.fail("isolated Codex state is missing owner auth material")
        target = dest / "auth.json"
        target.write_bytes(auth.read_bytes())
        target.chmod(0o600)

    async def _wait_parked(self, manager, session_id, timeout_s=180.0):
        deadline = asyncio.get_running_loop().time() + timeout_s
        last = None
        while asyncio.get_running_loop().time() < deadline:
            remaining_ms = max(50, int((deadline - asyncio.get_running_loop().time()) * 1000))
            last = await manager.wait_result(session_id, timeout_ms=min(remaining_ms, 5_000))
            if last.status == "awaiting_approval" and last.pending_approvals:
                return last
            if last.settled and last.status != "awaiting_approval":
                kinds = self._event_kinds(manager, session_id)
                code = last.failure.get("code") if isinstance(last.failure, dict) else None
                if isinstance(code, str) and code.startswith("outer_sandbox_"):
                    self.fail(
                        "worker start failed structurally; callback never parked; "
                        f"status={last.status} code={code} kinds={kinds}"
                    )
                tool_kinds = [kind for kind in kinds if kind in {"command", "file_change"}]
                if tool_kinds and "approval_request" not in kinds:
                    self.fail(
                        "callback never parked; tools ran without approval_request "
                        f"(auto_review shadowing); status={last.status} code={code} "
                        f"kinds={kinds}"
                    )
                self.fail(f"callback never parked; status={last.status} code={code} kinds={kinds}")
        kinds = self._event_kinds(manager, session_id) if last is not None else []
        status = getattr(last, "status", None)
        self.fail(f"callback never parked; status={status} kinds={kinds}")

    async def _wait_settled_after_decision(
        self, manager, session_id, *, extra_decision, timeout_s=180.0
    ):
        deadline = asyncio.get_running_loop().time() + timeout_s
        extras = 0
        last = None
        while asyncio.get_running_loop().time() < deadline:
            remaining_ms = max(50, int((deadline - asyncio.get_running_loop().time()) * 1000))
            last = await manager.wait_result(session_id, timeout_ms=min(remaining_ms, 5_000))
            if last.status == "awaiting_approval" and last.pending_approvals:
                extras += 1
                if extras > 4:
                    self.fail(
                        "too many follow-up parks after the first decision; "
                        f"kinds={self._event_kinds(manager, session_id)}"
                    )
                await manager.resolve_approval(
                    session_id,
                    last.pending_approvals[0]["request_id"],
                    extra_decision,
                )
                continue
            if last.settled:
                return last
        kinds = self._event_kinds(manager, session_id) if last is not None else []
        self.fail(
            "session did not settle after approval decision; "
            f"status={getattr(last, 'status', None)} kinds={kinds}"
        )

    def _session_events(self, manager, session_id):
        return manager.read_events(session_id, 0, tool_output="full").events

    def _event_kinds(self, manager, session_id):
        return [event.get("type") for event in self._session_events(manager, session_id)]

    def _assert_not_timed_out(self, result):
        outcomes = result.turn_outcomes or []
        timed_out = [
            item.get("outcome")
            for item in outcomes
            if isinstance(item, dict) and item.get("outcome") == "timed_out"
        ]
        self.assertFalse(
            timed_out or result.status == "timed_out",
            f"turn timed_out status={result.status} outcomes={[item.get('outcome') for item in outcomes if isinstance(item, dict)]}",
        )

    def _assert_approval_event_order(self, events, request_id, outcome):
        kinds = [event.get("type") for event in events]
        self.assertIn("approval_request", kinds, f"missing approval_request kinds={kinds}")
        self.assertIn("approval_resolved", kinds, f"missing approval_resolved kinds={kinds}")
        request_index = next(
            index
            for index, event in enumerate(events)
            if event.get("type") == "approval_request"
            and (event.get("raw") or {}).get("request_id") == request_id
        )
        resolved_index = next(
            index
            for index, event in enumerate(events)
            if event.get("type") == "approval_resolved"
            and (event.get("raw") or {}).get("request_id") == request_id
        )
        self.assertLess(
            request_index,
            resolved_index,
            f"approval_resolved before approval_request request_id={request_id}",
        )
        resolved = events[resolved_index]
        self.assertEqual(
            (resolved.get("raw") or {}).get("outcome"),
            outcome,
            f"approval_resolved outcome mismatch request_id={request_id}",
        )
        before = events[:resolved_index]
        results = [event.get("type") for event in before if self._is_tool_result(event)]
        self.assertFalse(
            results,
            f"tool result before approval_resolved request_id={request_id} kinds={kinds}",
        )

    def _assert_no_tool_result_before_resolved(self, events):
        if any(event.get("type") == "approval_resolved" for event in events):
            resolved_index = next(
                index
                for index, event in enumerate(events)
                if event.get("type") == "approval_resolved"
            )
            events = events[:resolved_index]
        results = [event.get("type") for event in events if self._is_tool_result(event)]
        self.assertFalse(
            results,
            f"tool result before park resolved kinds={[event.get('type') for event in events]}",
        )

    def _assert_deny_did_not_execute(self, events, request_id):
        resolved = [
            event
            for event in events
            if event.get("type") == "approval_resolved"
            and (event.get("raw") or {}).get("request_id") == request_id
        ]
        self.assertTrue(resolved, f"missing approval_resolved request_id={request_id}")
        outcome = (resolved[0].get("raw") or {}).get("outcome")
        self.assertIn(
            outcome,
            {"denied", "auto_denied"},
            f"deny outcome={outcome} request_id={request_id}",
        )
        after = events[events.index(resolved[0]) + 1 :]
        executed = [event.get("type") for event in after if self._is_successful_tool_result(event)]
        self.assertFalse(
            executed,
            f"tool ran as approved after deny request_id={request_id}",
        )

    def _assert_approve_resolved(self, events, request_id):
        resolved = [
            event
            for event in events
            if event.get("type") == "approval_resolved"
            and (event.get("raw") or {}).get("request_id") == request_id
        ]
        self.assertTrue(resolved, f"missing approval_resolved request_id={request_id}")
        self.assertEqual(
            (resolved[0].get("raw") or {}).get("outcome"),
            "approved",
            f"approve outcome={(resolved[0].get('raw') or {}).get('outcome')} "
            f"request_id={request_id}",
        )

    def _assert_in_process_approve_executed(self, marker, events):
        if marker.exists():
            return
        executed = any(self._is_successful_tool_result(event) for event in events)
        self.assertTrue(
            executed,
            "in-process approve neither wrote PARK.txt nor emitted a tool result",
        )

    @staticmethod
    def _is_tool_result(event):
        return event.get("type") in {"command", "file_change"}

    @staticmethod
    def _is_successful_tool_result(event):
        if event.get("type") == "error":
            return False
        if event.get("type") not in {"command", "file_change"}:
            return False
        raw = event.get("raw")
        if not isinstance(raw, dict):
            return True
        status = raw.get("status")
        if status in {"declined", "failed", "inProgress"}:
            return False
        if event.get("type") == "command" and raw.get("exit_code") not in (None, 0):
            return False
        return True

    def test_model_catalog(self):
        observation = self.discover_live_models()
        self.assertEqual(observation.status, "ok")
        self.assertEqual(observation.source, "sdk")
        self.assertTrue(observation.complete)
        self.assertTrue(observation.models)
