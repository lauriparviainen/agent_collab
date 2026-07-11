import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest import mock

from agent_collab.api_schema import API_VERSION, API_VERSION_HEADER
from agent_collab.client import AgentCollabClient, ClientError


class _Response:
    status = 200

    def __init__(self, payload):
        self.headers = {API_VERSION_HEADER: str(API_VERSION)}
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self._body


def _unauthorized(request):
    return HTTPError(
        request.full_url,
        401,
        "Unauthorized",
        {"Content-Type": "application/json"},
        io.BytesIO(b'{"error":"unauthorized"}'),
    )


def _write_config_token(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(f'[daemon]\ntoken = "{token}"\n', encoding="utf-8")
    (home / "config.toml").chmod(0o600)


class ClientAuthTests(unittest.TestCase):
    def test_client_rereads_config_token_once_after_unauthorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _write_config_token(home, "old")
            client = AgentCollabClient()
            seen = []

            def open_request(request, timeout):
                seen.append(request.get_header("Authorization"))
                if len(seen) == 1:
                    _write_config_token(home, "new")
                    raise _unauthorized(request)
                return _Response({"sessions": []})

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}, clear=True):
                with mock.patch("agent_collab.client.urlopen", side_effect=open_request):
                    result = client.list_sessions()

        self.assertEqual(result.to_dict(), {"sessions": []})
        self.assertEqual(seen, ["Bearer old", "Bearer new"])

    def test_env_token_overrides_config_and_works_for_remote_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _write_config_token(home, "config-token")
            client = AgentCollabClient("https://daemon.example")

            def open_request(request, timeout):
                self.assertEqual(request.get_header("Authorization"), "Bearer env-token")
                return _Response({"sessions": []})

            env = {"AGENT_COLLAB_HOME": str(home), "AGENT_COLLAB_TOKEN": "env-token"}
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("agent_collab.client.urlopen", side_effect=open_request):
                    client.list_sessions()

    def test_remote_server_does_not_reuse_local_config_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _write_config_token(home, "local-only")
            client = AgentCollabClient("https://daemon.example")

            def open_request(request, timeout):
                self.assertIsNone(request.get_header("Authorization"))
                return _Response({"sessions": []})

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}, clear=True):
                with mock.patch("agent_collab.client.urlopen", side_effect=open_request):
                    client.list_sessions()

    def test_second_unauthorized_response_reports_token_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _write_config_token(home, "wrong")
            client = AgentCollabClient()

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}, clear=True):
                with mock.patch(
                    "agent_collab.client.urlopen",
                    side_effect=lambda request, timeout: (_ for _ in ()).throw(
                        _unauthorized(request)
                    ),
                ):
                    with self.assertRaisesRegex(ClientError, "daemon token mismatch"):
                        client.list_sessions()

    def test_error_response_still_checks_api_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AgentCollabClient()

            def incompatible(request, timeout):
                raise HTTPError(
                    request.full_url,
                    400,
                    "Bad Request",
                    {API_VERSION_HEADER: str(API_VERSION + 1)},
                    io.BytesIO(b'{"error":"bad request"}'),
                )

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": tmp}, clear=True):
                with mock.patch("agent_collab.client.urlopen", side_effect=incompatible):
                    with self.assertRaisesRegex(ClientError, "API version"):
                        client.list_sessions()

    def test_missing_config_means_no_token_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AgentCollabClient()

            def open_request(request, timeout):
                self.assertIsNone(request.get_header("Authorization"))
                return _Response({"sessions": []})

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": tmp}, clear=True):
                with mock.patch("agent_collab.client.urlopen", side_effect=open_request):
                    client.list_sessions()

    def test_health_does_not_send_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            _write_config_token(home, "secret")
            client = AgentCollabClient()

            def open_request(request, timeout):
                self.assertIsNone(request.get_header("Authorization"))
                return _Response({"status": "ok", "sessions": 0, "api_version": API_VERSION})

            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(home)}, clear=True):
                with mock.patch("agent_collab.client.urlopen", side_effect=open_request):
                    self.assertEqual(client.health().status, "ok")


if __name__ == "__main__":
    unittest.main()
