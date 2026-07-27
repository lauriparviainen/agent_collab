"""xAI SDK outer-sandbox adapter: revocable ``no_local_effects`` (Stage 8).

The production turn path is remote gRPC chat only. It exposes no local tools,
callbacks, MCP, plugins, hooks, skills, subagents, child processes, or a
writable local provider-state root. Outer ``read-only`` therefore resolves to
``not_applicable_no_local_effects`` rather than Bubblewrap.

This is a versioned software capability, not an OS isolation claim. Any
local-execution surface or dependency series drift must revoke support until
the audit is renewed or the backend moves to the generic SDK worker.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from importlib import metadata
from importlib import util as importlib_util
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence, Tuple

from ...sandbox.plan import ResolvedSandboxPlan
from ...sandbox.specs import (
    BackendSandboxSpec,
    CompatibilityCheck,
    EnvironmentSpec,
    NativeSandboxProfile,
    SandboxContext,
    SandboxFailure,
    SandboxPolicy,
    SandboxSupport,
)

# Keep in lockstep with compat.py's verified series gate.
AUDITED_PACKAGE_NAME = "xai-sdk"
AUDITED_PACKAGE_SERIES = "1.17."

# Keys the production conversation may pass to ``client.chat.create``.
AUDITED_CHAT_CREATE_KEYS = frozenset(
    {
        "model",
        "store_messages",
        "previous_response_id",
        "reasoning_effort",
    }
)

# SDK chat.create surfaces that introduce tools, agent loops, remote search
# side-channels, or multi-agent expansion. Presence in production kwargs is a
# capability revocation signal.
FORBIDDEN_CHAT_CREATE_KEYS = frozenset(
    {
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "search_parameters",
        "include",
        "max_turns",
        "agent_count",
        "messages",
        "response_format",
    }
)

# Imports that would pull local file/tool execution into this backend package.
_FORBIDDEN_IMPORT_MARKERS = (
    "xai_sdk.tools",
    "xai_sdk.files",
    "from xai_sdk import tools",
    "from xai_sdk import files",
)

# Production backend must remain message-only remote chat; these markers would
# introduce local process/filesystem surfaces outside the audited claim.
_FORBIDDEN_LOCAL_EFFECT_MARKERS = (
    "subprocess.",
    "os.system",
    "os.popen",
    "os.remove",
    "os.unlink",
    "os.mkdir",
    "os.makedirs",
    "os.rename",
    "os.replace",
    "os.chmod",
    "os.symlink",
    "os.link",
    "Popen(",
    "shutil.",
    ".write_text(",
    ".write_bytes(",
    ".touch(",
    ".unlink(",
    ".mkdir(",
    ".rmdir(",
    ".rename(",
    ".replace(",
    ".chmod(",
    ".symlink_to(",
    ".hardlink_to(",
    "open(",
    "__import__(",
    "importlib.import_module",
)

# AST Call names/attrs that mutate the local host or spawn processes.
_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "open",
        "Popen",
        "system",
        "popen",
        "remove",
        "unlink",
        "mkdir",
        "makedirs",
        "rename",
        "replace",
        "chmod",
        "symlink",
        "link",
        "copy",
        "copyfile",
        "copy2",
        "move",
        "rmtree",
        "touch",
        "write_text",
        "write_bytes",
        "symlink_to",
        "hardlink_to",
        "NamedTemporaryFile",
        "TemporaryDirectory",
        "mkstemp",
        "mkdtemp",
        "TemporaryFile",
        "SpooledTemporaryFile",
        "__import__",
        "exec",
        "eval",
        "compile",
        "import_module",
    }
)

# Absolute top-level modules the production backend may import.
_ALLOWED_ABSOLUTE_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "asyncio",
        "pathlib",
        "typing",
        # Provider package: only used by the production conversation factory
        # (via import_xai_sdk + AsyncClient/user). Not a local-effects surface.
        "xai_sdk",
    }
)

# Exact relative import targets currently used by backend.py. A new sibling
# module (for example `.local_trace`) must be listed only after renewing the
# no_local_effects audit, so undeclared local-effect helpers cannot hide
# behind package-relative imports.
_ALLOWED_RELATIVE_IMPORTS = frozenset(
    {
        (3, "backend_contract"),
        (3, "config"),
        (3, "events"),
        (3, "outcomes"),
        (3, "runners"),
        (2, "base"),
        (2, "common.health"),
        (2, "common.options"),
        (2, "common.sdk"),
        (1, "compat"),
        (1, "sandbox"),
    }
)


def _ast_forbidden_calls(source: str) -> Tuple[str, ...]:
    """Return forbidden Call names found by AST walk (structural, not markers)."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source is not parseable for the local-effects audit",
            remediation=("Fix syntax in the production backend or renew the audit.",),
        ) from exc
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
            found.append(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_CALL_NAMES:
            found.append(func.attr)
    return tuple(found)


def _ast_forbidden_imports(source: str) -> Tuple[str, ...]:
    """Return absolute or relative imports outside the audited allowlists."""

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source is not parseable for the import allowlist audit",
            remediation=("Fix syntax in the production backend or renew the audit.",),
        ) from exc
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in _ALLOWED_ABSOLUTE_IMPORT_ROOTS:
                    found.append(root)
        elif isinstance(node, ast.ImportFrom):
            level = int(node.level or 0)
            if level > 0:
                key = (level, node.module or "")
                if key not in _ALLOWED_RELATIVE_IMPORTS:
                    found.append(f"{'.' * level}{node.module or ''}")
                continue
            if not node.module:
                continue
            root = node.module.split(".", 1)[0]
            if root not in _ALLOWED_ABSOLUTE_IMPORT_ROOTS:
                found.append(root)
    return tuple(found)


