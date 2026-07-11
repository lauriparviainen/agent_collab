import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.api_schema import API_VERSION, ROUTES
from agent_collab.project_setup import (
    HTTP_API_PATH,
    OPENAPI_PATH,
    SetupError,
    generate_openapi,
    render_http_api,
    render_openapi,
    run_setup,
)


class OpenApiGenerationTests(unittest.TestCase):
    def test_generated_paths_match_route_registry_exactly(self):
        schema = generate_openapi()
        generated = {
            (method.upper(), path)
            for path, methods in schema["paths"].items()
            for method in methods
        }
        self.assertEqual(generated, {(route.method, route.path) for route in ROUTES})
        self.assertNotIn("/mcp", schema["paths"])

    def test_generated_contract_captures_auth_dynamic_options_and_query_rules(self):
        schema = generate_openapi()
        self.assertEqual(schema["info"]["version"], str(API_VERSION))
        self.assertEqual(schema["paths"]["/health"]["get"]["security"], [])
        self.assertEqual(schema["paths"]["/sessions"]["get"]["security"], [{"bearerAuth": []}])
        options_response = schema["paths"]["/options"]["post"]["responses"]["200"]
        self.assertTrue(
            options_response["content"]["application/json"]["schema"]["additionalProperties"]
        )
        parameters = {
            item["name"]: item
            for item in schema["paths"]["/sessions/{session_id}/events"]["get"]["parameters"]
        }
        self.assertEqual(parameters["cursor"]["schema"]["minimum"], 0)
        self.assertEqual(parameters["limit"]["schema"]["minimum"], 1)
        self.assertEqual(parameters["tool_output"]["schema"]["enum"], ["summary", "full"])

    def test_checked_in_artifacts_are_current(self):
        schema = generate_openapi()
        self.assertEqual(OPENAPI_PATH.read_text(encoding="utf-8"), render_openapi(schema))
        self.assertEqual(HTTP_API_PATH.read_text(encoding="utf-8"), render_http_api(schema))
        self.assertEqual(json.loads(render_openapi(schema))["openapi"], "3.1.0")


class SetupWorkflowTests(unittest.TestCase):
    def test_setup_validates_config_writes_and_checks_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            openapi_path = root / "generated" / "openapi.json"
            http_api_path = root / "generated" / "http-api.md"
            with mock.patch.dict(os.environ, {"AGENT_COLLAB_HOME": str(root / "home")}, clear=True):
                agents, workflows = run_setup(
                    workdir,
                    openapi_path=openapi_path,
                    http_api_path=http_api_path,
                )
                self.assertGreater(agents, 0)
                self.assertGreater(workflows, 0)
                run_setup(
                    workdir,
                    check=True,
                    openapi_path=openapi_path,
                    http_api_path=http_api_path,
                )

                openapi_path.write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(SetupError, "run ./agent_collab.sh setup"):
                    run_setup(
                        workdir,
                        check=True,
                        openapi_path=openapi_path,
                        http_api_path=http_api_path,
                    )


if __name__ == "__main__":
    unittest.main()
