"""Install-time write-back: migrate the user config file on disk.

Write-back is the deliberate exception to the lazy in-memory layer — install
calls it to bring the file on disk forward while preserving user comments and
formatting. Shape-changing migrations implement a comment-preserving
counterpart here (the pre-v8 structural rewrite; the v10 antigravity model
renames); stamp-only steps keep a tomlkit-free regex fallback so a bootstrap
Python without tomlkit can still finish install.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .base import CURRENT_CONFIG_SCHEMA, ConfigMigrationError
from .v9_to_v10 import _apply_antigravity_model_renames


@dataclass(frozen=True)
class UserConfigWriteBack:
    """Result of ``migrate_user_config_file``."""

    status: str  # "absent" | "current" | "migrated"
    path: Path
    backup_path: Optional[Path] = None
    previous_version: Optional[int] = None
    permissions_fixed: bool = False


def migrate_user_config_file(path: Path) -> UserConfigWriteBack:
    """Migrate the user config file on disk to ``CURRENT_CONFIG_SCHEMA``.

    Write-back is the install-time convenience on top of the lazy in-memory
    layer, never a replacement for it. The original file is backed up to
    ``<name>.bak`` first, and user comments and formatting are preserved: an
    existing ``schema_version`` value is updated through tomlkit, a missing
    one is prepended as text. Shape-changing migrations implement a
    comment-preserving counterpart here (the pre-v8 structural rewrite; the
    v10 antigravity model renames) — a future one must do the same before it
    ships.
    """

    path = path.expanduser()
    if not path.exists():
        return UserConfigWriteBack(status="absent", path=path)
    # Operate on the symlink target so a dotfile-managed config keeps its
    # link: os.replace on the symlink path itself would sever it.
    path = path.resolve()
    from . import migrate_config_data
    from ..config import load_toml_file
    from ..paths import atomic_write_private_text

    text = path.read_text(encoding="utf-8")
    data = load_toml_file(path)
    migrated = migrate_config_data(data, source=str(path), scope="user")
    permissions_fixed = _tighten_private_permissions(path)
    raw_version = data.get("schema_version", 1)
    if raw_version == CURRENT_CONFIG_SCHEMA:
        return UserConfigWriteBack(status="current", path=path, permissions_fixed=permissions_fixed)

    backup_path = path.with_name(path.name + ".bak")
    atomic_write_private_text(backup_path, text)
    if int(raw_version) < 8:
        # The structural rewrite regenerates agents/backends/workflows from
        # the fully migrated data, so the v10 model renames ride along for
        # those sections. Sections it deliberately leaves as original text
        # (e.g. a hand-added [usage_windows]) still need the rename pass —
        # otherwise a display-name model would be frozen on disk under the
        # freshly stamped current version and never migrated again.
        new_text = _apply_model_renames_to_rendered(
            _rewrite_backend_first(text, migrated, path), path
        )
    else:
        new_text = _rewrite_model_renames_and_stamp(text, data, path)
    atomic_write_private_text(path, new_text)
    return UserConfigWriteBack(
        status="migrated",
        path=path,
        backup_path=backup_path,
        previous_version=int(raw_version),
        permissions_fixed=permissions_fixed,
    )


def _tighten_private_permissions(path: Path) -> bool:
    """Chmod a group/world-readable user config to 0600.

    The file can hold the daemon bearer token; a restored backup or copy made
    with a loose umask must not stay world-readable just because its schema
    is already current.
    """

    import os
    import stat

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            return True
    except OSError:
        pass
    return False


def _rewrite_backend_first(text: str, migrated: Mapping[str, Any], path: Path) -> str:
    """Rewrite a pre-v8 user config to the backend-first shape.

    Sections the migration does not touch (daemon, sessions, workdir) keep
    their comments and formatting; the agents/backends/workflows sections are
    regenerated from the migrated data. This structural rewrite requires
    tomlkit — install fails with a clear error rather than guessing.
    """

    try:
        import tomlkit
    except ImportError:
        raise ConfigMigrationError(
            f"{path}: migrating to the backend-first schema (v{CURRENT_CONFIG_SCHEMA}) requires "
            "tomlkit; install it with: pip install tomlkit"
        ) from None
    document = tomlkit.parse(text)
    for key in ("agents", "backends", "workflows"):
        if key in document:
            del document[key]
    had_version = "schema_version" in document
    if had_version:
        document["schema_version"] = CURRENT_CONFIG_SCHEMA
    for key in ("agents", "backends", "workflows"):
        value = migrated.get(key)
        if value:
            document[key] = tomlkit.item(value)
    rendered = tomlkit.dumps(document)
    if not had_version:
        # Appending a top-level scalar after tables would parse inside the
        # last table; a missing version is prepended as text instead.
        rendered = f"schema_version = {CURRENT_CONFIG_SCHEMA}\n\n{rendered}"
    return rendered


def _apply_model_renames_to_rendered(text: str, path: Path) -> str:
    """Apply pending antigravity model renames to already-rendered config text.

    Post-pass for the pre-v8 structural rewrite, whose output can still carry
    display-name models in sections it keeps as original text. Returns the
    text unchanged when nothing needs renaming. Callers reach here only via
    ``_rewrite_backend_first``, which already required tomlkit.
    """

    from ..config import load_toml_text

    data = load_toml_text(text, source=str(path))
    probe = copy.deepcopy(dict(data))
    if not _apply_antigravity_model_renames(probe):
        return text
    try:
        import tomlkit
    except ImportError:
        raise ConfigMigrationError(
            f"{path}: migrating antigravity model names to schema v{CURRENT_CONFIG_SCHEMA} "
            "requires tomlkit; install it with: pip install tomlkit"
        ) from None
    document = tomlkit.parse(text)
    _apply_antigravity_model_renames(document)
    return tomlkit.dumps(document)


def _rewrite_model_renames_and_stamp(text: str, data: Mapping[str, Any], path: Path) -> str:
    """Write back a v8/v9 config: apply the v10 model renames (if any values
    need them) and stamp the schema version, preserving comments/formatting.

    A config with no display-name model values reduces to the plain version
    stamp (keeping the tomlkit-free bootstrap fallback usable); one that needs
    renames is a shape change and requires tomlkit, per the write-back
    contract in ``migrate_user_config_file``.
    """

    probe = copy.deepcopy(dict(data))
    if not _apply_antigravity_model_renames(probe):
        if "schema_version" in data:
            return _stamp_schema_version(text, path)
        return f"schema_version = {CURRENT_CONFIG_SCHEMA}\n\n{text}"
    try:
        import tomlkit
    except ImportError:
        raise ConfigMigrationError(
            f"{path}: migrating antigravity model names to schema v{CURRENT_CONFIG_SCHEMA} "
            "requires tomlkit; install it with: pip install tomlkit"
        ) from None
    document = tomlkit.parse(text)
    _apply_antigravity_model_renames(document)
    had_version = "schema_version" in document
    if had_version:
        document["schema_version"] = CURRENT_CONFIG_SCHEMA
    rendered = tomlkit.dumps(document)
    if not had_version:
        # Appending a top-level scalar after tables would parse inside the
        # last table; a missing version is prepended as text instead.
        rendered = f"schema_version = {CURRENT_CONFIG_SCHEMA}\n\n{rendered}"
    return rendered


def _stamp_schema_version(text: str, path: Path) -> str:
    """Update an existing ``schema_version`` value, preserving everything else.

    tomlkit is the primary, fully style-preserving writer. The regex fallback
    is exactly equivalent whenever stamping is the only change needed for this
    file — it lets a bootstrap Python without tomlkit (fresh machine,
    dotfile-carried old config) still complete install. Shape-changing paths
    (``_rewrite_backend_first``, ``_rewrite_model_renames_and_stamp`` with
    pending renames) never route here; they require tomlkit outright.
    """

    try:
        import tomlkit
    except ImportError:
        import re

        from ..config import load_toml_text

        new_text, count = re.subn(
            r"(?m)^(\s*schema_version\s*=\s*)\d+",
            lambda match: f"{match.group(1)}{CURRENT_CONFIG_SCHEMA}",
            text,
            count=1,
        )
        # The single replacement may have hit a lookalike line inside a
        # multi-line string instead of the real top-level key; reparsing
        # proves the stamp landed (one replacement cannot do both).
        if (
            count != 1
            or load_toml_text(new_text, source=str(path)).get("schema_version")
            != CURRENT_CONFIG_SCHEMA
        ):
            raise ConfigMigrationError(
                f"{path}: could not safely update schema_version without tomlkit; "
                "install it with: pip install tomlkit"
            )
        return new_text
    document = tomlkit.parse(text)
    document["schema_version"] = CURRENT_CONFIG_SCHEMA
    return tomlkit.dumps(document)
