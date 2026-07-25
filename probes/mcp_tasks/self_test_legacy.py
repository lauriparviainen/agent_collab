#!/usr/bin/env python3
"""Positive control for the tracked probe's MCP 2025-11-25 Tasks flow."""

import argparse
import http.client
import json
import time


PROTOCOL_VERSION = "2025-11-25"


def post(port, payload, initialized=False):
    body = json.dumps(payload, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if initialized:
        headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", "/mcp", body=body, headers=headers)
    response = connection.getresponse()
    response_body = response.read()
    connection.close()
    if response.status not in (200, 202):
        raise AssertionError(f"HTTP {response.status}: {response_body.decode(errors='replace')}")
    if not response_body:
        return None
    decoded = json.loads(response_body)
    if "error" in decoded:
        raise AssertionError(f"JSON-RPC error: {decoded['error']!r}")
    return decoded["result"]


def request(request_id, method, params=None):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=48625)
    args = parser.parse_args()

    initialized = post(
        args.port,
        request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp-task-probe-positive-control",
                    "version": "1",
                },
            },
        ),
    )
    assert initialized["protocolVersion"] == PROTOCOL_VERSION
    assert "call" in initialized["capabilities"]["tasks"]["requests"]["tools"]

    post(
        args.port,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        initialized=True,
    )
    listed = post(
        args.port,
        request(2, "tools/list", {}),
        initialized=True,
    )
    assert listed["tools"][0]["execution"]["taskSupport"] == "required"

    created = post(
        args.port,
        request(
            3,
            "tools/call",
            {
                "name": "delayed_echo",
                "arguments": {
                    "seconds": 0.1,
                    "message": "positive control",
                },
                "task": {"ttl": 60_000},
            },
        ),
        initialized=True,
    )
    task_id = created["task"]["taskId"]

    deadline = time.monotonic() + 5
    while True:
        task = post(
            args.port,
            request(4, "tasks/get", {"taskId": task_id}),
            initialized=True,
        )
        if task["status"] == "completed":
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"task did not complete: {task!r}")
        time.sleep(task.get("pollInterval", 50) / 1000)

    result = post(
        args.port,
        request(5, "tasks/result", {"taskId": task_id}),
        initialized=True,
    )
    assert result["content"][0]["text"] == ("delayed_echo completed after 0.1s: positive control")
    assert result["_meta"]["io.modelcontextprotocol/related-task"]["taskId"] == (task_id)
    print(f"PASS: MCP {PROTOCOL_VERSION} Tasks lifecycle taskId={task_id}")


if __name__ == "__main__":
    main()
