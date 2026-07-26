"""Outer-sandbox policy resolution."""

from __future__ import annotations

from typing import Optional, Union

from .specs import (
    ResolvedSandboxPolicy,
    SandboxFailure,
    SandboxPolicy,
    SandboxPolicySource,
)


def parse_sandbox_policy(
    value: Union[str, SandboxPolicy, None],
    *,
    field: str,
    allow_none: bool = True,
) -> Optional[SandboxPolicy]:
    if value is None and allow_none:
        return None
    if isinstance(value, SandboxPolicy):
        return value
    if isinstance(value, str):
        try:
            return SandboxPolicy(value)
        except ValueError:
            pass
    allowed = ", ".join(repr(item.value) for item in SandboxPolicy)
    raise SandboxFailure(
        "outer_sandbox_policy_invalid",
        f"{field} must be one of {allowed}",
    )


def resolve_sandbox_policy(
    requested: Union[str, SandboxPolicy, None],
    configured_default: Union[str, SandboxPolicy],
    installation_override: Union[str, SandboxPolicy, None] = None,
    *,
    legacy_session: bool = False,
) -> ResolvedSandboxPolicy:
    request_value = parse_sandbox_policy(requested, field="sandbox")
    default_value = parse_sandbox_policy(
        configured_default,
        field="system.sandbox_default",
        allow_none=False,
    )
    override_value = parse_sandbox_policy(
        installation_override,
        field="system.sandbox_override",
    )
    assert default_value is not None

    if legacy_session:
        if override_value is SandboxPolicy.READ_ONLY:
            raise SandboxFailure(
                "outer_sandbox_legacy_session",
                "this legacy session was created without an outer sandbox and cannot continue "
                "under the installation read-only override; start a new session",
                remediation=("Start a new session so every turn uses the required boundary.",),
            )
        return ResolvedSandboxPolicy(
            requested=None,
            effective=SandboxPolicy.NONE,
            source=SandboxPolicySource.LEGACY_SESSION,
        )

    if override_value is not None:
        if request_value is not None and request_value is not override_value:
            raise SandboxFailure(
                "outer_sandbox_override_conflict",
                f"sandbox={request_value.value!r} conflicts with the installation override "
                f"{override_value.value!r}",
                remediation=(
                    "Use the installation policy value or ask the operator to change "
                    "system.sandbox_override.",
                ),
            )
        return ResolvedSandboxPolicy(
            requested=request_value,
            effective=override_value,
            source=SandboxPolicySource.INSTALLATION_OVERRIDE,
        )
    if request_value is not None:
        return ResolvedSandboxPolicy(
            requested=request_value,
            effective=request_value,
            source=SandboxPolicySource.REQUEST,
        )
    return ResolvedSandboxPolicy(
        requested=None,
        effective=default_value,
        source=SandboxPolicySource.CONFIGURED_DEFAULT,
    )
