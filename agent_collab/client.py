from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .api_schema import (
    API_VERSION,
    API_VERSION_HEADER,
    EventBatchModel,
    HealthModel,
    PruneResultModel,
    SessionListModel,
    SessionResultModel,
    SessionStateModel,
    TranscriptModel,
)
from .net import is_loopback_url


DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
SERVER_URL_ENV = "AGENT_COLLAB_SERVER"
TOKEN_ENV = "AGENT_COLLAB_TOKEN"


class ClientError(RuntimeError):
    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.payload = payload


def default_server_url() -> str:
    return os.environ.get(SERVER_URL_ENV, DEFAULT_SERVER_URL)


class AgentCollabClient:
    """Typed HTTP client: every response parses into its ``api_schema`` DTO.

    ``describe_options`` is the deliberate exception — the options payload is
    the ``/options`` runtime authority's dynamic shape, so it stays a raw dict
    (see stage-5.3 "What does NOT go into a static schema").
    """

    def __init__(
        self,
        server_url: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.server_url = (server_url or default_server_url()).rstrip("/")
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_loaded = False

    def health(self) -> HealthModel:
        return HealthModel.from_dict(self._request("GET", "/health"))

    def start_session(self, payload: Dict[str, Any]) -> SessionStateModel:
        return SessionStateModel.from_dict(self._request("POST", "/sessions", payload))

    def describe_options(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", "/options", payload or {})

    def list_sessions(self) -> SessionListModel:
        return SessionListModel.from_dict(self._request("GET", "/sessions"))

    def get_session(self, session_id: str) -> SessionStateModel:
        return SessionStateModel.from_dict(self._request("GET", f"/sessions/{session_id}"))

    def read_events(
        self,
        session_id: str,
        cursor: int = 0,
        *,
        limit: Optional[int] = None,
        tool_output: str = "summary",
    ) -> EventBatchModel:
        query: Dict[str, Any] = {"cursor": cursor, "tool_output": tool_output}
        if limit is not None:
            query["limit"] = limit
        return EventBatchModel.from_dict(
            self._request("GET", f"/sessions/{session_id}/events", query)
        )

    def wait_events(
        self,
        session_id: str,
        cursor: int = 0,
        timeout_ms: int = 30000,
        *,
        tool_output: str = "summary",
    ) -> EventBatchModel:
        return EventBatchModel.from_dict(
            self._request(
                "GET",
                f"/sessions/{session_id}/events/wait",
                {"cursor": cursor, "timeout_ms": timeout_ms, "tool_output": tool_output},
                timeout=max(self.timeout, (timeout_ms / 1000.0) + 5),
            )
        )

    def wait_result(self, session_id: str, timeout_ms: int = 60000) -> SessionResultModel:
        return SessionResultModel.from_dict(
            self._request(
                "GET",
                f"/sessions/{session_id}/result",
                {"timeout_ms": timeout_ms},
                timeout=max(self.timeout, (timeout_ms / 1000.0) + 5),
            )
        )

    def post_message(
        self,
        session_id: str,
        text: str,
        source: str = "referee",
        target: Optional[str] = None,
    ) -> EventBatchModel:
        payload: Dict[str, Any] = {"source": source, "text": text}
        if target is not None:
            payload["target"] = target
        return EventBatchModel.from_dict(
            self._request("POST", f"/sessions/{session_id}/messages", payload)
        )

    def read_transcript(self, session_id: str, *, tool_output: str = "summary") -> str:
        result = self._request(
            "GET", f"/sessions/{session_id}/transcript", {"tool_output": tool_output}
        )
        return TranscriptModel.from_dict(result).transcript

    def stop_session(self, session_id: str) -> SessionStateModel:
        return SessionStateModel.from_dict(self._request("POST", f"/sessions/{session_id}/stop"))

    def prune_sessions(self, payload: Dict[str, Any]) -> PruneResultModel:
        return PruneResultModel.from_dict(self._request("POST", "/sessions/prune", payload))

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

        for attempt in range(2):
            request_headers = dict(headers)
            token = self._load_token(force=attempt > 0)
            if token and path != "/health":
                request_headers["Authorization"] = f"Bearer {token}"
            request = Request(url, data=data, headers=request_headers, method=method)
            try:
                with urlopen(request, timeout=timeout or self.timeout) as response:
                    _assert_compatible_api(response.headers)
                    body = response.read().decode("utf-8")
                break
            except HTTPError as exc:
                _assert_compatible_api(exc.headers)
                if exc.code == 401:
                    exc.read()
                    if attempt == 0:
                        continue
                    raise ClientError("daemon token mismatch; restart the daemon") from exc
                body = exc.read().decode("utf-8", errors="replace")
                try:
                    error_payload = json.loads(body)
                except json.JSONDecodeError:
                    raise ClientError(body or str(exc)) from exc
                if isinstance(error_payload, dict):
                    raise ClientError(
                        _format_error_payload(error_payload), payload=error_payload
                    ) from exc
                raise ClientError(str(error_payload)) from exc
            except URLError as exc:
                raise ClientError(
                    f"could not reach agent-collab daemon at {self.server_url}: {exc.reason}"
                ) from exc

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClientError(f"invalid JSON response from daemon: {body[:200]}") from exc

    def _load_token(self, *, force: bool = False) -> Optional[str]:
        if self._token_loaded and not force:
            return self._token
        override = os.environ.get(TOKEN_ENV)
        if override is not None:
            token = override.strip()
        elif is_loopback_url(self.server_url):
            from .config import load_daemon_token

            try:
                token = load_daemon_token() or ""
            except OSError:
                token = ""
        else:
            token = ""
        self._token = token or None
        self._token_loaded = True
        return self._token


def _assert_compatible_api(headers: Any) -> None:
    """Assert the daemon's advertised API major matches the client's.

    An older daemon that predates versioning sends no header; that is tolerated
    (the wire is otherwise unchanged). A header with an incompatible major means
    the client and daemon disagree on the contract — fail loudly instead of
    shape-guessing.
    """
    raw = headers.get(API_VERSION_HEADER) if headers is not None else None
    if raw is None:
        return
    try:
        major = int(str(raw).split(".", 1)[0])
    except ValueError:
        return
    if major != API_VERSION:
        raise ClientError(
            f"daemon API version {raw} is incompatible with this client "
            f"(expected major {API_VERSION}); restart the daemon"
        )


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
