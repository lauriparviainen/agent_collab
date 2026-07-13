"""Install or uninstall the review skills from a source checkout.

Skill installation is deliberately separate from the agent-collab runtime
installer: it writes into another agent client's user configuration. Managed
state lets later installs upgrade unchanged copies and makes uninstall refuse
to delete user-modified files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .cli_output import error, info, ok, step
from .paths import AgentCollabHome, atomic_write_private_text


class SkillInstallError(RuntimeError):
    pass


SKILL_NAMES = (
    "agent-collab-solo-review",
    "agent-collab-dual-review",
)
CLIENT_SKILL_DIRS = {
    "claude": Path(".claude/skills"),
    "codex": Path(".agents/skills"),
    "antigravity": Path(".gemini/config/skills"),
    "grok": Path(".grok/skills"),
}
STATE_SCHEMA = 1
STATE_NAME = "skill-installs.json"


def install_skills(
    *,
    repo_root: Path,
    clients: Sequence[str],
    user_home: Optional[Path] = None,
    state_path: Optional[Path] = None,
) -> None:
    """Install or upgrade both review skills for the selected clients."""

    repo_root = repo_root.expanduser().resolve()
    home = (user_home or Path.home()).expanduser().resolve()
    state_path = state_path or (AgentCollabHome.resolve().root / STATE_NAME)
    selected = _expand_clients(clients)
    sources = _skill_sources(repo_root)
    source_fingerprints = {name: _fingerprint(path) for name, path in sources.items()}
    state = _read_state(state_path)
    installs = state["installs"]
    actions: List[Dict[str, Any]] = []

    # Check every destination before writing any of them. Expected conflicts
    # therefore fail before the first write; unexpected I/O failures can still
    # leave an already completed per-directory replacement in place.
    for client in selected:
        for name, source in sources.items():
            destination = home / CLIENT_SKILL_DIRS[client] / name
            key = _state_key(client, name)
            recorded = installs.get(key)
            current = _existing_fingerprint(destination)
            if recorded is None:
                if current is not None and current != source_fingerprints[name]:
                    raise SkillInstallError(_conflict_message(destination))
                action = "adopt" if current is not None else "install"
            else:
                expected = _recorded_fingerprint(recorded, key)
                if current is not None and current != expected:
                    raise SkillInstallError(
                        f"managed skill has local changes: {destination}; "
                        "move or remove it before installing again"
                    )
                if current is None:
                    action = "install"
                elif current == source_fingerprints[name]:
                    action = "current"
                else:
                    action = "update"
            actions.append(
                {
                    "action": action,
                    "client": client,
                    "name": name,
                    "source": source,
                    "destination": destination,
                    "fingerprint": source_fingerprints[name],
                }
            )

    step(f"Installing review skills for {', '.join(selected)}")
    for item in actions:
        action = item["action"]
        destination = item["destination"]
        if action in {"install", "update"}:
            _replace_directory(item["source"], destination)
        installs[_state_key(item["client"], item["name"])] = {
            "client": item["client"],
            "skill": item["name"],
            "fingerprint": item["fingerprint"],
        }
        if action == "current":
            info(f"Already current: {destination}")
        elif action == "adopt":
            ok(f"Now managed: {destination}")
        elif action == "update":
            ok(f"Updated: {destination}")
        else:
            ok(f"Installed: {destination}")
    _write_state(state_path, state)
    ok("Review skill installation complete")


def uninstall_skills(
    *,
    clients: Sequence[str],
    user_home: Optional[Path] = None,
    state_path: Optional[Path] = None,
) -> None:
    """Remove unchanged review skills previously managed by this command."""

    home = (user_home or Path.home()).expanduser().resolve()
    state_path = state_path or (AgentCollabHome.resolve().root / STATE_NAME)
    selected = _expand_clients(clients)
    state = _read_state(state_path)
    installs = state["installs"]
    actions: List[Dict[str, Any]] = []

    for client in selected:
        for name in SKILL_NAMES:
            destination = home / CLIENT_SKILL_DIRS[client] / name
            key = _state_key(client, name)
            recorded = installs.get(key)
            current = _existing_fingerprint(destination)
            if recorded is None:
                actions.append(
                    {
                        "action": "unmanaged" if current is not None else "absent",
                        "key": key,
                        "destination": destination,
                    }
                )
                continue
            expected = _recorded_fingerprint(recorded, key)
            if current is not None and current != expected:
                raise SkillInstallError(
                    f"managed skill has local changes: {destination}; "
                    "leaving all selected skills in place"
                )
            actions.append(
                {
                    "action": "remove" if current is not None else "missing",
                    "key": key,
                    "destination": destination,
                }
            )

    step(f"Uninstalling review skills for {', '.join(selected)}")
    for item in actions:
        action = item["action"]
        destination = item["destination"]
        if action == "remove":
            shutil.rmtree(destination)
            installs.pop(item["key"], None)
            ok(f"Removed: {destination}")
        elif action == "missing":
            installs.pop(item["key"], None)
            info(f"Already absent: {destination}")
        elif action == "unmanaged":
            info(f"Left unmanaged skill in place: {destination}")
        else:
            info(f"Not installed: {destination}")
    _write_state(state_path, state)
    ok("Review skill uninstall complete")


def _expand_clients(clients: Sequence[str]) -> List[str]:
    if not clients:
        return list(CLIENT_SKILL_DIRS)
    result: List[str] = []
    for client in clients:
        if client not in CLIENT_SKILL_DIRS:
            raise SkillInstallError(f"unknown skill client: {client}")
        if client not in result:
            result.append(client)
    return result


def _skill_sources(repo_root: Path) -> Mapping[str, Path]:
    sources = {name: repo_root / "skills" / name for name in SKILL_NAMES}
    for path in sources.values():
        if not path.is_dir() or path.is_symlink():
            raise SkillInstallError(f"review skill source is missing or invalid: {path}")
        if not (path / "SKILL.md").is_file():
            raise SkillInstallError(f"review skill source has no SKILL.md: {path}")
    return sources


def _existing_fingerprint(path: Path) -> Optional[str]:
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_dir():
        raise SkillInstallError(_conflict_message(path))
    return _fingerprint(path)


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(b"1\0" if path.stat().st_mode & 0o111 else b"0\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise SkillInstallError(f"skill contains an unsupported file type: {path}")
    return digest.hexdigest()


def _replace_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    staged = temporary_root / "new"
    previous = temporary_root / "previous"
    try:
        shutil.copytree(source, staged, symlinks=True)
        had_previous = os.path.lexists(destination)
        if had_previous:
            os.replace(destination, previous)
        try:
            os.replace(staged, destination)
        except Exception:
            if had_previous and not os.path.lexists(destination):
                os.replace(previous, destination)
            raise
    except OSError as exc:
        raise SkillInstallError(f"could not install skill at {destination}: {exc}") from exc
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _read_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": STATE_SCHEMA, "installs": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillInstallError(f"could not read managed skill state {path}: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != STATE_SCHEMA
        or not isinstance(raw.get("installs"), dict)
    ):
        raise SkillInstallError(f"managed skill state has an unsupported format: {path}")
    return raw


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    try:
        atomic_write_private_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise SkillInstallError(f"could not write managed skill state {path}: {exc}") from exc


def _recorded_fingerprint(record: Any, key: str) -> str:
    if not isinstance(record, dict) or not isinstance(record.get("fingerprint"), str):
        raise SkillInstallError(f"managed skill state contains an invalid record: {key}")
    return record["fingerprint"]


def _state_key(client: str, skill: str) -> str:
    return f"{client}/{skill}"


def _conflict_message(path: Path) -> str:
    return f"an unmanaged or different skill already exists at {path}; move or remove it first"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="./agent_collab.sh skills",
        description="Install or uninstall the agent-collab review skills.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "uninstall"):
        subparser = actions.add_parser(action)
        subparser.add_argument(
            "clients",
            nargs="*",
            choices=list(CLIENT_SKILL_DIRS),
            metavar="CLIENT",
            help="claude, codex, antigravity, or grok; omit to manage all clients",
        )
        subparser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            if args.repo_root is None:
                raise SkillInstallError("skill install must run through ./agent_collab.sh")
            install_skills(repo_root=args.repo_root, clients=args.clients)
        else:
            uninstall_skills(clients=args.clients)
        return 0
    except SkillInstallError as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
