from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_collab.sandbox.paths import (
    GitProtectionRecord,
    GitProvenance,
    MountInfoEntry,
    MountOperation,
    PinnedIdentity,
    audit_aliases,
    component_contains,
    discover_session_git,
    normalize_mounts,
    parse_mountinfo,
    resolve_workspace,
)
from agent_collab.sandbox.specs import (
    GitRole,
    PathAccess,
    PathOrigin,
    Persistence,
    SandboxFailure,
)


def _identity(path: Path) -> PinnedIdentity:
    return PinnedIdentity.from_stat(os.stat(path, follow_symlinks=False))


def _record(path: Path, role: GitRole) -> GitProtectionRecord:
    return GitProtectionRecord(path, role, (GitProvenance(),), _identity(path))


class GitProtectionTests(unittest.TestCase):
    def test_component_boundary_does_not_absorb_workspace_dot_git_sibling(self):
        self.assertFalse(component_contains(Path("/work/app"), Path("/work/app.git")))

    def test_ordinary_and_bare_roots_are_absorbed_by_workspace_last(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace_path = Path(raw).resolve()
            git = workspace_path / ".git"
            objects = git / "objects"
            objects.mkdir(parents=True)
            workspace = resolve_workspace(workspace_path)
            operations = normalize_mounts(
                workspace,
                (),
                (
                    _record(git, GitRole.WORKTREE_GIT_DIR),
                    _record(git, GitRole.COMMON_GIT_DIR),
                    _record(objects, GitRole.PRIMARY_OBJECT_STORE),
                ),
            )
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[-1].destination, workspace_path)
            self.assertEqual(
                operations[-1].git_roles,
                (
                    GitRole.WORKTREE_GIT_DIR,
                    GitRole.COMMON_GIT_DIR,
                    GitRole.PRIMARY_OBJECT_STORE,
                ),
            )

            bare_objects = workspace_path / "objects"
            bare_objects.mkdir()
            bare = normalize_mounts(
                workspace,
                (),
                (
                    _record(workspace_path, GitRole.WORKTREE_GIT_DIR),
                    _record(workspace_path, GitRole.COMMON_GIT_DIR),
                    _record(bare_objects, GitRole.PRIMARY_OBJECT_STORE),
                ),
            )
            self.assertEqual(len(bare), 1)
            self.assertEqual(bare[-1].origins, (PathOrigin.WORKSPACE, PathOrigin.GIT_METADATA))

    def test_external_roots_collapse_to_shallowest_anchor_and_workspace_is_last(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace_path = root / "workspace"
            external = root / "workspace.git"
            child = external / "objects"
            workspace_path.mkdir()
            child.mkdir(parents=True)
            workspace = resolve_workspace(workspace_path)
            operations = normalize_mounts(
                workspace,
                (),
                (
                    _record(external, GitRole.COMMON_GIT_DIR),
                    _record(child, GitRole.PRIMARY_OBJECT_STORE),
                ),
            )
            self.assertEqual([item.destination for item in operations], [external, workspace_path])
            self.assertEqual(operations[0].covered_paths, (external, child))

    def test_strict_worktree_linked_common_dir_and_alternates_bfs_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace_path = root / "workspace"
            git_dir = root / "gitdirs" / "one"
            common = root / "common"
            primary = common / "objects"
            alternate_a = root / "objects-a"
            alternate_b = root / "objects-b"
            workspace_path.mkdir()
            git_dir.mkdir(parents=True)
            primary.mkdir(parents=True)
            alternate_a.mkdir()
            alternate_b.mkdir()
            (workspace_path / ".git").write_text(
                f"gitdir: {git_dir}\n",
                encoding="utf-8",
            )
            (git_dir / "commondir").write_text(f"{common}\n", encoding="utf-8")
            (primary / "info").mkdir()
            (alternate_a / "info").mkdir()
            (primary / "info" / "alternates").write_text(
                f"{alternate_a}\n{alternate_b}\n{alternate_a}\n",
                encoding="utf-8",
            )
            (alternate_a / "info" / "alternates").write_text(
                f"{primary}\n",
                encoding="utf-8",
            )
            workspace = resolve_workspace(workspace_path)
            with (
                mock.patch(
                    "agent_collab.sandbox.paths._resolve_git",
                    return_value="/usr/bin/git",
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._git_validate",
                    return_value=(git_dir, common, False),
                ),
            ):
                discovery = discover_session_git(workspace)
            self.assertEqual(discovery.kind, "worktree")
            alternates = [
                item for item in discovery.records if item.role is GitRole.ALTERNATE_OBJECT_STORE
            ]
            self.assertEqual(
                [item.destination for item in alternates],
                [alternate_a, alternate_b, alternate_a, primary],
            )
            self.assertTrue(alternates[2].provenance[0].duplicate)
            self.assertTrue(alternates[3].provenance[0].cycle)
            self.assertEqual(alternates[0].provenance[0].line_ordinal, 1)

    def test_bare_repository_validation_retains_all_roles(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (root / "objects").mkdir()
            (root / "refs").mkdir()
            workspace = resolve_workspace(root)
            with (
                mock.patch(
                    "agent_collab.sandbox.paths._resolve_git",
                    return_value="/usr/bin/git",
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._git_validate",
                    return_value=(root, root, True),
                ),
            ):
                discovery = discover_session_git(workspace)
            self.assertEqual(discovery.kind, "bare")
            self.assertEqual(
                [item.role for item in discovery.records],
                [
                    GitRole.WORKTREE_GIT_DIR,
                    GitRole.COMMON_GIT_DIR,
                    GitRole.PRIMARY_OBJECT_STORE,
                ],
            )

    def test_alternate_grammar_requires_final_newline(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            git = root / ".git"
            objects = git / "objects"
            (objects / "info").mkdir(parents=True)
            (objects / "info" / "alternates").write_text("/missing", encoding="utf-8")
            workspace = resolve_workspace(root)
            with (
                mock.patch(
                    "agent_collab.sandbox.paths._resolve_git",
                    return_value="/usr/bin/git",
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._git_validate",
                    return_value=(git, git, False),
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                discover_session_git(workspace)
            self.assertEqual(raised.exception.code, "outer_sandbox_git_discovery_invalid")


class AliasAuditTests(unittest.TestCase):
    def _mount(self, root: Path, filesystem: str = "ext4") -> MountInfoEntry:
        return MountInfoEntry(
            mount_id=1,
            parent_id=0,
            major_minor="8:1",
            root=Path("/"),
            mountpoint=Path("/"),
            mount_options=frozenset({"rw"}),
            filesystem_type=filesystem,
            source="/dev/test",
            super_options=frozenset({"rw"}),
        )

    def _operation(
        self,
        path: Path,
        access: PathAccess,
        origin: PathOrigin,
    ) -> MountOperation:
        return MountOperation(
            path,
            path,
            access,
            Persistence.HOST,
            (origin,),
            (origin.value,),
        )

    def test_unsupported_filesystem_fails_with_stable_code(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    (self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),),
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root, "overlay"),),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_filesystem_unsupported")

    def test_read_only_bind_ignores_writable_backing_superblock(self):
        entries = parse_mountinfo("42 31 8:1 / /workspace ro,relatime - ext4 /dev/root rw\n")

        self.assertFalse(entries[0].writable)

    def test_hardlink_from_protected_tree_into_writable_remainder_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            workspace.mkdir()
            writable.mkdir()
            source = workspace / "tracked"
            source.write_text("x", encoding="utf-8")
            os.link(source, writable / "alias")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_hardlink_between_two_pruned_protected_anchors_is_allowed(self):
        with tempfile.TemporaryDirectory() as raw:
            writable = Path(raw).resolve()
            left = writable / "git-a"
            right = writable / "git-b"
            left.mkdir()
            right.mkdir()
            source = left / "object"
            source.write_text("x", encoding="utf-8")
            os.link(source, right / "same-object")
            operations = (
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
                self._operation(left, PathAccess.READ_ONLY, PathOrigin.GIT_METADATA),
                self._operation(right, PathAccess.READ_ONLY, PathOrigin.GIT_METADATA),
            )
            audit_aliases(
                operations,
                (),
                max_entries=1_000_000,
                timeout_seconds=10,
                mount_entries=(self._mount(writable),),
            )

    def test_nested_writable_bind_below_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            workspace.mkdir()
            writable.mkdir()
            nested = MountInfoEntry(
                mount_id=2,
                parent_id=1,
                major_minor="8:1",
                root=writable,
                mountpoint=workspace / "alias",
                mount_options=frozenset({"rw"}),
                filesystem_type="ext4",
                source="/dev/test",
                super_options=frozenset({"rw"}),
            )
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )

            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root), nested),
                )

            self.assertEqual(raised.exception.code, "outer_sandbox_mount_alias")

    def test_writable_identity_is_revalidated_before_launch_audit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            workspace.mkdir()
            writable.mkdir()
            writable_operation = MountOperation(
                writable,
                writable,
                PathAccess.WRITABLE,
                Persistence.HOST,
                (PathOrigin.PROVIDER_STATE,),
                ("State",),
                identity=_identity(writable),
            )
            writable.rename(root / "old-state")
            writable.mkdir()

            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    (
                        self._operation(
                            workspace,
                            PathAccess.READ_ONLY,
                            PathOrigin.WORKSPACE,
                        ),
                        writable_operation,
                    ),
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )

            self.assertEqual(raised.exception.code, "outer_sandbox_path_identity_changed")
