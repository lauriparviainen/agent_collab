"""Repository setup checks and generated daemon REST API documentation."""

from __future__ import annotations

import argparse
from dataclasses import MISSING, fields, is_dataclass
import json
from pathlib import Path
import types
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from .api_schema import (
    API_VERSION,
    API_VERSION_HEADER,
    ROUTES,
    ErrorModel,
    EventBatchModel,
    EventModel,
    HealthModel,
    OptionsRequestModel,
    PostMessageRequestModel,
    PruneResultModel,
    PruneSessionDetailModel,
    PruneSessionsRequestModel,
    ReadEventsRequestModel,
    SessionListModel,
    SessionStateModel,
    StartSessionRequestModel,
    TranscriptModel,
    TranscriptRequestModel,
    WaitEventsRequestModel,
)
from .config import load_config


REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = REPO_ROOT / "doc" / "daemon_api_doc"
OPENAPI_PATH = DOC_DIR / "openapi.json"
HTTP_API_PATH = DOC_DIR / "http-api.md"

_MODELS = (
    HealthModel,
    SessionStateModel,
    SessionListModel,
    EventModel,
    EventBatchModel,
    TranscriptModel,
    ErrorModel,
    PruneResultModel,
    PruneSessionDetailModel,
    StartSessionRequestModel,
    OptionsRequestModel,
    PostMessageRequestModel,
    PruneSessionsRequestModel,
    ReadEventsRequestModel,
    WaitEventsRequestModel,
    TranscriptRequestModel,
)
_REQUEST_MODELS = {
    StartSessionRequestModel,
    OptionsRequestModel,
    PostMessageRequestModel,
    PruneSessionsRequestModel,
    ReadEventsRequestModel,
    WaitEventsRequestModel,
    TranscriptRequestModel,
}
_UNION_ORIGINS = {Union}
if hasattr(types, "UnionType"):
    _UNION_ORIGINS.add(types.UnionType)
_FIELD_SCHEMAS: Dict[Tuple[type, str], Dict[str, Any]] = {
    (HealthModel, "status"): {"const": "ok"},
    (HealthModel, "sessions"): {"minimum": 0},
    (HealthModel, "api_version"): {"const": API_VERSION},
    (SessionStateModel, "status"): {
        "enum": ["running", "awaiting_input", "done", "failed", "stopped", "interrupted"]
    },
    (StartSessionRequestModel, "task"): {"minLength": 1, "pattern": r".*\S.*"},
    (StartSessionRequestModel, "workdir"): {"minLength": 1, "pattern": r".*\S.*"},
    (OptionsRequestModel, "workdir"): {"minLength": 1, "pattern": r".*\S.*"},
    (OptionsRequestModel, "health_refresh"): {"enum": ["cached", "fresh"]},
    (PostMessageRequestModel, "text"): {"minLength": 1, "pattern": r".*\S.*"},
    (PostMessageRequestModel, "source"): {"enum": ["human", "referee"]},
    (ReadEventsRequestModel, "cursor"): {"minimum": 0},
    (ReadEventsRequestModel, "limit"): {"minimum": 1},
    (ReadEventsRequestModel, "tool_output"): {"enum": ["summary", "full"]},
    (WaitEventsRequestModel, "cursor"): {"minimum": 0},
    (WaitEventsRequestModel, "timeout_ms"): {"minimum": 0},
    (WaitEventsRequestModel, "tool_output"): {"enum": ["summary", "full"]},
    (TranscriptRequestModel, "tool_output"): {"enum": ["summary", "full"]},
    (PruneSessionsRequestModel, "keep"): {"minimum": 0},
    (PruneSessionsRequestModel, "older_than"): {"pattern": r"^\s*[0-9]+[hdw]\s*$"},
    (PruneSessionDetailModel, "disposition"): {
        "enum": ["pruned", "preview", "kept", "skipped_no_timestamp", "skipped_live", "failed"]
    },
    (PruneSessionDetailModel, "bytes_reclaimed"): {"minimum": 0},
    (PruneResultModel, "candidates"): {"minimum": 0},
    (PruneResultModel, "pruned"): {"minimum": 0},
    (PruneResultModel, "failed"): {"minimum": 0},
    (PruneResultModel, "bytes_reclaimed"): {"minimum": 0},
    (PruneResultModel, "unparseable_records"): {"minimum": 0},
    (PruneResultModel, "keep"): {"minimum": 0},
}


class SetupError(RuntimeError):
    pass


