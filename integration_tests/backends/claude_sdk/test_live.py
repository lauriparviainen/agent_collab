import asyncio
from dataclasses import replace
import os
from pathlib import Path
import secrets
import tempfile
from unittest import mock

from agent_collab.backends.claude_sdk.backend import ClaudeSdkRunner
from agent_collab.config import builtin_config
from agent_collab.daemon import SessionManager, SessionRequestError, StartSessionRequest
from agent_collab.options import StartOptionsError
from integration_tests.harness import LiveBackendTestCase, REPO_ROOT


class ClaudeSdkLiveTests(LiveBackendTestCase):
    provider = "claude"
    backend_id = "sdk"

    def live_agent(self):
        return replace(
            builtin_config().agents["claude_cli"],
            id="claude_sdk",
            backend="sdk",
            command=None,
            enabled=True,
        )

    def test_turn_and_session(self):
        events = self.run_live()
        self.assert_message(events)
        self.assert_session_kind(events, "session")

    def test_provider_memory_across_interactive_turns(self):
        codeword = f"SABLE-{secrets.token_hex(4).upper()}"
        prompts = []
        original_run_turn = ClaudeSdkRunner.run_turn

        async def recording_run_turn(runner, prompt, workdir, emit):
            prompts.append(prompt)
            return await original_run_turn(runner, prompt, workdir, emit)

        async def scenario(workdir):
            manager = SessionManager()
            state = await manager.start_session(
                StartSessionRequest(
                    task=(
                        f"For this session the project id is {codeword}. "
                        "Reply exactly STORED without repeating the project id."
                    ),
                    workflow="solo",
                    members={"claude_cli": "claude_sdk"},
                    backend_options={"claude_sdk": self.requested_options()},
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
                    "What is the project id? Reply with only the id.",
                )
                second = await manager.wait_result(state.session_id, timeout_ms=240_000)
                self.assertTrue(second.settled)
                self.assertEqual(second.status, "awaiting_input")
                self.assertEqual(len(second.answers), 1)
                self.assertIn(codeword, second.answers[0]["text"].upper())

                events = manager.read_events(state.session_id, 0, tool_output="full").events
                session_ids = [
                    event["raw"]["provider_session_id"]
                    for event in events
                    if isinstance(event.get("raw"), dict)
                    and event["raw"].get("provider_session_kind") == "session"
                ]
                self.assertGreaterEqual(len(session_ids), 2)
                self.assertEqual(len(set(session_ids)), 1)
                session = manager.get_session(state.session_id, detail="full")
                self.assertEqual(
                    session.agent_sessions["claude_sdk"]["provider_session_id"],
                    session_ids[0],
                )

                # The follow-up used the Stage 3 delta continuation prompt: no
                # guardrails/task/window re-send, and the codeword reached the
                # model only through provider-held context.
                self.assertEqual(len(prompts), 2)
                self.assertIn("TASK:", prompts[0])
                self.assertIn("NEW EVENTS SINCE YOUR LAST TURN:", prompts[1])
                self.assertNotIn("TASK:", prompts[1])
                self.assertNotIn("RECENT TRANSCRIPT:", prompts[1])
                self.assertNotIn(codeword, prompts[1])
            finally:
                await manager.stop_session(state.session_id)

        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            home_path = Path(home)
            (home_path / "config.toml").write_text(
                ("schema_version = 10\n\n[backends.claude_sdk]\nenabled = true\n"),
                encoding="utf-8",
            )
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            try:
                with mock.patch.object(ClaudeSdkRunner, "run_turn", recording_run_turn):
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

    def test_interrupt_long_turn_worker(self):
        self._run_interrupt_long_turn(sandbox="read-only")

    def test_interrupt_long_turn_in_process(self):
        self._run_interrupt_long_turn(sandbox="none")

    def test_interrupt_continue_worker(self):
        self._run_interrupt_continue(sandbox="read-only")

    def test_interrupt_continue_in_process(self):
        self._run_interrupt_continue(sandbox="none")

    def test_reload_public_resume_worker(self):
        self._run_reload_public_resume(sandbox="read-only")

    def test_reload_public_resume_in_process(self):
        self._run_reload_public_resume(sandbox="none")

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
                        members={"claude_cli": "claude_sdk"},
                        backend_options={"claude_sdk": self.requested_options()},
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

        self._run_isolated_session(scenario)

    def _park_prompt(self, token):
        return (
            f"Create a file named PARK.txt in the current working directory. "
            f"The file must contain exactly this token and nothing else: {token}. "
            "You must use the Write tool to create PARK.txt. "
            "Do not only reply in chat. Do not use a read-only tool. Call Write now."
        )

    def _run_reload_public_resume(self, *, sandbox):
        from integration_tests.resume_proof import run_reload_public_resume

        codeword = f"SABLE-{secrets.token_hex(4).upper()}"

        async def scenario(workdir):
            proof = await run_reload_public_resume(
                self,
                workdir,
                sandbox=sandbox,
                members={"claude_cli": "claude_sdk"},
                backend_options={"claude_sdk": self.requested_options()},
                agent_id="claude_sdk",
                codeword=codeword,
            )
            self.assertTrue(proof["resumed"], "public resume was never invoked")

        self._run_isolated_session(scenario)

    def _run_isolated_session(self, scenario):
        with (
            tempfile.TemporaryDirectory(prefix="agent-collab-it-") as tmp,
            tempfile.TemporaryDirectory(prefix="agent-collab-it-home-") as home,
        ):
            workdir = Path(tmp).resolve()
            self.assertNotEqual(workdir, REPO_ROOT)
            self.assertNotIn(REPO_ROOT, workdir.parents)
            home_path = Path(home)
            (home_path / "config.toml").write_text(
                ("schema_version = 12\n\n[backends.claude_sdk]\nenabled = true\n"),
                encoding="utf-8",
            )
            previous = os.environ.get("AGENT_COLLAB_HOME")
            os.environ["AGENT_COLLAB_HOME"] = str(home_path)
            try:
                asyncio.run(scenario(workdir))
            finally:
                if previous is None:
                    os.environ.pop("AGENT_COLLAB_HOME", None)
                else:
                    os.environ["AGENT_COLLAB_HOME"] = previous

    def _run_interrupt_long_turn(self, *, sandbox):
        async def scenario(workdir):
            manager = SessionManager()
            state = None
            try:
                state = await self._start_interrupt_session(manager, workdir, sandbox)
                result, issued = await self._interrupt_live_turn(manager, state.session_id)
                self._assert_distinguishable_interrupt(result, manager, state.session_id, issued)
            finally:
                if state is not None:
                    await manager.stop_session(state.session_id)

        self._run_isolated_session(scenario)

    def _run_interrupt_continue(self, *, sandbox):
        async def scenario(workdir):
            manager = SessionManager()
            state = None
            try:
                state = await self._start_interrupt_session(manager, workdir, sandbox)
                result, issued = await self._interrupt_live_turn(manager, state.session_id)
                self._assert_distinguishable_interrupt(result, manager, state.session_id, issued)
                self._assert_continue_rejected(result, manager, state.session_id)
                with self.assertRaises(SessionRequestError) as ctx:
                    await manager.post_message(
                        state.session_id,
                        "Reply with the single word: ready.",
                    )
                if "session is not live: failed" not in str(ctx.exception):
                    self._fail_interrupt(
                        result,
                        manager,
                        state.session_id,
                        issued=issued,
                        detail=(
                            f"continue-after-interrupt post_message rejection was {ctx.exception!r}"
                        ),
                    )
            finally:
                if state is not None:
                    await manager.stop_session(state.session_id)

        self._run_isolated_session(scenario)

    async def _start_interrupt_session(self, manager, workdir, sandbox):
        try:
            return await manager.start_session(
                StartSessionRequest(
                    task=self._interrupt_prompt(),
                    workflow="solo",
                    members={"claude_cli": "claude_sdk"},
                    backend_options={"claude_sdk": self.requested_options()},
                    max_turns=1,
                    timeout=180,
                    workdir=workdir,
                    sandbox=sandbox,
                    interactive=True,
                    interactive_idle_timeout=300,
                )
            )
        except StartOptionsError as exc:
            codes = [detail.get("code") for detail in exc.details]
            self.fail(
                f"worker start failed structurally sandbox={sandbox} codes={codes} error={exc}"
            )

    def _interrupt_prompt(self):
        return (
            "Count slowly from 1 to 40 in chat. Write each number as its own "
            "short sentence on its own line, like 'Number 1 is one.' Do not use "
            "any tools. Do not skip numbers. Do not summarize. Keep going until "
            "you reach 40."
        )

    async def _interrupt_live_turn(self, manager, session_id):
        await self._wait_provider_in_flight(manager, session_id)
        managed = manager._sessions[session_id]
        referee = managed.referee
        if referee is None:
            peek = await manager.wait_result(session_id, timeout_ms=0)
            self._fail_interrupt(
                peek,
                manager,
                session_id,
                issued=False,
                detail="no live referee",
            )
        issued = await referee.interrupt_in_flight()
        result = await self._wait_settled_after_interrupt(manager, session_id)
        return result, issued

    async def _wait_provider_in_flight(self, manager, session_id, timeout_s=90.0):
        deadline = asyncio.get_running_loop().time() + timeout_s
        last = None
        while asyncio.get_running_loop().time() < deadline:
            remaining_ms = max(50, int((deadline - asyncio.get_running_loop().time()) * 1000))
            last = await manager.wait_result(session_id, timeout_ms=min(remaining_ms, 500))
            if last.settled:
                code = self._failure_code(last)
                kinds = self._event_kinds(manager, session_id)
                if isinstance(code, str) and code.startswith("outer_sandbox_"):
                    self.fail(
                        "worker start failed structurally; never reached in-flight; "
                        f"status={last.status} code={code} kinds={kinds}"
                    )
                self._fail_interrupt(
                    last,
                    manager,
                    session_id,
                    issued=None,
                    detail="settled before interrupt",
                )
            if last.status == "running" and self._has_provider_progress(manager, session_id):
                return last
        self._fail_interrupt(
            last,
            manager,
            session_id,
            issued=None,
            detail="no provider progress before interrupt",
        )

    async def _wait_settled_after_interrupt(self, manager, session_id, timeout_s=120.0):
        deadline = asyncio.get_running_loop().time() + timeout_s
        last = None
        while asyncio.get_running_loop().time() < deadline:
            remaining_ms = max(50, int((deadline - asyncio.get_running_loop().time()) * 1000))
            last = await manager.wait_result(session_id, timeout_ms=min(remaining_ms, 5_000))
            if last.settled:
                return last
        self._fail_interrupt(
            last,
            manager,
            session_id,
            issued=True,
            detail="interrupt wait hung",
        )

    def _has_provider_progress(self, manager, session_id):
        for event in self._session_events(manager, session_id):
            raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
            # Worker emits source=claude "run started" before connect/query;
            # interrupting then is a no-op and the turn completes.
            if raw.get("phase") == "run_started":
                continue
            if raw.get("provider_session_id"):
                return True
            if event.get("source") == "claude":
                return True
            if event.get("type") in {"message", "tool"} and event.get("source") not in {
                "human",
                "referee",
            }:
                return True
        return False

    def _assert_distinguishable_interrupt(self, result, manager, session_id, issued):
        if not issued:
            self._fail_interrupt(
                result,
                manager,
                session_id,
                issued=issued,
                detail="interrupt_in_flight issued nothing",
            )
        if not result.settled:
            self._fail_interrupt(
                result,
                manager,
                session_id,
                issued=issued,
                detail="interrupt wait hung",
            )
        if self._has_local_interrupt(result):
            return
        self._fail_interrupt(
            result,
            manager,
            session_id,
            issued=issued,
            detail="interrupt lacked abort marker",
        )

    def _assert_continue_rejected(self, result, manager, session_id):
        session = manager.get_session(session_id)
        result_code = self._failure_code(result)
        session_code = self._failure_code(session)
        if (
            result.status == "failed"
            and result_code == "local_turn_interrupted"
            and session.status == "failed"
            and session_code == "local_turn_interrupted"
        ):
            return
        self._fail_interrupt(
            result,
            manager,
            session_id,
            issued=True,
            detail=(
                "continue-after-interrupt did not fail the session; "
                f"wait_result status={result.status} code={result_code} "
                f"get_session status={session.status} code={session_code}"
            ),
        )

    def _has_local_interrupt(self, result):
        return any(
            outcome == "interrupted" and code == "local_turn_interrupted"
            for outcome, code in self._turn_outcome_pairs(result)
        )

    def _turn_outcome_pairs(self, result):
        pairs = []
        for item in result.turn_outcomes or []:
            if isinstance(item, dict):
                pairs.append((item.get("outcome"), item.get("code")))
        return pairs

    def _failure_code(self, result):
        if result is None:
            return None
        failure = result.failure
        if isinstance(failure, dict):
            return failure.get("code")
        return None

    def _fail_interrupt(self, result, manager, session_id, *, issued, detail):
        status = getattr(result, "status", None)
        code = self._failure_code(result)
        pairs = self._turn_outcome_pairs(result) if result is not None else []
        kinds = self._event_kinds(manager, session_id)
        self.fail(
            f"{detail}; status={status} code={code} issued={issued} outcomes={pairs} kinds={kinds}"
        )

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
        raw = event.get("raw")
        return isinstance(raw, dict) and bool(raw.get("tool_use_id"))

    @staticmethod
    def _is_successful_tool_result(event):
        if event.get("type") == "error":
            return False
        raw = event.get("raw")
        if not isinstance(raw, dict) or not raw.get("tool_use_id"):
            return False
        return raw.get("is_error") is not True
