#!/usr/bin/env python3
"""Internal lifecycle control for the tracked MCP Tasks extension probe."""

import argparse
import http.client
import json
import time


PROTOCOL_VERSION = "2026-07-28"
TASK_EXTENSION = "io.modelcontextprotocol/tasks"
CLIENT_INFO = {
    "name": "mcp-task-extension-probe-positive-control",
    "version": "1",
}


def request_meta():
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {
            "extensions": {TASK_EXTENSION: {}},
        },
    }


def request(request_id, method, **params):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": request_meta()},
    }


def post(port, payload):
    method = payload["method"]
    body = json.dumps(payload, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = payload["params"]["name"]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", "/mcp", body=body, headers=headers)
    response = connection.getresponse()
    response_body = response.read()
    connection.close()
    if response.status != 200:
        raise AssertionError(f"HTTP {response.status}: {response_body.decode(errors='replace')}")
    decoded = json.loads(response_body)
    if "error" in decoded:
        raise AssertionError(f"JSON-RPC error: {decoded['error']!r}")
    return decoded["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=48626)
    args = parser.parse_args()

    discovered = post(args.port, request(1, "server/discover"))
    assert discovered["resultType"] == "complete"
    assert PROTOCOL_VERSION in discovered["supportedVersions"]
    assert TASK_EXTENSION in discovered["capabilities"]["extensions"]
    assert discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == ("mcp-task-probe")

    listed = post(args.port, request(2, "tools/list"))
    assert listed["resultType"] == "complete"
    assert listed["tools"][0]["name"] == "delayed_echo"
    assert "execution" not in listed["tools"][0]

    created = post(
        args.port,
        request(
            3,
            "tools/call",
            name="delayed_echo",
            arguments={
                "seconds": 0.1,
                "message": "extension positive control",
            },
        ),
    )
    assert created["resultType"] == "task"
    task_id = created["taskId"]

    deadline = time.monotonic() + 5
    while True:
        task = post(args.port, request(4, "tasks/get", taskId=task_id))
        assert task["resultType"] == "complete"
        if task["status"] == "completed":
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"task did not complete: {task!r}")
        time.sleep(task.get("pollIntervalMs", 50) / 1000)

    result = task["result"]
    assert result["resultType"] == "complete"
    assert result["content"][0]["text"] == (
        "delayed_echo completed after 0.1s: extension positive control"
    )
    print(f"PASS: MCP Tasks extension lifecycle taskId={task_id}")


if __name__ == "__main__":
    main()
