#!/usr/bin/env python3
"""Tracked manual Streamable HTTP server for testing MCP Tasks clients.

Two deliberately separate protocol modes are available:

* legacy: the experimental Tasks flow in MCP 2025-11-25.  The client must add
  ``params.task`` to a tool whose ``execution.taskSupport`` is ``required``,
  then use ``tasks/get`` and ``tasks/result``.
* extension: the newer ``io.modelcontextprotocol/tasks`` extension.  The client
  must declare the extension in per-request capabilities, accept a
  ``resultType: task`` response, and poll ``tasks/get``.

This is diagnostic scratch code, not an agent-collab server implementation.
The request log is the result of the probe.
"""

import argparse
import datetime as dt
import json
import pathlib
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LEGACY_VERSION = "2025-11-25"
MODERN_VERSIONS = ("2026-07-28",)
TASK_EXTENSION = "io.modelcontextprotocol/tasks"
TASK_TTL_MS = 600_000
POLL_INTERVAL_MS = 500
SERVER_INFO = {"name": "mcp-task-probe", "version": "0.1.0"}

T0 = time.monotonic()
LOG_LOCK = threading.Lock()
TASK_LOCK = threading.Lock()
TASKS = {}
LOG_FILE = None
REQUEST_SEQUENCE = 0
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
}


def log(message):
    wall_time = now()
    thread_name = threading.current_thread().name
    line = f"{wall_time} +{time.monotonic() - T0:09.3f}s [{thread_name}] {message}\n"
    with LOG_LOCK:
        sys.stderr.write(line)
        sys.stderr.flush()
        if LOG_FILE is not None:
            LOG_FILE.write(line)
            LOG_FILE.flush()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def task_view(task, mode, include_result=False):
    base = {
        "taskId": task["taskId"],
        "status": task["status"],
        "statusMessage": task["statusMessage"],
        "createdAt": task["createdAt"],
        "lastUpdatedAt": task["lastUpdatedAt"],
    }
    if mode == "legacy":
        base["ttl"] = TASK_TTL_MS
        base["pollInterval"] = POLL_INTERVAL_MS
    else:
        base["resultType"] = "complete"
        base["ttlMs"] = TASK_TTL_MS
        base["pollIntervalMs"] = POLL_INTERVAL_MS
        if include_result and task["status"] == "completed":
            base["result"] = call_tool_result(task, mode)
    return base


def call_tool_result(task, mode):
    result = {
        "content": [{"type": "text", "text": task["result"]}],
        "isError": False,
        "_meta": {
            "io.modelcontextprotocol/related-task": {
                "taskId": task["taskId"],
            }
        },
    }
    if mode == "extension":
        result["resultType"] = "complete"
    return result


def complete_later(task_id, seconds):
    time.sleep(seconds)
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if task is None or task["status"] != "working":
            return
        task["status"] = "completed"
        task["statusMessage"] = "The delayed echo completed."
        task["lastUpdatedAt"] = now()
    log(f"TASK {task_id} completed")


def create_task(message, seconds):
    task_id = str(uuid.uuid4())
    created = now()
    task = {
        "taskId": task_id,
        "status": "working",
        "statusMessage": "The delayed echo is running.",
        "createdAt": created,
        "lastUpdatedAt": created,
        "result": f"delayed_echo completed after {seconds:g}s: {message}",
    }
    with TASK_LOCK:
        TASKS[task_id] = task
    threading.Thread(
        target=complete_later,
        args=(task_id, seconds),
        daemon=True,
        name=f"task-{task_id}",
    ).start()
    return task


def find_task(task_id):
    with TASK_LOCK:
        return TASKS.get(task_id)


def next_request_number():
    global REQUEST_SEQUENCE
    with LOG_LOCK:
        REQUEST_SEQUENCE += 1
        return REQUEST_SEQUENCE


class TaskProbeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, mode):
        super().__init__(address, handler)
        self.mode = mode


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    @property
    def mode(self):
        return self.server.mode

    def setup(self):
        super().setup()
        log(f"TCP OPEN remote={self.client_address!r} local={self.connection.getsockname()!r}")

    def finish(self):
        try:
            super().finish()
        finally:
            log(f"TCP CLOSE remote={self.client_address!r}")

    def handle(self):
        try:
            super().handle()
        except Exception as exc:
            log(
                f"CONNECTION EXCEPTION remote={self.client_address!r} "
                f"type={type(exc).__name__} message={exc!r}"
            )
            for line in traceback.format_exc().splitlines():
                log(f"    traceback={line}")
            raise

    def _log_headers(self, prefix):
        for name, value in self.headers.items():
            if name.lower() in SENSITIVE_HEADERS:
                rendered = f"<redacted; {len(value)} chars>"
            else:
                rendered = value
            log(f"    {prefix}-header {name}: {rendered}")

    def _json(self, http_status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        log(f"RESPONSE rpc-id={payload.get('id')!r} http={http_status} bytes={len(body)}")
        log("    response-header Content-Type: application/json")
        log(f"    response-header Content-Length: {len(body)}")
        log(f"    response-body={body.decode('utf-8')}")
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        log(f"RESPONSE FLUSHED rpc-id={payload.get('id')!r}")

    def _empty(self, http_status):
        log(f"RESPONSE http={http_status} bytes=0")
        log("    response-header Content-Length: 0")
        self.send_response(http_status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.flush()
        log("RESPONSE FLUSHED empty")

    def _result(self, request_id, result):
        if self.mode == "extension":
            result = dict(result)
            result.setdefault("resultType", "complete")
            meta = dict(result.get("_meta") or {})
            meta.setdefault("io.modelcontextprotocol/serverInfo", SERVER_INFO)
            result["_meta"] = meta
        self._json(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            },
        )

    def _error(self, request_id, code, message, data=None, http_status=200):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._json(
            http_status,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": error,
            },
        )

    def do_GET(self):
        request_number = next_request_number()
        log(
            f"REQUEST #{request_number} GET path={self.path!r} "
            f"request-version={self.request_version!r} remote={self.client_address!r}"
        )
        self._log_headers("request")
        self._empty(405)

    def do_DELETE(self):
        request_number = next_request_number()
        log(
            f"REQUEST #{request_number} DELETE path={self.path!r} "
            f"request-version={self.request_version!r} remote={self.client_address!r}"
        )
        self._log_headers("request")
        self._empty(200)

    def do_POST(self):
        request_number = next_request_number()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        log(
            f"REQUEST #{request_number} POST path={self.path!r} "
            f"request-version={self.request_version!r} remote={self.client_address!r} "
            f"declared-bytes={length} received-bytes={len(raw)}"
        )
        self._log_headers("request")
        log(f"    request-body-bytes={raw!r}")
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            log("    request-json=<invalid>")
            self._empty(400)
            return

        method = request.get("method")
        request_id = request.get("id")
        log(
            f"REQUEST #{request_number} DECODED method={method!r} id={request_id!r} "
            f"protocol={self.headers.get('MCP-Protocol-Version')!r} "
            f"mcp-method={self.headers.get('Mcp-Method')!r}"
        )
        log(f"    request-json={json.dumps(request, separators=(',', ':'))}")

        if method == "initialize":
            self._initialize(request_id, request.get("params") or {})
        elif method == "server/discover":
            self._discover(request_id)
        elif method in (
            "notifications/initialized",
            "notifications/cancelled",
        ):
            self._empty(202)
        elif method == "ping":
            self._result(request_id, {})
        elif method == "tools/list":
            self._tools_list(request_id)
        elif method == "tools/call":
            self._tools_call(request_id, request.get("params") or {})
        elif method == "tasks/get":
            self._tasks_get(request_id, request.get("params") or {})
        elif method == "tasks/result":
            self._tasks_result(request_id, request.get("params") or {})
        elif method == "tasks/list":
            self._tasks_list(request_id)
        elif method == "tasks/cancel":
            self._tasks_cancel(request_id, request.get("params") or {})
        elif method == "tasks/update":
            self._result(request_id, {"resultType": "complete"})
        else:
            self._error(
                request_id,
                -32601,
                f"Method not found: {method}",
                http_status=404 if self.mode == "extension" else 200,
            )

    def _initialize(self, request_id, params):
        if self.mode != "legacy":
            self._error(
                request_id,
                -32601,
                "initialize is unavailable in extension mode; use server/discover",
                http_status=404,
            )
            return
        requested_version = params.get("protocolVersion")
        if requested_version != LEGACY_VERSION:
            log(
                "    LEGACY PROTOCOL UNSUPPORTED: "
                f"client requested {requested_version!r}; "
                f"Tasks require {LEGACY_VERSION!r}"
            )
            self._error(
                request_id,
                -32602,
                "Unsupported protocol version",
                {
                    "supported": [LEGACY_VERSION],
                    "requested": requested_version,
                },
            )
            return
        self._result(
            request_id,
            {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {
                    "tools": {},
                    "tasks": {
                        "list": {},
                        "cancel": {},
                        "requests": {"tools": {"call": {}}},
                    },
                },
                "serverInfo": SERVER_INFO,
                "instructions": ("Call delayed_echo directly. It requires MCP task execution."),
            },
        )

    def _discover(self, request_id):
        if self.mode != "extension":
            self._error(
                request_id,
                -32601,
                "server/discover is unavailable in legacy mode",
                http_status=404,
            )
            return
        self._result(
            request_id,
            {
                "resultType": "complete",
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": {
                    "tools": {},
                    "extensions": {TASK_EXTENSION: {}},
                },
                "instructions": (
                    "Call delayed_echo directly. It returns an MCP Tasks extension task."
                ),
            },
        )

    def _tools_list(self, request_id):
        tool = {
            "name": "delayed_echo",
            "description": (
                "Wait briefly and echo a message. This probe exists only to "
                "test protocol-level asynchronous MCP Tasks support."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                    },
                    "message": {"type": "string"},
                },
                "required": ["seconds", "message"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
        if self.mode == "legacy":
            tool["execution"] = {"taskSupport": "required"}
        self._result(request_id, {"tools": [tool]})

    def _tools_call(self, request_id, params):
        if params.get("name") != "delayed_echo":
            self._error(request_id, -32602, "Unknown tool")
            return
        arguments = params.get("arguments") or {}
        try:
            seconds = float(arguments.get("seconds", 1))
        except (TypeError, ValueError):
            self._error(request_id, -32602, "seconds must be a number")
            return
        if seconds < 0 or seconds > 30:
            self._error(request_id, -32602, "seconds must be between 0 and 30")
            return
        message = str(arguments.get("message", "hello"))

        if self.mode == "legacy":
            if "task" not in params:
                log("    LEGACY TASK UNSUPPORTED: tools/call omitted params.task")
                self._error(
                    request_id,
                    -32601,
                    "delayed_echo requires task-augmented execution",
                )
                return
            task = create_task(message, seconds)
            log(f"    LEGACY TASK CREATED {task['taskId']}")
            result = {
                "task": task_view(task, "legacy"),
                "_meta": {
                    "io.modelcontextprotocol/model-immediate-response": (
                        f"Background task {task['taskId']} started."
                    )
                },
            }
            self._result(request_id, result)
            return

        meta = params.get("_meta") or {}
        capabilities = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        extensions = capabilities.get("extensions") or {}
        if TASK_EXTENSION not in extensions:
            log("    EXTENSION UNSUPPORTED: client capability is absent")
            self._error(
                request_id,
                -32021,
                "Missing required client capability",
                {
                    "requiredCapabilities": {
                        "extensions": {TASK_EXTENSION: {}},
                    }
                },
            )
            return
        task = create_task(message, seconds)
        log(f"    EXTENSION TASK CREATED {task['taskId']}")
        result = task_view(task, "extension")
        result["resultType"] = "task"
        self._result(request_id, result)

    def _tasks_get(self, request_id, params):
        task = find_task(params.get("taskId"))
        if task is None:
            self._error(request_id, -32602, "Task not found")
            return
        log(f"    TASK GET {task['taskId']} status={task['status']} mode={self.mode}")
        include_result = self.mode == "extension"
        self._result(request_id, task_view(task, self.mode, include_result))

    def _tasks_result(self, request_id, params):
        if self.mode != "legacy":
            self._error(
                request_id,
                -32601,
                "tasks/result does not exist in the Tasks extension",
            )
            return
        task = find_task(params.get("taskId"))
        if task is None:
            self._error(request_id, -32602, "Task not found")
            return
        log(f"    TASK RESULT {task['taskId']} entered status={task['status']}")
        while task["status"] == "working":
            time.sleep(0.05)
        log(f"    TASK RESULT {task['taskId']} returning status={task['status']}")
        self._result(request_id, call_tool_result(task, self.mode))

    def _tasks_list(self, request_id):
        if self.mode != "legacy":
            self._error(request_id, -32601, "tasks/list is unavailable")
            return
        with TASK_LOCK:
            tasks = [task_view(task, "legacy") for task in TASKS.values()]
        self._result(request_id, {"tasks": tasks})

    def _tasks_cancel(self, request_id, params):
        task = find_task(params.get("taskId"))
        if task is None:
            self._error(request_id, -32602, "Task not found")
            return
        with TASK_LOCK:
            if task["status"] == "working":
                task["status"] = "cancelled"
                task["statusMessage"] = "The task was cancelled."
                task["lastUpdatedAt"] = now()
        if self.mode == "legacy":
            self._result(request_id, task_view(task, "legacy"))
        else:
            self._result(request_id, {"resultType": "complete"})


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("legacy", "extension"),
        help="Tasks protocol dialect to expose",
    )
    parser.add_argument(
        "port",
        nargs="?",
        type=port_number,
        default=48623,
        help="localhost port (default: 48623)",
    )
    parser.add_argument(
        "--log-file",
        type=pathlib.Path,
        help=(
            "append the complete wire log to this file in addition to stderr "
            "(default: probe-<mode>-<port>.log beside this script)"
        ),
    )
    parser.add_argument(
        "--truncate-log",
        action="store_true",
        help="truncate the log file instead of appending a new run",
    )
    return parser.parse_args()


def main():
    global LOG_FILE
    args = parse_args()
    log_path = args.log_file
    if log_path is None:
        log_path = pathlib.Path(__file__).resolve().with_name(f"probe-{args.mode}-{args.port}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE = log_path.open(
        "w" if args.truncate_log else "a",
        encoding="utf-8",
        buffering=1,
    )
    server = TaskProbeServer(("127.0.0.1", args.port), Handler, args.mode)
    log("=" * 80)
    log(
        f"MCP Tasks probe mode={args.mode} "
        f"url=http://127.0.0.1:{args.port}/mcp "
        f"log={str(log_path)!r} pid-thread={threading.current_thread().name!r}"
    )
    log(
        "LOGGING full JSON bodies and non-sensitive headers; "
        "authorization and cookie values are redacted"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("probe stopped")
    finally:
        server.server_close()
        LOG_FILE.close()
        LOG_FILE = None


if __name__ == "__main__":
    main()
