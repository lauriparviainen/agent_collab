#!/usr/bin/env python3
"""Isolated stdlib-only exec gate used as Bubblewrap's first inner command."""

from __future__ import annotations

import json
import os
import struct
import sys

PROTOCOL_VERSION = 1
FRAME_LIMIT = 16 * 1024
ROLE_NAMES = (
    "proof",
    "worker",
    "provider_stdin",
    "provider_stdout",
    "provider_stderr",
)


class ProtocolError(RuntimeError):
    pass


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON field")
        result[key] = value
    return result


def _recv_exact(channel: int, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        value = os.read(channel, length - len(chunks))
        if not value:
            raise ProtocolError("unexpected proof EOF")
        chunks.extend(value)
    return bytes(chunks)


def _recv_frame(channel: int) -> dict[str, object]:
    length = struct.unpack(">I", _recv_exact(channel, 4))[0]
    if length == 0 or length > FRAME_LIMIT:
        raise ProtocolError("invalid proof frame length")
    raw = _recv_exact(channel, length)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid proof JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("proof frame must be an object")
    return value


def _send_frame(channel: int, value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not raw or len(raw) > FRAME_LIMIT:
        raise ProtocolError("invalid outgoing proof frame")
    pending = memoryview(struct.pack(">I", len(raw)) + raw)
    while pending:
        written = os.write(channel, pending)
        pending = pending[written:]


def _parse_args(argv: list[str]) -> tuple[dict[str, int], list[str]]:
    roles: dict[str, int] = {}
    index = 0
    if argv[:2] != ["--protocol-version", "1"]:
        raise ProtocolError("unsupported or missing protocol version")
    index = 2
    options = {
        "--proof-fd": "proof",
        "--worker-fd": "worker",
        "--provider-stdin-fd": "provider_stdin",
        "--provider-stdout-fd": "provider_stdout",
        "--provider-stderr-fd": "provider_stderr",
    }
    while index < len(argv) and argv[index] != "--":
        option = argv[index]
        if option not in options or index + 1 >= len(argv):
            raise ProtocolError("unknown or incomplete bootstrap option")
        role = options[option]
        if role in roles:
            raise ProtocolError("duplicate bootstrap role")
        try:
            descriptor = int(argv[index + 1], 10)
        except ValueError as exc:
            raise ProtocolError("bootstrap descriptor is not decimal") from exc
        if descriptor <= 2 or descriptor in roles.values():
            raise ProtocolError("bootstrap descriptor roles must be distinct and greater than 2")
        roles[role] = descriptor
        index += 2
    if index >= len(argv) or argv[index] != "--":
        raise ProtocolError("missing inner-command separator")
    inner = argv[index + 1 :]
    required = {"proof", "provider_stdin", "provider_stdout", "provider_stderr"}
    if not required.issubset(roles) or not inner:
        raise ProtocolError("missing bootstrap roles or inner command")
    if not os.path.isabs(inner[0]):
        raise ProtocolError("inner executable must be absolute")
    return roles, inner


def _enumerate_fds(expected: set[int]) -> None:
    directory_fd = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY)
    try:
        observed = {int(item) for item in os.listdir(directory_fd) if item.isdigit()}
        observed.discard(directory_fd)
        # CPython's fd-based listdir duplicates the directory descriptor for
        # fdopendir. The duplicate is present in the returned snapshot but is
        # already closed when listdir returns. Exclude exactly that one
        # transient closed descriptor, never an open inherited descriptor.
        closed = []
        for descriptor in observed - expected:
            try:
                os.fstat(descriptor)
            except OSError:
                closed.append(descriptor)
        if len(closed) == 1:
            observed.discard(closed[0])
    finally:
        os.close(directory_fd)
    if observed != expected:
        raise ProtocolError("unexpected inherited file descriptor")


def _validate_nonce(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ProtocolError("invalid nonce")
    if any(char not in "0123456789abcdef" for char in value):
        raise ProtocolError("invalid nonce")
    return value


def main(argv: list[str]) -> int:
    channel: int | None = None
    stage = "parse"
    try:
        roles, inner = _parse_args(argv)
        channel = roles["proof"]
        stage = "challenge"
        challenge = _recv_frame(channel)
        if set(challenge) != {"type", "version", "nonce"}:
            raise ProtocolError("invalid challenge fields")
        if challenge["type"] != "sandbox_challenge" or challenge["version"] != 1:
            raise ProtocolError("invalid challenge")
        nonce = _validate_nonce(challenge["nonce"])
        stage = "fd_check"
        expected = {0, 1, 2, *roles.values()}
        _enumerate_fds(expected)
        stage = "hello"
        _send_frame(
            channel,
            {
                "type": "sandbox_hello",
                "version": PROTOCOL_VERSION,
                "nonce": nonce,
                "pid": os.getpid(),
                "fd_roles": dict(sorted(roles.items())),
            },
        )
        stage = "ack"
        acknowledgement = _recv_frame(channel)
        if set(acknowledgement) != {"type", "version", "nonce", "verified"}:
            raise ProtocolError("invalid acknowledgement fields")
        if (
            acknowledgement["type"] != "sandbox_ack"
            or acknowledgement["version"] != 1
            or acknowledgement["nonce"] != nonce
            or acknowledgement["verified"] is not True
        ):
            raise ProtocolError("invalid acknowledgement")
        nonce = ""
        stage = "stdio_transfer"
        for role, target in (
            ("provider_stdin", 0),
            ("provider_stdout", 1),
            ("provider_stderr", 2),
        ):
            os.dup2(roles[role], target, inheritable=True)
        keep = {0, 1, 2}
        if "worker" in roles:
            keep.add(roles["worker"])
        for descriptor in roles.values():
            if descriptor not in keep:
                os.close(descriptor)
        stage = "final_fd_check"
        final_dir_fd = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY)
        try:
            final = {int(item) for item in os.listdir(final_dir_fd) if item.isdigit()}
            final.discard(final_dir_fd)
            closed = []
            for descriptor in final - keep:
                try:
                    os.fstat(descriptor)
                except OSError:
                    closed.append(descriptor)
            if len(closed) == 1:
                final.discard(closed[0])
        finally:
            os.close(final_dir_fd)
        if final != keep:
            raise ProtocolError("unexpected descriptor after stdio transfer")
        stage = "exec"
        os.execve(inner[0], inner, dict(os.environ))
    except BaseException:
        try:
            os.write(2, f"bootstrap failed at {stage}\n".encode("ascii"))
        except BaseException:
            pass
        if channel is not None:
            try:
                _send_frame(
                    channel,
                    {
                        "type": "sandbox_error",
                        "version": PROTOCOL_VERSION,
                        "code": "outer_sandbox_bootstrap_failed",
                        "stage": stage,
                    },
                )
            except BaseException:
                pass
        return 125
    return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