_BACKEND_SOURCE = Path(__file__).with_name("backend.py")
_SANDBOX_SOURCE = Path(__file__)


def installed_xai_sdk_version() -> Optional[str]:
    """Return the installed distribution version, or None only when absent.

    Any metadata error other than genuine package absence is a capability
    failure: treating corrupted dist-info as "not installed" would skip the
    import-identity audit while a shadow module still runs.
    """

    try:
        return metadata.version(AUDITED_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return None
    except Exception as exc:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"failed to read installed {AUDITED_PACKAGE_NAME} metadata ({type(exc).__name__})",
            remediation=(
                "Repair the xai-sdk installation metadata, or renew the no_local_effects audit.",
            ),
        ) from exc


def is_audited_package_series(version: Optional[str]) -> bool:
    if not version:
        return False
    return str(version).startswith(AUDITED_PACKAGE_SERIES)


# Sole allowed create form: expand production_chat_kwargs directly into create
# so no intermediate dict can be mutated after validation.
_AUDITED_CREATE_RE = re.compile(
    r"chat\.create\s*\(\s*\*\*\s*production_chat_kwargs\s*\(",
    re.MULTILINE,
)


def audit_production_source(source: Optional[str] = None) -> None:
    """Fail closed when the production backend source drifts from the audit.

    Exact request/source checks are authoritative for exposed tool paths: the
    production module must not import local tool/file helpers or pass forbidden
    ``chat.create`` kwargs. The sole allowed create form expands
    ``production_chat_kwargs(...)`` directly into ``chat.create`` so post-builder
    mutation of an intermediate dict cannot reintroduce tools.
    """

    text = source if source is not None else _BACKEND_SOURCE.read_text(encoding="utf-8")
    for marker in _FORBIDDEN_IMPORT_MARKERS:
        if marker in text:
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk production source imports forbidden local surface {marker!r}",
                remediation=(
                    "Remove local tool/file imports or move this backend to the "
                    "generic SDK worker and renew the outer-sandbox audit.",
                ),
            )
    for marker in _FORBIDDEN_LOCAL_EFFECT_MARKERS:
        if marker in text:
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk production source introduces local-effect marker {marker!r}",
                remediation=(
                    "Remove local process/filesystem use from the production path, "
                    "or move this backend to the generic SDK worker and renew the audit.",
                ),
            )
    forbidden_calls = _ast_forbidden_calls(text)
    if forbidden_calls:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source contains forbidden local-effect call(s): "
            f"{sorted(set(forbidden_calls))!r}",
            remediation=(
                "Remove local process/filesystem calls from the production path, "
                "or move this backend to the generic SDK worker and renew the audit.",
            ),
        )
    forbidden_imports = _ast_forbidden_imports(text)
    if forbidden_imports:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source imports unaudited module(s): "
            f"{sorted(set(forbidden_imports))!r}",
            remediation=(
                "Remove the import or add it to the Stage 8 import allowlist "
                "after renewing the no_local_effects audit.",
            ),
        )
    # Reject rebinding of chat.create without an immediate call (alias bypass).
    if re.search(r"=\s*[^\n]*\.chat\.create\b(?!\s*\()", text):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source rebinds chat.create (alias would bypass audit)",
            remediation=(
                "Call chat.create only in the audited production_chat_kwargs form, "
                "or renew the no_local_effects audit.",
            ),
        )
    if re.search(r"getattr\s*\([^)]*['\"]create['\"]", text):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source uses getattr create (alias would bypass audit)",
            remediation=(
                "Call chat.create only in the audited production_chat_kwargs form, "
                "or renew the no_local_effects audit.",
            ),
        )
    create_calls = len(re.findall(r"chat\.create\s*\(", text))
    audited_calls = len(_AUDITED_CREATE_RE.findall(text))
    # Every textual mention of chat.create must be the audited call form.
    create_mentions = len(re.findall(r"chat\.create\b", text))
    if create_calls == 0:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source no longer calls chat.create",
            remediation=(
                "Restore the audited chat.create(**production_chat_kwargs(...)) "
                "call site or renew the no_local_effects audit.",
            ),
        )
    if audited_calls != create_calls or audited_calls != create_mentions:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "xai_sdk production source has a chat.create use that is not the "
            "audited chat.create(**production_chat_kwargs(...)) form",
            remediation=(
                "Expand production_chat_kwargs directly into chat.create with no "
                "intermediate mutable kwargs dict or aliases, or renew the audit.",
            ),
        )
    # Reject forbidden keys as direct kwargs or string literals (dict drift).
    for key in sorted(FORBIDDEN_CHAT_CREATE_KEYS):
        if re.search(rf"chat\.create\([^)]*\b{re.escape(key)}\s*=", text):
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk production chat.create passes forbidden key {key!r}",
                remediation=("Remove the local/tool surface or renew the no_local_effects audit.",),
            )
        if re.search(rf"""['\"]{re.escape(key)}['\"]""", text):
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk production source mentions forbidden chat key {key!r}",
                remediation=("Remove the local/tool surface or renew the no_local_effects audit.",),
            )
    # Map-options subscript assignments must stay within the audited set.
    for match in re.finditer(
        r"""mapped\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]\s*=""",
        text,
    ):
        key = match.group(1)
        if key in FORBIDDEN_CHAT_CREATE_KEYS:
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk production path assigns forbidden chat key {key!r}",
                remediation=("Remove the local/tool surface or renew the no_local_effects audit.",),
            )
        if key not in AUDITED_CHAT_CREATE_KEYS:
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk production path assigns unaudited chat key {key!r}",
                remediation=(
                    "Add the key to the Stage 8 audited set after review, "
                    "or remove it from the production path.",
                ),
            )
    # production_chat_kwargs lives in this module and runs on every turn — audit
    # only that function body for local-effect markers (not this module's marker
    # tables or other audit helpers).
    builder_source = _SANDBOX_SOURCE.read_text(encoding="utf-8")
    builder_match = re.search(
        r"^def production_chat_kwargs\b.*?(?=^def |\Z)",
        builder_source,
        re.MULTILINE | re.DOTALL,
    )
    if builder_match is None:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "production_chat_kwargs is missing from the xai_sdk sandbox module",
            remediation=("Restore the audited kwargs builder or renew the audit.",),
        )
    builder_body = builder_match.group(0)
    for marker in _FORBIDDEN_LOCAL_EFFECT_MARKERS:
        if marker in builder_body:
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                f"xai_sdk sandbox helper introduces local-effect marker {marker!r}",
                remediation=(
                    "Keep production_chat_kwargs free of process/filesystem side "
                    "effects, or renew the no_local_effects audit.",
                ),
            )
    builder_calls = _ast_forbidden_calls(builder_body)
    if builder_calls:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "production_chat_kwargs contains forbidden local-effect call(s): "
            f"{sorted(set(builder_calls))!r}",
            remediation=(
                "Keep production_chat_kwargs free of process/filesystem side "
                "effects, or renew the no_local_effects audit.",
            ),
        )


