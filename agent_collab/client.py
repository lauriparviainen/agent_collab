from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
SERVER_URL_ENV = "AGENT_COLLAB_SERVER"


class ClientError(RuntimeError):
    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.payload = payload


def default_server_url() -> str:
    return os.environ.get(SERVER_URL_ENV, DEFAULT_SERVER_URL)


class AgentCollabClient:
    def __init__(self, server_url: Optional[str] = None, timeout: float = 60.0):
        self.server_url = (server_url or default_server_url()).rstrip("/")
        self.timeout = timeout

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    def start_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/sessions", payload)

    def describe_options(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", "/options", payload or {})

    def list_sessions(self) -> Dict[str, Any]:
        return self._request("GET", "/sessions")

    def get_session(self, session_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id}")

    def read_events(self, session_id: str, cursor: int = 0) -> Dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id}/events", {"cursor": cursor})

    def wait_events(self, session_id: str, cursor: int = 0, timeout_ms: int = 30000) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/sessions/{session_id}/events/wait",
            {"cursor": cursor, "timeout_ms": timeout_ms},
            timeout=max(self.timeout, (timeout_ms / 1000.0) + 5),
        )

    def post_message(
        self,
        session_id: str,
        text: str,
        source: str = "referee",
        target: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"source": source, "text": text}
        if target is not None:
            payload["target"] = target
        return self._request("POST", f"/sessions/{session_id}/messages", payload)

    def read_transcript(self, session_id: str) -> str:
        result = self._request("GET", f"/sessions/{session_id}/transcript")
        return str(result.get("transcript", ""))

    def stop_session(self, session_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/stop")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = self.server_url + path
        data = None
        headers = {"Accept": "application/json"}
        if method == "GET" and payload:
            url += "?" + urlencode(payload)
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise ClientError(body or str(exc)) from exc
            if isinstance(payload, dict):
                raise ClientError(_format_error_payload(payload), payload=payload) from exc
            raise ClientError(str(payload)) from exc
        except URLError as exc:
            raise ClientError(f"could not reach agent-collab daemon at {self.server_url}: {exc.reason}") from exc

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClientError(f"invalid JSON response from daemon: {body[:200]}") from exc


def _format_error_payload(payload: Dict[str, Any]) -> str:
    error = payload.get("error", payload)
    if error == "invalid_start_options" and isinstance(payload.get("details"), list):
        lines = [str(error)]
        for detail in payload["details"]:
            if isinstance(detail, dict):
                path = str(detail.get("path", ""))
                message = str(detail.get("message", ""))
                lines.append(f"{path}: {message}" if path else message)
        return "\n".join(lines)
    return str(error)
