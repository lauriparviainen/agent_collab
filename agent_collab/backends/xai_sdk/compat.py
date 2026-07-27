"""Import shim that lets ``xai-sdk`` load under a protobuf 7 runtime.

``xai_sdk/proto/__init__.py`` (verified in 1.17.0) selects between two shipped
generated-proto trees by the protobuf runtime's major version — ``v5`` for
protobuf 5.x, ``v6`` for 6.x — and raises ``ValueError("Unsupported protobuf
version: ...")`` for anything newer. Its 6.x gencode runs fine on a 7.x
runtime: protobuf's own validator only rejects gencode *newer* than the
runtime, and one-major-older gencode is inside the official cross-version
guarantee. The gate, not protobuf, is the only obstacle to sharing one
environment with ``google-antigravity`` (whose gencode needs runtime 7.35+).

``import_xai_sdk`` therefore tries a plain import first and, only on that
specific ``ValueError``, retries while the gate sees a spoofed 6.x version
string. The spoof touches ``google.protobuf.__version__`` for the duration of
one import and always restores it. When xAI accepts protobuf 7 upstream
(https://github.com/xai-org/xai-sdk-python), the plain import succeeds and the
workaround becomes dead code.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Optional

GATE_ERROR_MARKER = "Unsupported protobuf version"
SPOOFED_VERSION = "6.999.0"
# Verified contract: xai-sdk 1.17.x v6 gencode on protobuf runtime major 7 only.
SUPPORTED_RUNTIME_MAJOR = 7
# Distribution line whose dual gencode trees + gate text were inspected. A later
# 1.x may keep the same error wording while changing or dropping the v6 tree —
# do not auto-spoof for unverified releases.
VERIFIED_XAI_SDK_SERIES = "1.17."


def _runtime_major(version: str) -> Optional[int]:
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError):
        return None


def _installed_xai_sdk_version() -> Optional[str]:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python <3.8 not supported
        return None
    try:
        return metadata.version("xai-sdk")
    except Exception:
        return None


def _is_verified_xai_sdk_series(version: Optional[str]) -> bool:
    if not version:
        return False
    return str(version).startswith(VERIFIED_XAI_SDK_SERIES)


def _is_known_protobuf_gate(exc: BaseException, real_version: str) -> bool:
    """True only for the verified 1.17 gate under a protobuf 7.x runtime."""

    if not isinstance(exc, ValueError):
        return False
    message = str(exc)
    if not message.startswith(f"{GATE_ERROR_MARKER}:"):
        return False
    # Prefer the version the gate itself reported when present.
    reported = message.split(":", 1)[1].strip()
    major = _runtime_major(reported) or _runtime_major(real_version)
    if major != SUPPORTED_RUNTIME_MAJOR:
        return False
    return _is_verified_xai_sdk_series(_installed_xai_sdk_version())


def import_xai_sdk() -> ModuleType:
    """Import and return ``xai_sdk``, defeating its protobuf-major gate.

    Raises ``ImportError`` when the package is absent and re-raises any
    ``ValueError`` that is not the known version-gate message under protobuf 7.
    """

    # The protobuf-major gate only runs on first import, so an already-imported
    # module needs no spoofing. Returning it here also keeps the shim from
    # requiring protobuf where the caller supplied `xai_sdk` itself.
    cached = sys.modules.get("xai_sdk")
    if cached is not None:
        return cached

    import google.protobuf

    real_version = google.protobuf.__version__
    try:
        import xai_sdk

        return xai_sdk
    except ValueError as exc:
        if not _is_known_protobuf_gate(exc, real_version):
            raise

    # The failed import leaves partially initialized xai_sdk modules cached;
    # drop them so the retry re-executes the gate from scratch.
    for name in [m for m in sys.modules if m == "xai_sdk" or m.startswith("xai_sdk.")]:
        del sys.modules[name]

    google.protobuf.__version__ = SPOOFED_VERSION
    try:
        import xai_sdk
    finally:
        google.protobuf.__version__ = real_version
    return xai_sdk
