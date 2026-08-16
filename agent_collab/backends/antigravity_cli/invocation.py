"""Antigravity CLI ordinary-argv audit and strict resume-by-id finalizer."""

from __future__ import annotations

from typing import Any, Mapping, NoReturn, Optional, Sequence, Tuple

from ...sandbox.specs import SandboxFailure
from ..common.cli import reject_cli_ownership_flags

CLI_OWNERSHIP_FLAGS = ("--conversation", "--continue")
_PRINT_MARKERS = ("-p", "--print", "--prompt")


def reject_antigravity_ownership_flags(command: Sequence[str]) -> None:
    """Reject user-configured ``--conversation`` / ``--continue`` under either policy."""

    reject_cli_ownership_flags(command, CLI_OWNERSHIP_FLAGS)


def finalize_antigravity_cli_invocation(
    prepared: Sequence[str],
    descriptor: Optional[Mapping[str, Any]],
) -> Tuple[str, ...]:
    """Insert only ``--conversation <id>`` before the print marker, or stay identity."""

    if descriptor is None:
        return tuple(prepared)
    session_id = descriptor.get("provider_session_id")
    if not isinstance(session_id, str) or not session_id:
        _invocation_invalid("the resume descriptor is missing a conversation id")
    if not prepared:
        _invocation_invalid("the prepared Antigravity command is empty")
    reject_antigravity_ownership_flags(prepared)

    argv = list(prepared)
    marker_indexes: list[int] = []
    end_of_options: Optional[int] = None
    for index, item in enumerate(argv):
        if item == "--":
            end_of_options = index
            break
        if item in _PRINT_MARKERS:
            marker_indexes.append(index)
    if len(marker_indexes) != 1:
        _invocation_invalid("the Antigravity print marker is missing or ambiguous")
    insert_at = marker_indexes[0]
    if end_of_options is not None and end_of_options < insert_at:
        _invocation_invalid("the Antigravity print marker is after end-of-options")
    return tuple(argv[:insert_at] + ["--conversation", session_id] + argv[insert_at:])


def _invocation_invalid(message: str) -> NoReturn:
    raise SandboxFailure(
        "outer_sandbox_inner_command_invalid",
        message,
        remediation=(
            "Repair the Antigravity print-mode command so -p/--print/--prompt is unique.",
        ),
    )