def audit_installed_package_version(version: Optional[str] = None) -> None:
    """When xai-sdk is installed, require the audited 1.17.x series."""

    installed = installed_xai_sdk_version() if version is None else version
    if installed is None:
        # Package absence is a health/install concern, not an outer-sandbox
        # revocation. The capability describes our production source surface.
        return
    if not is_audited_package_series(installed):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"installed {AUDITED_PACKAGE_NAME} {installed!r} is outside the "
            f"audited {AUDITED_PACKAGE_SERIES}x series for no_local_effects",
            remediation=(
                "Pin xai-sdk to the audited 1.17.x series, or renew the "
                "no_local_effects audit for the new dependency series.",
            ),
        )


def audit_import_identity() -> None:
    """When the package is installed, require the production import identity.

    Metadata alone is insufficient: a shadow ``xai_sdk`` earlier on ``sys.path``
    can execute arbitrary local effects while the installed 1.17.x dist still
    satisfies the series pin. The production import helper must resolve to that
    distribution's package tree and an audited module version.

    Skip only when distribution metadata is absent (package not installed). When
    metadata says the package is present, every import outcome is validated:
    package code can raise ``ImportError`` *after* side effects, so a broad
    ``ImportError`` skip would leave ``no_local_effects`` intact after a shadow
    write.
    """

    if installed_xai_sdk_version() is None:
        # Metadata absence is only safe when the package is also unresolvable.
        # An importable shadow without dist-info would otherwise keep
        # no_local_effects while health may still report the module present.
        try:
            absent_spec = importlib_util.find_spec("xai_sdk")
        except Exception as exc:
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                "resolving xai_sdk without installed distribution metadata failed "
                f"({type(exc).__name__})",
                remediation=(
                    "Remove shadow xai_sdk packages from sys.path, or install the "
                    "audited xai-sdk 1.17.x distribution.",
                ),
            ) from exc
        if absent_spec is not None and getattr(absent_spec, "origin", None):
            raise SandboxFailure(
                "outer_sandbox_backend_incompatible",
                "xai_sdk is resolvable without installed xai-sdk distribution "
                "metadata (possible sys.path shadow)",
                remediation=(
                    "Remove the shadow xai_sdk package from sys.path, or install "
                    "the audited xai-sdk 1.17.x distribution.",
                ),
            )
        return
    try:
        dist = metadata.distribution(AUDITED_PACKAGE_NAME)
    except Exception as exc:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "installed xai-sdk metadata is not readable for identity audit",
            remediation=(
                "Repair the xai-sdk installation, or remove any shadow module from sys.path.",
            ),
        ) from exc
    expected_init = Path(str(dist.locate_file("xai_sdk/__init__.py"))).resolve()
    expected_pkg = Path(str(dist.locate_file("xai_sdk"))).resolve()
    # Resolve the import target without executing package code. PathFinder
    # find_spec returns origin without running __init__.py; executing first
    # would let a shadow module write before we reject it.
    try:
        spec = importlib_util.find_spec("xai_sdk")
    except Exception as exc:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"resolving xai_sdk import location for identity audit failed ({type(exc).__name__})",
            remediation=(
                "Fix the xai_sdk import path (remove shadow packages, repair the "
                "install), or renew the no_local_effects audit.",
            ),
        ) from exc
    if spec is None or not spec.origin:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "installed xai-sdk metadata is present but xai_sdk cannot be resolved",
            remediation=("Repair the xai-sdk installation or remove broken sys.path entries.",),
        )
    resolved_origin = Path(spec.origin).resolve()
    if resolved_origin != expected_init and resolved_origin.parent != expected_pkg:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "resolved xai_sdk module path does not match the installed "
            f"{AUDITED_PACKAGE_NAME} distribution (possible sys.path shadow)",
            remediation=(
                "Remove the shadow xai_sdk package from sys.path, or renew the "
                "no_local_effects audit for the module that would be imported.",
            ),
        )
    try:
        from .compat import import_xai_sdk

        module = import_xai_sdk()
    except SandboxFailure:
        raise
    except Exception as exc:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "importing installed xai_sdk for the no_local_effects identity audit "
            f"failed ({type(exc).__name__})",
            remediation=(
                "Fix the xai_sdk import path (remove shadow packages, repair the "
                "install), or renew the no_local_effects audit.",
            ),
        ) from exc
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "importable xai_sdk has no __file__ for distribution identity check",
            remediation=("Use a regular package install of xai-sdk 1.17.x.",),
        )
    actual = Path(module_file).resolve()
    if actual != expected_init and actual.parent != expected_pkg:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            "imported xai_sdk module path does not match the installed "
            f"{AUDITED_PACKAGE_NAME} distribution (possible sys.path shadow)",
            remediation=(
                "Remove the shadow xai_sdk package from sys.path, or renew the "
                "no_local_effects audit for the module that is actually imported.",
            ),
        )
    module_version = getattr(module, "__version__", None)
    if module_version is not None and not is_audited_package_series(str(module_version)):
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"imported xai_sdk reports version {module_version!r}, outside the "
            f"audited {AUDITED_PACKAGE_SERIES}x series",
            remediation=(
                "Pin xai-sdk to the audited 1.17.x series, or renew the "
                "no_local_effects audit for the new dependency series.",
            ),
        )
    # Series pin still applies to the distribution metadata when importable.
    audit_installed_package_version()