def generate_openapi() -> Dict[str, Any]:
    """Build OpenAPI 3.1 from the shared DTOs and REST route registry."""

    paths: Dict[str, Any] = {}
    for route in ROUTES:
        operation = _operation(route)
        paths.setdefault(route.path, {})[route.method.lower()] = operation

    schemas = {
        model.__name__: _model_schema(model, request=model in _REQUEST_MODELS) for model in _MODELS
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "agent-collab daemon REST API",
            "version": str(API_VERSION),
            "description": (
                "Generated from agent_collab.api_schema DTOs and ROUTES. "
                "/options response fields are runtime-defined; /mcp is JSON-RPC and excluded."
            ),
        },
        "servers": [{"url": "http://127.0.0.1:8765"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": schemas,
        },
    }


def render_openapi(schema: Optional[Mapping[str, Any]] = None) -> str:
    return json.dumps(schema or generate_openapi(), indent=2, sort_keys=True) + "\n"


def render_http_api(schema: Optional[Mapping[str, Any]] = None) -> str:
    document = schema or generate_openapi()
    lines = [
        "# Daemon REST API",
        "",
        "<!-- Generated by ./agent_collab.sh setup; do not edit by hand. -->",
        "",
        f"API major: `{API_VERSION}`. Every response carries `{API_VERSION_HEADER}: {API_VERSION}`.",
        "",
        "All operations except `GET /health` require the per-daemon Bearer token. "
        "`/options` responses are runtime-defined, and `/mcp` is JSON-RPC rather than REST, "
        "so neither has a fixed response schema here.",
        "",
        "| Method | Path | Authentication | Request | Response |",
        "| --- | --- | --- | --- | --- |",
    ]
    route_by_key = {(route.method, route.path): route for route in ROUTES}
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            route = route_by_key[(method.upper(), path)]
            request = route.request_model.__name__ if route.request_model else "—"
            response = "runtime object" if route.dynamic_response else route.response_model.__name__
            auth = "open" if (route.method, route.path) == ("GET", "/health") else "Bearer"
            lines.append(f"| `{route.method}` | `{route.path}` | {auth} | {request} | {response} |")

    lines.extend(["", "## Operations", ""])
    for route in ROUTES:
        operation = document["paths"][route.path][route.method.lower()]
        lines.extend(
            [
                f"### {route.method} `{route.path}`",
                "",
                operation["summary"],
                "",
                f"- Operation ID: `{operation['operationId']}`",
                f"- Authentication: {'none' if operation['security'] == [] else 'Bearer token'}",
                f"- Request model: `{route.request_model.__name__}`"
                if route.request_model
                else "- Request model: none",
                (
                    "- Response model: runtime-defined object"
                    if route.dynamic_response
                    else f"- Response model: `{route.response_model.__name__}`"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Generated artifacts",
            "",
            "The machine-readable contract is [openapi.json](openapi.json). Regenerate both files with "
            "`./agent_collab.sh setup`; verify them without writes using `./agent_collab.sh setup --check`.",
            "",
        ]
    )
    return "\n".join(lines)


def run_setup(
    workdir: Path,
    *,
    check: bool = False,
    openapi_path: Path = OPENAPI_PATH,
    http_api_path: Path = HTTP_API_PATH,
) -> Tuple[int, int]:
    """Validate effective config and write or verify generated API artifacts."""

    config = load_config(workdir.expanduser().resolve())
    schema = generate_openapi()
    outputs = {
        openapi_path: render_openapi(schema),
        http_api_path: render_http_api(schema),
    }
    drift = []
    for path, expected in outputs.items():
        if check:
            try:
                actual = path.read_text(encoding="utf-8")
            except OSError:
                actual = None
            if actual != expected:
                drift.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    if drift:
        names = ", ".join(str(path) for path in drift)
        raise SetupError(
            f"generated setup artifacts are stale: {names}; run ./agent_collab.sh setup"
        )
    return len(config.agents), len(config.workflows)


def _operation(route: Any) -> Dict[str, Any]:
    protected = (route.method, route.path) != ("GET", "/health")
    response_schema: Dict[str, Any]
    if route.dynamic_response:
        response_schema = {"type": "object", "additionalProperties": True}
    else:
        response_schema = _ref(route.response_model)
    success = _response("Successful response", response_schema)
    error = _response("Error response", _ref(ErrorModel))
    responses: Dict[str, Any] = {"200": success, "400": error, "404": error, "default": error}
    if protected:
        responses["401"] = error

    operation: Dict[str, Any] = {
        "operationId": f"{route.handler}_{route.method.lower()}",
        "summary": _summary(route),
        "security": [{"bearerAuth": []}] if protected else [],
        "responses": responses,
    }
    parameters = _path_parameters(route.path)
    if route.method == "GET" and route.request_model is not None:
        parameters.extend(_query_parameters(route.request_model))
    if parameters:
        operation["parameters"] = parameters
    if route.method != "GET" and route.request_model is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _ref(route.request_model)}},
        }
    return operation


