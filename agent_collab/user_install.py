"""Install the source checkout into a durable user venv and expose its CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import List, Optional


class UserInstallError(RuntimeError):
    pass


def install_user_command(
    *,
    repo_root: Path,
    venv: Path,
    bin_dir: Path,
    editable: bool = False,
    force: bool = False,
    bootstrap_python: Optional[Path] = None,
) -> Path:
    repo_root = repo_root.expanduser().resolve()
    venv = venv.expanduser().resolve()
    bin_dir = bin_dir.expanduser().resolve()
    bootstrap_python = (bootstrap_python or Path(sys.executable)).expanduser().resolve()
    venv_python = venv / "bin" / "python"
    if not venv_python.exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        _run_checked([str(bootstrap_python), "-m", "venv", str(venv)])
    if not venv_python.exists():
        raise UserInstallError(f"venv creation did not produce an interpreter: {venv_python}")

    install_args = [str(venv_python), "-m", "pip", "install"]
    if editable:
        install_args.append("--editable")
    install_args.append(str(repo_root))
    _run_checked(install_args)

    entrypoint = venv / "bin" / "agent-collab"
    if not entrypoint.exists():
        raise UserInstallError(f"package installation did not create {entrypoint}")
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "agent-collab"
    _install_link(link, entrypoint, force=force)
    return link


def _install_link(link: Path, target: Path, *, force: bool) -> None:
    existing = _link_target(link)
    if existing is not None and existing == target.resolve():
        return
    if os.path.lexists(link) and not force:
        raise UserInstallError(
            f"refusing to replace existing command: {link}; remove it or pass --force"
        )
    fd, temporary = tempfile.mkstemp(prefix=f".{link.name}.", dir=str(link.parent))
    os.close(fd)
    temporary_path = Path(temporary)
    temporary_path.unlink()
    try:
        temporary_path.symlink_to(target)
        os.replace(temporary_path, link)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _link_target(path: Path) -> Optional[Path]:
    if not path.is_symlink():
        return None
    raw = path.readlink()
    return (path.parent / raw).resolve() if not raw.is_absolute() else raw.resolve()


def _run_checked(argv: List[str]) -> None:
    try:
        subprocess.run(argv, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UserInstallError(f"command failed: {' '.join(argv)}") from exc


def _path_contains(directory: Path) -> bool:
    for item in os.environ.get("PATH", "").split(os.pathsep):
        if not item:
            continue
        try:
            if Path(item).expanduser().resolve() == directory:
                return True
        except OSError:
            continue
    return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="./agent_collab.sh install",
        description="Install agent-collab into a durable user environment.",
    )
    parser.add_argument("--repo-root", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--venv", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path(os.environ.get("AGENT_COLLAB_BIN_DIR", "~/.local/bin")),
        help="Directory in which to expose the agent-collab command.",
    )
    parser.add_argument("--editable", action="store_true", help="Install the checkout editable.")
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing user command link or file."
    )
    args = parser.parse_args(argv)
    try:
        link = install_user_command(
            repo_root=args.repo_root,
            venv=args.venv,
            bin_dir=args.bin_dir,
            editable=args.editable,
            force=args.force,
        )
    except UserInstallError as exc:
        print(f"ERROR   {exc}", file=sys.stderr)
        return 1
    print(f"installed agent-collab command: {link}")
    if not _path_contains(link.parent):
        print(
            f"note: {link.parent} is not on PATH; add it to PATH to use agent-collab in new shells",
            file=sys.stderr,
        )
    print("try: agent-collab --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
