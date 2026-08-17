"""Codex CLI ordinary-argv audit and strict ``exec resume`` finalizer."""

from __future__ import annotations

from typing import Any, List, Mapping, NoReturn, Optional, Sequence, Tuple

from ...sandbox.specs import SandboxFailure
from ..common.cli import reject_cli_ownership_flags

# ``resume`` as a bare token is the ownership subcommand; ``--resume`` is not
# the installed form but is still a user-configured selector if present.
CLI_OWNERSHIP_FLAGS = ("--resume", "resume")

_ROOT_FLAGS_WITH_VALUE = (
    "--profile",
    "--sandbox",
    "--approval-policy",
    "-C",
    "--cd",
    "--workdir",
)
_ROOT_FLAGS_BOOL = ("--search",)
_RESUME_FLAGS_WITH_VALUE = ("--model", "--config", "-c", "--reasoning-effort")
_RESUME_FLAGS_BOOL = ("--json",)


def reject_codex_ownership_flags(command: Sequence[str]) -> None:
    """Reject user-configured ``resume`` / ``--resume`` under either policy."""

    reject_cli_ownership_flags(command, CLI_OWNERSHIP_FLAGS)


def finalize_codex_cli_invocation(
    prepared: Sequence[str],
    descriptor: Optional[Mapping[str, Any]],
) -> Tuple[str, ...]:
    """Rewrite to ``codex [root] exec resume --json [opts] <thread-id>``."""

    if descriptor is None:
        return tuple(prepared)
    thread_id = descriptor.get("provider_session_id")
    if not isinstance(thread_id, str) or not thread_id:
        _invocation_invalid("the resume descriptor is missing a thread id")
    if not prepared:
        _invocation_invalid("the prepared Codex command is empty")
    reject_codex_ownership_flags(prepared)
    root, resume_opts = partition_codex_exec(prepared)
    return tuple(root + ["exec", "resume"] + resume_opts + [thread_id])


def partition_codex_exec(command: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split a normalized Codex argv into root options and ``exec resume`` options.

    Root-only profile, sandbox, approval, workdir, and search stay before
    ``exec``. Unknown or ambiguous tokens fail instead of being guessed.
    """

    if not command:
        _invocation_invalid("the prepared Codex command is empty")
    exec_at = None
    for index, item in enumerate(command):
        if item == "exec":
            if exec_at is not None:
                _invocation_invalid("the Codex exec subcommand is ambiguous")
            exec_at = index
    if exec_at is None:
        _invocation_invalid("the Codex exec subcommand is missing")
    if exec_at == 0:
        _invocation_invalid("the Codex executable is missing")

    root = list(command[:exec_at])
    after = list(command[exec_at + 1 :])
    if after and after[0] == "resume":
        _invocation_invalid("the ordinary Codex command already selects resume")

    resume_opts: List[str] = []
    index = 0
    while index < len(after):
        item = after[index]
        if item == "--":
            _invocation_invalid("Codex end-of-options is not allowed before the prompt")
        name, attached = _split_flag(item)
        if name in _ROOT_FLAGS_BOOL:
            if attached is not None:
                _invocation_invalid(f"Codex flag {name} does not take a value")
            root.append(item)
            index += 1
            continue
        if name in _ROOT_FLAGS_WITH_VALUE:
            value, index = _take_value(after, index, name, attached)
            root.extend([name, value] if attached is None else [item])
            continue
        if name in _RESUME_FLAGS_BOOL:
            if attached is not None:
                _invocation_invalid(f"Codex flag {name} does not take a value")
            resume_opts.append(item)
            index += 1
            continue
        if name in _RESUME_FLAGS_WITH_VALUE:
            value, index = _take_value(after, index, name, attached)
            resume_opts.extend([name, value] if attached is None else [item])
            continue
        if item.startswith("-"):
            _invocation_invalid(f"unsupported Codex token {item!r} cannot be placed for resume")
        _invocation_invalid(f"unexpected Codex positional {item!r} before the prompt")
    return root, resume_opts


def _split_flag(item: str) -> Tuple[str, Optional[str]]:
    if item.startswith("--") and "=" in item:
        name, value = item.split("=", 1)
        return name, value
    if item.startswith("-") and not item.startswith("--") and "=" in item:
        name, value = item.split("=", 1)
        return name, value
    return item, None


def _take_value(
    tokens: Sequence[str],
    index: int,
    name: str,
    attached: Optional[str],
) -> Tuple[str, int]:
    if attached is not None:
        if attached == "":
            _invocation_invalid(f"Codex flag {name} is missing a value")
        return attached, index + 1
    if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
        _invocation_invalid(f"Codex flag {name} is missing a value")
    return tokens[index + 1], index + 2


def _invocation_invalid(message: str) -> NoReturn:
    raise SandboxFailure(
        "outer_sandbox_inner_command_invalid",
        message,
        remediation=(
            "Repair the Codex exec command so resume can place root options "
            "before exec and keep --json after exec resume.",
        ),
    )