def _summary(route: Any) -> str:
    names = {
        "health": "Read daemon liveness and API version",
        "options": "Describe workdir-scoped runtime options",
        "start_session": "Start a collaboration session",
        "list_sessions": "List daemon sessions",
        "get_session": "Read one session",
        "read_events": "Read session events from a cursor",
        "wait_events": "Long-poll session events from a cursor",
        "post_message": "Post input to an interactive session",
        "read_transcript": "Read a session transcript",
        "stop_session": "Stop a live session",
        "prune_sessions": "Preview or apply terminal-session retention",
    }
    return names[route.handler]


def _response(description: str, schema: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "description": description,
        "headers": {
            API_VERSION_HEADER: {
                "description": "Daemon REST API major version",
                "schema": {"type": "integer", "const": API_VERSION},
            }
        },
        "content": {"application/json": {"schema": dict(schema)}},
    }


def _path_parameters(path: str) -> List[Dict[str, Any]]:
    return [
        {
            "name": part[1:-1],
            "in": "path",
            "required": True,
            "schema": {"type": "string", "minLength": 1},
        }
        for part in path.split("/")
        if part.startswith("{") and part.endswith("}")
    ]


def _query_parameters(model: type) -> List[Dict[str, Any]]:
    schema = _model_schema(model, request=True)
    required = set(schema.get("required", []))
    return [
        {
            "name": name,
            "in": "query",
            "required": name in required,
            "schema": value,
        }
        for name, value in schema["properties"].items()
    ]


def _model_schema(model: type, *, request: bool) -> Dict[str, Any]:
    hints = get_type_hints(model)
    properties: Dict[str, Any] = {}
    required = []
    declared_required = set(getattr(model, "REQUIRED_FIELDS", ()))
    for item in fields(model):
        field_schema = _type_schema(hints.get(item.name, Any))
        if request:
            default = _field_default(item)
            if default is not MISSING:
                field_schema = dict(field_schema)
                field_schema["default"] = default
            if item.name in declared_required or (
                not declared_required
                and item.default is MISSING
                and item.default_factory is MISSING
            ):
                required.append(item.name)
        elif not (model is ErrorModel and item.name == "details"):
            required.append(item.name)
        properties[item.name] = field_schema
        properties[item.name].update(_FIELD_SCHEMAS.get((model, item.name), {}))
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _field_default(item: Any) -> Any:
    if item.default is not MISSING:
        return item.default
    if item.default_factory is not MISSING:
        return item.default_factory()
    return MISSING


def _type_schema(annotation: Any) -> Dict[str, Any]:
    if annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in _UNION_ORIGINS:
        return {"anyOf": [_type_schema(item) for item in args]}
    if origin in {list, List, tuple, Tuple}:
        return {"type": "array", "items": _type_schema(args[0] if args else Any)}
    if origin in {dict, Dict, Mapping}:
        value = args[1] if len(args) == 2 else Any
        return {"type": "object", "additionalProperties": _type_schema(value)}
    if annotation is type(None):
        return {"type": "null"}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if is_dataclass(annotation):
        return _ref(annotation)
    return {}


def _ref(model: type) -> Dict[str, str]:
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate repository setup and generate API artifacts."
    )
    parser.add_argument(
        "--check", action="store_true", help="Fail if generated artifacts differ; do not write."
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=REPO_ROOT,
        help="Workdir whose effective config is validated.",
    )
    args = parser.parse_args(argv)
    try:
        agents, workflows = run_setup(args.workdir, check=args.check)
    except (OSError, ValueError, SetupError) as exc:
        print(f"setup error: {exc}")
        return 1
    action = "verified" if args.check else "generated"
    print(
        f"validated config: {args.workdir.expanduser().resolve()} ({agents} agents, {workflows} workflows)"
    )
    print(f"{action}: {OPENAPI_PATH}")
    print(f"{action}: {HTTP_API_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