def production_chat_kwargs(
    options: Mapping[str, Any],
    *,
    previous_response_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the exact chat.create kwargs used by the production conversation.

    Shared with tests so the deny probe and the runtime path cannot drift.
    """

    kwargs: dict[str, Any] = {
        "store_messages": True,
    }
    model = options.get("model")
    if model:
        kwargs["model"] = model
    if options.get("thinking_level"):
        kwargs["reasoning_effort"] = options["thinking_level"]
    elif options.get("reasoning_effort"):
        kwargs["reasoning_effort"] = options["reasoning_effort"]
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    unknown = set(kwargs) - AUDITED_CHAT_CREATE_KEYS
    if unknown:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"xai_sdk chat kwargs include unaudited keys {sorted(unknown)!r}",
        )
    forbidden = set(kwargs) & FORBIDDEN_CHAT_CREATE_KEYS
    if forbidden:
        raise SandboxFailure(
            "outer_sandbox_backend_incompatible",
            f"xai_sdk chat kwargs include forbidden keys {sorted(forbidden)!r}",
        )
    return kwargs


def surface_is_audited() -> bool:
    """True when production source, package series, and import identity match."""

    try:
        audit_production_source()
        audit_installed_package_version()
        audit_import_identity()
    except SandboxFailure:
        return False
    return True


@dataclass(frozen=True)
class XaiSdkSandboxAdapter:
    """Declare the audited no-local-effects shape for ``xai_sdk``."""

    def describe(self, context: SandboxContext) -> BackendSandboxSpec:
        del context
        if not surface_is_audited():
            return BackendSandboxSpec(
                support=SandboxSupport.UNSUPPORTED,
                policies=frozenset({SandboxPolicy.NONE}),
            )
        return BackendSandboxSpec(
            support=SandboxSupport.NO_LOCAL_EFFECTS,
            policies=frozenset({SandboxPolicy.READ_ONLY, SandboxPolicy.NONE}),
            state_roots=(),
            provider_visible_paths=(),
            environment=EnvironmentSpec(),
            native_profile=NativeSandboxProfile(
                summary={
                    "shape": "no_local_effects",
                    "local_tools": "none",
                    "local_state_root": "none",
                    "local_children": "none",
                    "transport": "remote_grpc_chat_only",
                    "audited_package": f"{AUDITED_PACKAGE_NAME} {AUDITED_PACKAGE_SERIES}x",
                },
            ),
            compatibility=(
                CompatibilityCheck(
                    "audited_xai_sdk_version",
                    audit_installed_package_version,
                ),
                CompatibilityCheck(
                    "audited_production_surface",
                    audit_production_source,
                ),
                CompatibilityCheck(
                    "audited_import_identity",
                    audit_import_identity,
                ),
            ),
            external_services=(
                "xAI remote model API (external service outside filesystem boundary)",
            ),
        )

    def prepare_inner(
        self,
        plan: ResolvedSandboxPlan,
        command: Sequence[str],
    ) -> Tuple[str, ...]:
        del plan
        return tuple(command)
