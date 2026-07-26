"""Strict path, Git-protection, mount-table, and alias-audit handling."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Dict, Iterator, Optional, Sequence, Tuple

from .specs import (
    CreationPolicy,
    GitRole,
    PathAccess,
    PathOrigin,
    Persistence,
    SandboxFailure,
    StateRootSpec,
)

GIT_MINIMUM_VERSION = (2, 31, 0)
GIT_OUTPUT_LIMIT = 64 * 1024
ALTERNATES_FILE_LIMIT = 1024 * 1024
SUPPORTED_FILESYSTEMS = frozenset({"ext4", "xfs", "tmpfs"})
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class PinnedIdentity:
    device: int
    inode: int
    file_type: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "PinnedIdentity":
        return cls(value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


@dataclass(frozen=True)
class ResolvedSandboxPath:
    configured: str
    destination: Path
    source: Path
    access: PathAccess
    origin: PathOrigin
    persistence: Persistence
    creation: CreationPolicy
    created: bool
    label: str
    identity: PinnedIdentity


@dataclass(frozen=True)
class GitProvenance:
    referring_store: Optional[Path] = None
    line_ordinal: Optional[int] = None
    duplicate: bool = False
    cycle: bool = False


@dataclass(frozen=True)
class GitProtectionRecord:
    destination: Path
    role: GitRole
    provenance: Tuple[GitProvenance, ...]
    identity: PinnedIdentity


@dataclass(frozen=True)
class MountOperation:
    source: Path
    destination: Path
    access: PathAccess
    persistence: Persistence
    origins: Tuple[PathOrigin, ...]
    labels: Tuple[str, ...]
    covered_paths: Tuple[Path, ...] = ()
    git_roles: Tuple[GitRole, ...] = ()
    git_provenance: Tuple[GitProvenance, ...] = ()
    identity: Optional[PinnedIdentity] = None


@dataclass(frozen=True)
class GitDiscovery:
    kind: str
    records: Tuple[GitProtectionRecord, ...]


@dataclass(frozen=True)
class MountInfoEntry:
    mount_id: int
    parent_id: int
    major_minor: str
    root: Path
    mountpoint: Path
    mount_options: frozenset[str]
    filesystem_type: str
    source: str
    super_options: frozenset[str]

    @property
    def writable(self) -> bool:
        # Per-mount VFS flags are authoritative for a bind mount. A read-only
        # bind commonly retains ``rw`` in its backing superblock options.
        return "rw" in self.mount_options


def component_contains(parent: Path, child: Path) -> bool:
    """Component-boundary containment, including equality."""

    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def path_depth(path: Path) -> int:
    return len(path.parts)


def resolve_workspace(path: Path) -> ResolvedSandboxPath:
    configured = str(path)
    source = path.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            "the resolved session workspace is not a directory",
        )
    value = source.stat()
    return ResolvedSandboxPath(
        configured=configured,
        destination=source,
        source=source,
        access=PathAccess.READ_ONLY,
        origin=PathOrigin.WORKSPACE,
        persistence=Persistence.HOST,
        creation=CreationPolicy.MUST_EXIST,
        created=False,
        label="Workspace",
        identity=PinnedIdentity.from_stat(value),
    )


def resolve_effective_cwd(workspace: ResolvedSandboxPath, configured_cwd: Optional[str]) -> Path:
    if not configured_cwd:
        return workspace.destination
    candidate = Path(configured_cwd).expanduser()
    if not candidate.is_absolute():
        candidate = workspace.destination / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            "the configured agent cwd does not exist",
        ) from exc
    if not resolved.is_dir():
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            "the configured agent cwd is not a directory",
        )
    return resolved


def resolve_state_root(
    spec: StateRootSpec, *, daemon_uid: Optional[int] = None
) -> ResolvedSandboxPath:
    uid = os.getuid() if daemon_uid is None else daemon_uid
    configured = str(spec.destination)
    candidate = spec.destination.expanduser()
    if not candidate.is_absolute():
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            f"{spec.label} must be an absolute path",
        )
    created = False
    if spec.creation is CreationPolicy.CREATE_PRIVATE_DIRECTORY and not candidate.exists():
        create_private_directory(candidate, uid=uid)
        created = True
    try:
        source = candidate.resolve(strict=True)
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_path_missing",
            f"{spec.label} must already exist",
        ) from exc
    if source != candidate.absolute():
        raise SandboxFailure(
            "outer_sandbox_path_symlink",
            f"{spec.label} may not be a symlink or contain a symlink component",
        )
    _verify_no_symlink_components(source)
    value = os.stat(source, follow_symlinks=False)
    if not stat.S_ISDIR(value.st_mode):
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            f"{spec.label} must be a directory",
        )
    if (
        spec.access is PathAccess.WRITABLE
        or spec.creation is CreationPolicy.CREATE_PRIVATE_DIRECTORY
    ):
        if value.st_uid != uid:
            raise SandboxFailure(
                "outer_sandbox_path_ownership",
                f"{spec.label} must be owned by the daemon uid",
            )
        if value.st_mode & 0o022:
            raise SandboxFailure(
                "outer_sandbox_path_permissions",
                f"{spec.label} must not be group/world writable",
            )
    return ResolvedSandboxPath(
        configured=configured,
        destination=source,
        source=source,
        access=spec.access,
        origin=spec.origin,
        persistence=spec.persistence,
        creation=spec.creation,
        created=created,
        label=spec.label,
        identity=PinnedIdentity.from_stat(value),
    )


def create_private_directory(path: Path, *, uid: Optional[int] = None) -> None:
    """Create a private directory without traversing a symlink component."""

    owner = os.getuid() if uid is None else uid
    absolute = path.absolute()
    missing: list[Path] = []
    cursor = absolute
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if not cursor.exists() or not cursor.is_dir():
        raise SandboxFailure(
            "outer_sandbox_path_invalid",
            "private directory has no existing trusted parent",
        )
    _verify_no_symlink_components(cursor)
    parent_stat = os.stat(cursor, follow_symlinks=False)
    if parent_stat.st_uid != owner:
        raise SandboxFailure(
            "outer_sandbox_path_ownership",
            "private directory parent is not owned by the daemon uid",
        )
    for item in reversed(missing):
        try:
            os.mkdir(item, 0o700)
        except FileExistsError:
            pass
        value = os.stat(item, follow_symlinks=False)
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != owner:
            raise SandboxFailure(
                "outer_sandbox_path_ownership",
                "created private directory failed ownership validation",
            )
        os.chmod(item, 0o700, follow_symlinks=False)


def _verify_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            value = os.lstat(current)
        except OSError as exc:
            raise SandboxFailure(
                "outer_sandbox_path_missing",
                "declared path disappeared during validation",
            ) from exc
        if stat.S_ISLNK(value.st_mode):
            raise SandboxFailure(
                "outer_sandbox_path_symlink",
                "writable paths may not contain symlink components",
            )


def validate_writable_breadth(
    path: ResolvedSandboxPath,
    workspace: ResolvedSandboxPath,
    protected_external: Sequence[Path] = (),
) -> None:
    if path.access is not PathAccess.WRITABLE:
        return
    home = Path.home().resolve()
    destination = path.destination
    if destination == Path("/") or destination == home:
        raise SandboxFailure(
            "outer_sandbox_writable_too_broad",
            f"{path.label} may not make the filesystem root or daemon home writable",
        )
    if component_contains(workspace.destination, destination) or component_contains(
        destination, workspace.destination
    ):
        raise SandboxFailure(
            "outer_sandbox_writable_workspace_overlap",
            f"{path.label} overlaps the protected workspace",
        )
    for anchor in protected_external:
        if component_contains(anchor, destination):
            raise SandboxFailure(
                "outer_sandbox_writable_git_overlap",
                f"{path.label} is equal to or below protected Git metadata",
            )


def discover_session_git(workspace: ResolvedSandboxPath) -> GitDiscovery:
    root = workspace.source
    dotgit = root / ".git"
    is_worktree_candidate = dotgit.exists() or dotgit.is_symlink()
    is_bare_candidate = (
        (root / "HEAD").is_file() and (root / "objects").is_dir() and (root / "refs").is_dir()
    )
    if not is_worktree_candidate and not is_bare_candidate:
        return GitDiscovery("not_git", ())

    git = _resolve_git()
    git_dir, common_dir, is_bare = _git_validate(git, root)
    if is_bare_candidate:
        if not is_bare or git_dir != root or common_dir != root:
            raise SandboxFailure(
                "outer_sandbox_git_discovery_invalid",
                "Git disagreed with the strictly parsed bare repository layout",
            )
        kind = "bare"
    else:
        parsed_git_dir = _parse_git_dir(root, dotgit)
        parsed_common_dir = _parse_common_dir(parsed_git_dir)
        if is_bare or git_dir != parsed_git_dir or common_dir != parsed_common_dir:
            raise SandboxFailure(
                "outer_sandbox_git_discovery_invalid",
                "Git disagreed with the strictly parsed worktree metadata layout",
            )
        kind = "worktree"

    records: list[GitProtectionRecord] = [
        _git_record(git_dir, GitRole.WORKTREE_GIT_DIR),
        _git_record(common_dir, GitRole.COMMON_GIT_DIR),
    ]
    primary = _strict_directory(common_dir / "objects")
    records.append(_git_record(primary, GitRole.PRIMARY_OBJECT_STORE))

    queue = deque([primary])
    parsed: set[Path] = set()
    discovered: set[Path] = {primary}
    while queue:
        object_store = queue.popleft()
        if object_store in parsed:
            continue
        parsed.add(object_store)
        alternate_file = object_store / "info" / "alternates"
        if not alternate_file.exists():
            continue
        for ordinal, raw in _read_alternates(alternate_file):
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = object_store / candidate
            target = _strict_directory(candidate)
            duplicate = target in discovered
            cycle = target in parsed
            provenance = GitProvenance(
                referring_store=object_store,
                line_ordinal=ordinal,
                duplicate=duplicate,
                cycle=cycle,
            )
            records.append(
                GitProtectionRecord(
                    target,
                    GitRole.ALTERNATE_OBJECT_STORE,
                    (provenance,),
                    PinnedIdentity.from_stat(os.stat(target, follow_symlinks=False)),
                )
            )
            if not duplicate:
                discovered.add(target)
                queue.append(target)
    return GitDiscovery(kind, tuple(records))


def _resolve_git() -> str:
    import shutil

    executable = shutil.which("git")
    if executable is None:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_unavailable",
            "Git is required to validate session-root repository metadata",
            remediation=("Install a compatible Git executable.",),
        )
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_unavailable",
            "Git version validation failed",
        ) from exc
    match = re.fullmatch(r"git version (\d+)\.(\d+)(?:\.(\d+))?.*\n?", result.stdout)
    version = tuple(int(item or 0) for item in match.groups()) if match is not None else (0, 0, 0)
    if version < GIT_MINIMUM_VERSION:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_unavailable",
            "Git is older than the minimum supported sandbox discovery version",
        )
    return str(Path(executable).resolve())


def _git_environment() -> Dict[str, str]:
    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return environment


def _git_validate(executable: str, workspace: Path) -> Tuple[Path, Path, bool]:
    argv = [
        executable,
        "-C",
        str(workspace),
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
        "--is-bare-repository",
    ]
    try:
        result = subprocess.run(
            argv,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_unavailable",
            "isolated Git metadata validation could not run",
        ) from exc
    if result.returncode != 0 or len(result.stdout) > GIT_OUTPUT_LIMIT:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "isolated Git metadata validation failed",
        )
    try:
        text = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "isolated Git metadata output was not UTF-8",
        ) from exc
    lines = text.splitlines()
    if len(lines) != 3 or lines[2] not in {"true", "false"}:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "isolated Git metadata output had an unexpected shape",
        )
    paths = []
    for raw in lines[:2]:
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise SandboxFailure(
                "outer_sandbox_git_discovery_invalid",
                "Git returned a non-absolute metadata path",
            )
        paths.append(_strict_directory(candidate))
    return paths[0], paths[1], lines[2] == "true"


def _parse_git_dir(workspace: Path, dotgit: Path) -> Path:
    if dotgit.is_dir():
        return _strict_directory(dotgit)
    try:
        value = dotgit.read_bytes()
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the root .git candidate could not be parsed",
        ) from exc
    if len(value) > 4096 or b"\0" in value or not value.endswith(b"\n"):
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the root gitfile is malformed",
        )
    try:
        line = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the root gitfile is not UTF-8",
        ) from exc
    if line.count("\n") != 1 or not line.startswith("gitdir: "):
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the root gitfile has invalid grammar",
        )
    raw = line[len("gitdir: ") : -1]
    if not raw:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the root gitfile has an empty path",
        )
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return _strict_directory(candidate)


def _parse_common_dir(git_dir: Path) -> Path:
    path = git_dir / "commondir"
    if not path.exists():
        return git_dir
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the Git commondir file could not be parsed",
        ) from exc
    if len(value) > 4096 or b"\0" in value or not value.endswith(b"\n"):
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the Git commondir file is malformed",
        )
    try:
        line = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the Git commondir file is not UTF-8",
        ) from exc
    if line.count("\n") != 1 or not line[:-1]:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "the Git commondir file has invalid grammar",
        )
    candidate = Path(line[:-1])
    if not candidate.is_absolute():
        candidate = git_dir / candidate
    return _strict_directory(candidate)


def _strict_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        value = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "a required Git metadata path is missing or unresolvable",
        ) from exc
    if not stat.S_ISDIR(value.st_mode):
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "a required Git metadata path is not a directory",
        )
    return resolved


def _git_record(path: Path, role: GitRole) -> GitProtectionRecord:
    value = os.stat(path, follow_symlinks=False)
    return GitProtectionRecord(path, role, (GitProvenance(),), PinnedIdentity.from_stat(value))


def _read_alternates(path: Path) -> Iterator[Tuple[int, str]]:
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "a Git alternates file could not be read",
        ) from exc
    if len(value) > ALTERNATES_FILE_LIMIT or b"\0" in value or not value.endswith(b"\n"):
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "a Git alternates file is malformed",
        )
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SandboxFailure(
            "outer_sandbox_git_discovery_invalid",
            "a Git alternates file is not UTF-8",
        ) from exc
    for ordinal, raw in enumerate(text.splitlines(), start=1):
        if not raw or "://" in raw:
            raise SandboxFailure(
                "outer_sandbox_git_discovery_invalid",
                "a Git alternates entry is empty or is not a filesystem path",
            )
        yield ordinal, raw


def normalize_mounts(
    workspace: ResolvedSandboxPath,
    declarations: Sequence[ResolvedSandboxPath],
    git_records: Sequence[GitProtectionRecord],
) -> Tuple[MountOperation, ...]:
    """Normalize declarations and the authoritative Git coverage contract."""

    generic: Dict[Path, list[ResolvedSandboxPath]] = {}
    for declaration in declarations:
        generic.setdefault(declaration.destination, []).append(declaration)

    operations: list[MountOperation] = []
    for destination, grouped in generic.items():
        access_values = {item.access for item in grouped}
        if len(access_values) > 1:
            if any(
                item.origin in {PathOrigin.WORKSPACE, PathOrigin.GIT_METADATA} for item in grouped
            ):
                raise SandboxFailure(
                    "outer_sandbox_mount_conflict",
                    "a protected path cannot also be declared writable",
                )
            access = PathAccess.WRITABLE
        else:
            access = next(iter(access_values))
        first = sorted(grouped, key=lambda item: (item.origin.value, item.label))[0]
        operations.append(
            MountOperation(
                source=first.source,
                destination=destination,
                access=access,
                persistence=first.persistence,
                origins=tuple(
                    sorted({item.origin for item in grouped}, key=lambda item: item.value)
                ),
                labels=tuple(sorted({item.label for item in grouped})),
                identity=first.identity,
            )
        )

    external_groups: Dict[Path, list[GitProtectionRecord]] = {}
    workspace_records: list[GitProtectionRecord] = []
    for record in git_records:
        if component_contains(workspace.destination, record.destination):
            workspace_records.append(record)
        else:
            external_groups.setdefault(record.destination, []).append(record)

    selected: list[Path] = []
    coverage: Dict[Path, list[GitProtectionRecord]] = {}
    for destination in sorted(external_groups, key=lambda item: (path_depth(item), str(item))):
        anchor = next((item for item in selected if component_contains(item, destination)), None)
        if anchor is None:
            anchor = destination
            selected.append(anchor)
            coverage[anchor] = []
        coverage[anchor].extend(external_groups[destination])

    external_ops: list[MountOperation] = []
    for anchor in sorted(selected, key=lambda item: (path_depth(item), str(item))):
        records = sorted(
            coverage[anchor],
            key=lambda item: (
                list(GitRole).index(item.role),
                str(item.destination),
                tuple(
                    (
                        str(provenance.referring_store or ""),
                        provenance.line_ordinal or 0,
                    )
                    for provenance in item.provenance
                ),
            ),
        )
        external_ops.append(
            MountOperation(
                source=anchor,
                destination=anchor,
                access=PathAccess.READ_ONLY,
                persistence=Persistence.HOST,
                origins=(PathOrigin.GIT_METADATA,),
                labels=("Git metadata",),
                covered_paths=tuple(dict.fromkeys(item.destination for item in records)),
                git_roles=tuple(dict.fromkeys(item.role for item in records)),
                git_provenance=tuple(
                    provenance for item in records for provenance in item.provenance
                ),
                identity=PinnedIdentity.from_stat(os.stat(anchor, follow_symlinks=False)),
            )
        )

    workspace_records.sort(
        key=lambda item: (
            list(GitRole).index(item.role),
            str(item.destination),
        )
    )
    workspace_op = MountOperation(
        source=workspace.source,
        destination=workspace.destination,
        access=PathAccess.READ_ONLY,
        persistence=Persistence.HOST,
        origins=(
            (PathOrigin.WORKSPACE, PathOrigin.GIT_METADATA)
            if workspace_records
            else (PathOrigin.WORKSPACE,)
        ),
        labels=("Workspace",),
        covered_paths=tuple(dict.fromkeys(item.destination for item in workspace_records)),
        git_roles=tuple(dict.fromkeys(item.role for item in workspace_records)),
        git_provenance=tuple(
            provenance for item in workspace_records for provenance in item.provenance
        ),
        identity=workspace.identity,
    )

    readable = sorted(
        (item for item in operations if item.access is PathAccess.READ_ONLY),
        key=lambda item: (path_depth(item.destination), str(item.destination)),
    )
    writable = sorted(
        (item for item in operations if item.access is PathAccess.WRITABLE),
        key=lambda item: (path_depth(item.destination), str(item.destination)),
    )
    for item in writable:
        validate_writable_breadth(
            _operation_as_path(item),
            workspace,
            selected,
        )
    return tuple([*readable, *writable, *external_ops, workspace_op])


def _operation_as_path(operation: MountOperation) -> ResolvedSandboxPath:
    value = os.stat(operation.source, follow_symlinks=False)
    return ResolvedSandboxPath(
        configured=str(operation.destination),
        destination=operation.destination,
        source=operation.source,
        access=operation.access,
        origin=operation.origins[0],
        persistence=operation.persistence,
        creation=CreationPolicy.MUST_EXIST,
        created=False,
        label=operation.labels[0],
        identity=PinnedIdentity.from_stat(value),
    )


def parse_mountinfo(text: str) -> Tuple[MountInfoEntry, ...]:
    entries: list[MountInfoEntry] = []
    for raw_line in text.splitlines():
        left, separator, right = raw_line.partition(" - ")
        if not separator:
            raise SandboxFailure(
                "outer_sandbox_mount_table_invalid",
                "the host mount table contained a malformed entry",
            )
        fields = left.split()
        tail = right.split()
        if len(fields) < 6 or len(tail) < 3:
            raise SandboxFailure(
                "outer_sandbox_mount_table_invalid",
                "the host mount table contained an incomplete entry",
            )
        try:
            entries.append(
                MountInfoEntry(
                    mount_id=int(fields[0]),
                    parent_id=int(fields[1]),
                    major_minor=fields[2],
                    root=Path(_unescape_mount(fields[3])),
                    mountpoint=Path(_unescape_mount(fields[4])),
                    mount_options=frozenset(fields[5].split(",")),
                    filesystem_type=tail[0],
                    source=_unescape_mount(tail[1]),
                    super_options=frozenset(tail[2].split(",")),
                )
            )
        except (TypeError, ValueError) as exc:
            raise SandboxFailure(
                "outer_sandbox_mount_table_invalid",
                "the host mount table contained invalid numeric fields",
            ) from exc
    return tuple(entries)


def read_mountinfo(path: Path = Path("/proc/self/mountinfo")) -> Tuple[MountInfoEntry, ...]:
    try:
        return parse_mountinfo(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SandboxFailure(
            "outer_sandbox_mount_table_unavailable",
            "the host mount table could not be read",
        ) from exc


def _unescape_mount(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def enclosing_mount(path: Path, entries: Sequence[MountInfoEntry]) -> MountInfoEntry:
    candidates = [item for item in entries if component_contains(item.mountpoint, path)]
    if not candidates:
        raise SandboxFailure(
            "outer_sandbox_mount_table_invalid",
            "no enclosing mount entry exists for a declared path",
        )
    return max(candidates, key=lambda item: (path_depth(item.mountpoint), item.mount_id))


def audit_aliases(
    operations: Sequence[MountOperation],
    git_records: Sequence[GitProtectionRecord],
    *,
    max_entries: int,
    timeout_seconds: int,
    mount_entries: Optional[Sequence[MountInfoEntry]] = None,
) -> None:
    """Reject unsupported filesystems, mount aliases, and hard-link aliases."""

    entries = tuple(mount_entries or read_mountinfo())
    protected = [
        item
        for item in operations
        if PathOrigin.WORKSPACE in item.origins or PathOrigin.GIT_METADATA in item.origins
    ]
    writable = [item for item in operations if item.access is PathAccess.WRITABLE]
    deadline = time.monotonic() + timeout_seconds
    visited = [0]
    for operation in operations:
        _verify_no_symlink_components(operation.destination)
        value = os.stat(operation.destination, follow_symlinks=False)
        if operation.identity is not None and PinnedIdentity.from_stat(value) != operation.identity:
            raise SandboxFailure(
                "outer_sandbox_path_identity_changed",
                "a declared sandbox path changed identity after plan resolution",
            )
        if operation.access is PathAccess.WRITABLE:
            if value.st_uid != os.getuid():
                raise SandboxFailure(
                    "outer_sandbox_path_ownership",
                    "a writable sandbox path is no longer owned by the daemon uid",
                )
            if value.st_mode & 0o022:
                raise SandboxFailure(
                    "outer_sandbox_path_permissions",
                    "a writable sandbox path became group/world writable",
                )
    for operation in [*protected, *writable]:
        mount = enclosing_mount(operation.destination, entries)
        if mount.filesystem_type not in SUPPORTED_FILESYSTEMS:
            raise SandboxFailure(
                "outer_sandbox_filesystem_unsupported",
                f"{operation.labels[0]} is on unsupported filesystem {mount.filesystem_type!r}",
                remediation=("Relocate the workspace and writable state to ext4, xfs, or tmpfs.",),
            )

    for protected_op in protected:
        protected_mount = enclosing_mount(protected_op.destination, entries)
        protected_sides = [
            (
                protected_op.destination,
                _underlying_identity(protected_op.destination, protected_mount),
            )
        ]
        protected_sides.extend(
            (
                nested.mountpoint,
                _underlying_identity(nested.mountpoint, nested),
            )
            for nested in entries
            if nested.mountpoint != protected_mount.mountpoint
            and component_contains(protected_op.destination, nested.mountpoint)
        )
        for writable_op in writable:
            writable_mount = enclosing_mount(writable_op.destination, entries)
            writable_sides = [
                (
                    writable_op.destination,
                    _underlying_identity(writable_op.destination, writable_mount),
                )
            ]
            writable_sides.extend(
                (
                    nested.mountpoint,
                    _underlying_identity(nested.mountpoint, nested),
                )
                for nested in entries
                if nested.mountpoint != writable_mount.mountpoint
                and component_contains(writable_op.destination, nested.mountpoint)
            )
            for protected_path, protected_identity in protected_sides:
                for writable_path, writable_identity in writable_sides:
                    if not _identity_paths_overlap(protected_identity, writable_identity):
                        continue
                    narrowing_relation = component_contains(
                        writable_op.destination,
                        protected_op.destination,
                    )
                    allowed_narrowing = narrowing_relation and (
                        (
                            protected_path == protected_op.destination
                            and writable_path == writable_op.destination
                        )
                        or (
                            protected_path == writable_path
                            and component_contains(
                                protected_op.destination,
                                protected_path,
                            )
                        )
                    )
                    if allowed_narrowing:
                        continue
                    raise SandboxFailure(
                        "outer_sandbox_mount_alias",
                        "a writable mount or nested mount aliases protected storage",
                    )
    protected_inodes = _collect_protected_inodes(
        protected,
        deadline=deadline,
        max_entries=max_entries,
        visited=visited,
    )
    for writable_op in writable:
        _audit_writable_hardlinks(
            protected,
            protected_inodes,
            writable_op,
            deadline=deadline,
            max_entries=max_entries,
            visited=visited,
        )

    # Logical roots remain pinned even though containment comparisons use only
    # coverage sides.
    for record in git_records:
        value = os.stat(record.destination, follow_symlinks=False)
        if PinnedIdentity.from_stat(value) != record.identity:
            raise SandboxFailure(
                "outer_sandbox_path_identity_changed",
                "Git metadata identity changed during the alias audit",
            )


def _underlying_identity(path: Path, mount: MountInfoEntry) -> Tuple[str, Path]:
    relative = path.relative_to(mount.mountpoint)
    return mount.major_minor, (mount.root / relative)


def _identity_paths_overlap(left: Tuple[str, Path], right: Tuple[str, Path]) -> bool:
    if left[0] != right[0]:
        return False
    return component_contains(left[1], right[1]) or component_contains(right[1], left[1])


def _collect_protected_inodes(
    protected: Sequence[MountOperation],
    *,
    deadline: float,
    max_entries: int,
    visited: list[int],
) -> set[Tuple[int, int, int]]:
    roots: list[Path] = []
    for candidate in sorted(
        (item.destination for item in protected),
        key=lambda item: (path_depth(item), str(item)),
    ):
        if not any(component_contains(parent, candidate) for parent in roots):
            roots.append(candidate)

    protected_inodes: set[Tuple[int, int, int]] = set()
    for root in roots:
        for _path, value in _walk_no_symlinks(
            root,
            (),
            deadline=deadline,
            budget=max_entries,
            visited_start=visited[0],
        ):
            visited[0] += 1
            if not stat.S_ISDIR(value.st_mode):
                protected_inodes.add((value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)))
    return protected_inodes


def _audit_writable_hardlinks(
    protected: Sequence[MountOperation],
    protected_inodes: set[Tuple[int, int, int]],
    writable: MountOperation,
    *,
    deadline: float,
    max_entries: int,
    visited: list[int],
) -> None:
    protected_below = sorted(
        (
            item.destination
            for item in protected
            if component_contains(writable.destination, item.destination)
        ),
        key=lambda item: (path_depth(item), str(item)),
    )
    prune: list[Path] = []
    for candidate in protected_below:
        if not any(component_contains(parent, candidate) for parent in prune):
            prune.append(candidate)

    for _path, value in _walk_no_symlinks(
        writable.destination,
        tuple(prune),
        deadline=deadline,
        budget=max_entries,
        visited_start=visited[0],
    ):
        visited[0] += 1
        if stat.S_ISDIR(value.st_mode):
            continue
        identity = (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))
        if identity in protected_inodes:
            raise SandboxFailure(
                "outer_sandbox_hardlink_alias",
                "a protected inode is reachable through writable remainder",
            )


def _walk_no_symlinks(
    root: Path,
    prune: Sequence[Path],
    *,
    deadline: float,
    budget: int,
    visited_start: int,
) -> Iterator[Tuple[Path, os.stat_result]]:
    visited = visited_start
    root_fd = _open_pinned_directory(root)
    root_identity = PinnedIdentity.from_stat(os.fstat(root_fd))
    stack: list[tuple[Tuple[str, ...], PinnedIdentity]] = [((), root_identity)]
    try:
        while stack:
            relative, expected = stack.pop()
            path = root.joinpath(*relative)
            directory_fd = _reopen_pinned_relative_directory(root_fd, relative, expected)
            try:
                value = os.fstat(directory_fd)
                _check_alias_audit_limit(visited, budget, deadline)
                visited += 1
                yield path, value
                if any(component_contains(item, path) for item in prune):
                    continue
                try:
                    names = sorted(os.listdir(directory_fd), reverse=True)
                except OSError as exc:
                    raise SandboxFailure(
                        "outer_sandbox_alias_audit_failed",
                        "the alias audit could not enumerate a declared tree",
                    ) from exc
                pending_directories: list[tuple[Tuple[str, ...], PinnedIdentity]] = []
                for name in names:
                    child_path = path / name
                    if any(item == child_path for item in prune):
                        continue
                    _check_alias_audit_limit(visited, budget, deadline)
                    child_fd = None
                    try:
                        child_fd = os.open(
                            name,
                            getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory_fd,
                        )
                        child_value = os.fstat(child_fd)
                    except OSError as exc:
                        raise SandboxFailure(
                            "outer_sandbox_alias_audit_failed",
                            "a declared tree changed during its pinned alias audit",
                        ) from exc
                    finally:
                        if child_fd is not None:
                            os.close(child_fd)
                    identity = PinnedIdentity.from_stat(child_value)
                    if stat.S_ISDIR(child_value.st_mode):
                        pending_directories.append(((*relative, name), identity))
                    else:
                        visited += 1
                        yield child_path, child_value
                stack.extend(pending_directories)
            finally:
                os.close(directory_fd)
    finally:
        os.close(root_fd)


def _check_alias_audit_limit(visited: int, budget: int, deadline: float) -> None:
    if time.monotonic() >= deadline or visited >= budget:
        raise SandboxFailure(
            "outer_sandbox_alias_audit_exceeded",
            "the bounded alias audit exceeded its configured limit",
            remediation=(
                "Raise system.sandbox_alias_audit_max_entries or "
                "system.sandbox_alias_audit_timeout_seconds.",
            ),
        )


def _open_pinned_directory(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise SandboxFailure(
            "outer_sandbox_alias_audit_failed",
            "a declared audit root changed or traversed a symlink",
        ) from exc


def _reopen_pinned_relative_directory(
    root_fd: int,
    relative: Sequence[str],
    expected: PinnedIdentity,
) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in relative:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        if PinnedIdentity.from_stat(os.fstat(descriptor)) != expected:
            raise SandboxFailure(
                "outer_sandbox_alias_audit_failed",
                "a declared directory changed identity during its pinned alias audit",
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
