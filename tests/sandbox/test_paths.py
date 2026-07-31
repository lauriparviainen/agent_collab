from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_collab.sandbox import paths as sandbox_paths
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
    _reopen_pinned_relative_directory,
    resolve_accounting_peer_roots,
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

    def test_relative_directory_reopen_wraps_filesystem_races(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                expected = PinnedIdentity.from_stat(os.fstat(root_fd))
                with self.assertRaises(SandboxFailure) as raised:
                    _reopen_pinned_relative_directory(
                        root_fd,
                        ("removed-before-reopen",),
                        expected,
                    )
            finally:
                os.close(root_fd)

        self.assertEqual(
            raised.exception.code,
            "outer_sandbox_alias_audit_failed",
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

    def test_protected_churn_is_not_walked_without_unaccounted_candidates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            churn = workspace / "churn"
            churn.mkdir(parents=True)
            writable.mkdir()
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            stop = threading.Event()

            def writer() -> None:
                while not stop.is_set():
                    path = churn / "moving"
                    path.write_text("x", encoding="utf-8")
                    path.unlink(missing_ok=True)

            original_walk = sandbox_paths._walk_no_symlinks

            def guarded_walk(path, *args, **kwargs):
                if path == workspace:
                    self.fail("protected coverage must not be walked")
                yield from original_walk(path, *args, **kwargs)

            thread = threading.Thread(target=writer)
            thread.start()
            try:
                with mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    guarded_walk,
                ):
                    audit_aliases(
                        operations,
                        (),
                        max_entries=1_000_000,
                        timeout_seconds=10,
                        mount_entries=(self._mount(root),),
                    )
            finally:
                stop.set()
                thread.join()

    def test_hardlinks_wholly_inside_writable_union_skip_protected_walk(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable_a = root / "state-a"
            writable_b = root / "state-b"
            workspace.mkdir()
            writable_a.mkdir()
            writable_b.mkdir()
            source = writable_a / "one"
            source.write_text("x", encoding="utf-8")
            os.link(source, writable_b / "two")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable_a, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
                self._operation(writable_b, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_walk = sandbox_paths._walk_no_symlinks

            def guarded_walk(path, *args, **kwargs):
                if path == workspace:
                    self.fail("fully accounted writable inode forced protected walk")
                yield from original_walk(path, *args, **kwargs)

            with mock.patch(
                "agent_collab.sandbox.paths._walk_no_symlinks",
                guarded_walk,
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )

    def test_file_bind_of_writable_name_cannot_complete_link_count(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            workspace.mkdir()
            writable.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            writable_name = writable / "alias"
            os.link(protected_name, writable_name)
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            bound_view = writable / "bound-view"
            writable_file_bind = MountInfoEntry(
                mount_id=2,
                parent_id=1,
                major_minor="8:1",
                root=writable_name,
                mountpoint=bound_view,
                mount_options=frozenset({"rw"}),
                filesystem_type="ext4",
                source="/dev/test",
                super_options=frozenset({"rw"}),
            )
            original_walk = sandbox_paths._walk_no_symlinks
            original_revalidate = sandbox_paths._revalidate_counted_name

            def bind_view_walk(path, *args, **kwargs):
                if path == writable:
                    yield writable, os.stat(writable)
                    yield writable_name, os.stat(writable_name)
                    prune = args[0] if args else kwargs["prune"]
                    if bound_view not in prune:
                        yield bound_view, os.stat(writable_name)
                    return
                yield from original_walk(path, *args, **kwargs)

            def bind_view_revalidate(path):
                if path == bound_view:
                    return os.stat(writable_name), _identity(writable)
                return original_revalidate(path)

            with (
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    bind_view_walk,
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._revalidate_counted_name",
                    bind_view_revalidate,
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root), writable_file_bind),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_peer_root_accounts_external_name_but_not_protected_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            peer = root / "peer"
            workspace.mkdir()
            writable.mkdir()
            peer.mkdir()
            source = writable / "state-name"
            source.write_text("x", encoding="utf-8")
            os.link(source, peer / "peer-name")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_walk = sandbox_paths._walk_no_symlinks

            def guarded_walk(path, *args, **kwargs):
                if path == workspace:
                    self.fail("peer-accounted inode forced protected walk")
                yield from original_walk(path, *args, **kwargs)

            with mock.patch(
                "agent_collab.sandbox.paths._walk_no_symlinks",
                guarded_walk,
            ):
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )

            os.link(source, workspace / "protected-name")
            logs: list[str] = []
            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                    log=logs.append,
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")
            self.assertTrue(any("protected-search" in item for item in logs))
            self.assertTrue(any("hard-link-match" in item for item in logs))

    def test_peer_walk_failure_is_nonfatal_and_forces_protected_search(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            peer = root / "peer"
            workspace.mkdir()
            writable.mkdir()
            peer.mkdir()
            source = writable / "state-name"
            source.write_text("x", encoding="utf-8")
            os.link(source, peer / "peer-name")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_walk = sandbox_paths._walk_no_symlinks

            def failing_peer_walk(path, *args, **kwargs):
                if path == peer:
                    error = OSError(errno.ENOENT, "simulated peer churn")
                    failure = sandbox_paths._alias_audit_traversal_failure(
                        "peer changed",
                        peer / "moving",
                        error,
                    )
                    kwargs["on_error"](failure)
                    return
                yield from original_walk(path, *args, **kwargs)

            logs: list[str] = []
            with mock.patch(
                "agent_collab.sandbox.paths._walk_no_symlinks",
                failing_peer_walk,
            ):
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                    log=logs.append,
                )
            self.assertTrue(any("peer-skipped" in item for item in logs))
            self.assertTrue(any("protected-search" in item for item in logs))

    def test_unrelated_peer_entry_failure_keeps_revalidated_candidate_names(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            peer = root / "peer"
            workspace.mkdir()
            writable.mkdir()
            peer.mkdir()
            source = writable / "state-name"
            source.write_text("x", encoding="utf-8")
            os.link(source, peer / "peer-name")
            (peer / "z-moving").write_text("churn", encoding="utf-8")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_open = os.open
            original_walk = sandbox_paths._walk_no_symlinks

            def racing_open(path, flags, *args, **kwargs):
                if path == "z-moving" and kwargs.get("dir_fd") is not None:
                    raise FileNotFoundError(errno.ENOENT, "simulated unrelated peer churn")
                return original_open(path, flags, *args, **kwargs)

            def guarded_walk(path, *args, **kwargs):
                if path == workspace:
                    self.fail("a stable peer count must avoid the protected walk")
                yield from original_walk(path, *args, **kwargs)

            logs: list[str] = []
            with (
                mock.patch("agent_collab.sandbox.paths.os.open", racing_open),
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    guarded_walk,
                ),
            ):
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                    log=logs.append,
                )
            self.assertTrue(any("peer-skipped" in item for item in logs))
            self.assertFalse(any("protected-search" in item for item in logs))

    def test_different_device_candidate_skips_protected_walk(self):
        if not Path("/dev/shm").is_dir():
            self.skipTest("/dev/shm is unavailable")
        with (
            tempfile.TemporaryDirectory() as raw,
            tempfile.TemporaryDirectory(dir="/dev/shm") as state_raw,
        ):
            root = Path(raw).resolve()
            state_root = Path(state_raw).resolve()
            if os.stat(root).st_dev == os.stat(state_root).st_dev:
                self.skipTest("test requires two devices")
            workspace = root / "workspace"
            writable = state_root / "state"
            external = state_root / "external"
            workspace.mkdir()
            writable.mkdir()
            external.mkdir()
            source = writable / "state-name"
            source.write_text("x", encoding="utf-8")
            os.link(source, external / "unaccounted-name")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            shm_mount = MountInfoEntry(
                mount_id=2,
                parent_id=1,
                major_minor="0:2",
                root=Path("/"),
                mountpoint=Path("/dev/shm"),
                mount_options=frozenset({"rw"}),
                filesystem_type="tmpfs",
                source="tmpfs",
                super_options=frozenset({"rw"}),
            )
            original_walk = sandbox_paths._walk_no_symlinks

            def guarded_walk(path, *args, **kwargs):
                if path == workspace:
                    self.fail("different-device protected coverage was walked")
                yield from original_walk(path, *args, **kwargs)

            with mock.patch(
                "agent_collab.sandbox.paths._walk_no_symlinks",
                guarded_walk,
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root), shm_mount),
                )

    def test_rename_inflation_does_not_hide_protected_alias(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            first = writable / "d1"
            second = writable / "d2"
            workspace.mkdir()
            first.mkdir(parents=True)
            second.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            old_name = first / "a"
            new_name = second / "b"
            os.link(protected_name, old_name)
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_walk = sandbox_paths._walk_no_symlinks

            def renaming_walk(path, *args, **kwargs):
                for found, value in original_walk(path, *args, **kwargs):
                    yield found, value
                    if path == writable and found == old_name:
                        old_name.rename(new_name)

            with (
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    renaming_walk,
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_link_count_deflation_is_revalidated_before_accounting(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            workspace.mkdir()
            writable.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            writable_name = writable / "alias"
            os.link(protected_name, writable_name)
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_walk = sandbox_paths._walk_no_symlinks

            def deflating_walk(path, *args, **kwargs):
                changed = False
                for found, value in original_walk(path, *args, **kwargs):
                    yield found, value
                    if path == writable and found == writable_name:
                        protected_name.unlink()
                        changed = True
                if changed:
                    os.link(writable_name, protected_name)

            with (
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    deflating_walk,
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_protected_below_prune_is_load_bearing(self):
        with tempfile.TemporaryDirectory() as raw:
            writable = Path(raw).resolve()
            protected = writable / "git"
            protected.mkdir()
            protected_name = protected / "object"
            protected_name.write_text("x", encoding="utf-8")
            os.link(protected_name, writable / "alias")
            operations = (
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
                self._operation(protected, PathAccess.READ_ONLY, PathOrigin.GIT_METADATA),
            )
            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(writable),),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

            with mock.patch(
                "agent_collab.sandbox.paths._protected_below_prune",
                return_value=(),
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(writable),),
                )

    def test_hide_and_restore_open_failure_stays_fail_closed_and_logs_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            external = root / "external"
            workspace.mkdir()
            writable.mkdir()
            external.mkdir()
            churn = workspace / "churn"
            churn.write_text("x", encoding="utf-8")
            source = writable / "state-name"
            source.write_text("x", encoding="utf-8")
            os.link(source, external / "unaccounted-name")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            original_open = os.open

            def racing_open(path, flags, *args, **kwargs):
                if path == "churn" and kwargs.get("dir_fd") is not None:
                    raise FileNotFoundError(errno.ENOENT, "simulated hide-and-restore")
                return original_open(path, flags, *args, **kwargs)

            logs: list[str] = []
            with (
                mock.patch("agent_collab.sandbox.paths.os.open", racing_open),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                    log=logs.append,
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_alias_audit_failed")
            self.assertTrue(any(f"path={churn}" in item and "errno=2" in item for item in logs))

    def test_accounting_peer_overlap_with_writable_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            writable = Path(raw).resolve()
            operations = (
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            for peer in (writable / "peer", writable.parent / "elsewhere" / ".." / writable.name):
                with (
                    self.subTest(peer=peer),
                    self.assertRaises(SandboxFailure) as raised,
                ):
                    resolve_accounting_peer_roots((peer,), operations)
                self.assertEqual(
                    raised.exception.code,
                    "outer_sandbox_accounting_peer_overlap",
                )

    def test_dotdot_peer_alias_of_protected_root_cannot_hide_hardlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            workspace.mkdir()
            writable.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            os.link(protected_name, writable / "alias")
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            disguised_workspace = root / "unused" / ".." / "workspace"
            with self.assertRaises(SandboxFailure) as raised:
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(disguised_workspace,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root),),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_bind_peer_alias_of_protected_root_cannot_hide_hardlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            peer = root / "peer"
            workspace.mkdir()
            writable.mkdir()
            peer.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            writable_name = writable / "alias"
            os.link(protected_name, writable_name)
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            peer_bind = MountInfoEntry(
                mount_id=2,
                parent_id=1,
                major_minor="8:1",
                root=workspace,
                mountpoint=peer,
                mount_options=frozenset({"rw"}),
                filesystem_type="ext4",
                source="/dev/test",
                super_options=frozenset({"rw"}),
            )
            original_walk = sandbox_paths._walk_no_symlinks
            original_revalidate = sandbox_paths._revalidate_counted_name
            peer_name = peer / protected_name.name

            def bind_view_walk(path, *args, **kwargs):
                if path == peer:
                    yield peer, os.stat(peer)
                    yield peer_name, os.stat(protected_name)
                    return
                yield from original_walk(path, *args, **kwargs)

            def bind_view_revalidate(path):
                if path == peer_name:
                    return os.stat(protected_name), _identity(peer)
                return original_revalidate(path)

            with (
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    bind_view_walk,
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._revalidate_counted_name",
                    bind_view_revalidate,
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root), peer_bind),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_file_bind_peer_alias_of_writable_name_cannot_hide_hardlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            peer = root / "peer"
            workspace.mkdir()
            writable.mkdir()
            peer.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            writable_name = writable / "alias"
            os.link(protected_name, writable_name)
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            peer_name = peer / "bound-alias"
            peer_file_bind = MountInfoEntry(
                mount_id=2,
                parent_id=1,
                major_minor="8:1",
                root=writable_name,
                mountpoint=peer_name,
                mount_options=frozenset({"rw"}),
                filesystem_type="ext4",
                source="/dev/test",
                super_options=frozenset({"rw"}),
            )
            original_walk = sandbox_paths._walk_no_symlinks
            original_revalidate = sandbox_paths._revalidate_counted_name

            def bind_view_walk(path, *args, **kwargs):
                if path == peer:
                    yield peer, os.stat(peer)
                    prune = args[0] if args else kwargs["prune"]
                    if peer_name not in prune:
                        yield peer_name, os.stat(writable_name)
                    return
                yield from original_walk(path, *args, **kwargs)

            def bind_view_revalidate(path):
                if path == peer_name:
                    return os.stat(writable_name), _identity(peer)
                return original_revalidate(path)

            with (
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    bind_view_walk,
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._revalidate_counted_name",
                    bind_view_revalidate,
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root), peer_file_bind),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_file_bind_of_peer_name_cannot_complete_link_count(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            workspace = root / "workspace"
            writable = root / "state"
            peer = root / "peer"
            workspace.mkdir()
            writable.mkdir()
            peer.mkdir()
            protected_name = workspace / "secret"
            protected_name.write_text("x", encoding="utf-8")
            writable_name = writable / "alias"
            peer_name = peer / "result"
            os.link(protected_name, writable_name)
            os.link(protected_name, peer_name)
            operations = (
                self._operation(workspace, PathAccess.READ_ONLY, PathOrigin.WORKSPACE),
                self._operation(writable, PathAccess.WRITABLE, PathOrigin.PROVIDER_STATE),
            )
            bound_view = peer / "bound-view"
            peer_file_bind = MountInfoEntry(
                mount_id=2,
                parent_id=1,
                major_minor="8:1",
                root=peer_name,
                mountpoint=bound_view,
                mount_options=frozenset({"rw"}),
                filesystem_type="ext4",
                source="/dev/test",
                super_options=frozenset({"rw"}),
            )
            original_walk = sandbox_paths._walk_no_symlinks
            original_revalidate = sandbox_paths._revalidate_counted_name

            def bind_view_walk(path, *args, **kwargs):
                if path == peer:
                    yield peer, os.stat(peer)
                    yield peer_name, os.stat(peer_name)
                    prune = args[0] if args else kwargs["prune"]
                    if bound_view not in prune:
                        yield bound_view, os.stat(peer_name)
                    return
                yield from original_walk(path, *args, **kwargs)

            def bind_view_revalidate(path):
                if path == bound_view:
                    return os.stat(peer_name), _identity(peer)
                return original_revalidate(path)

            with (
                mock.patch(
                    "agent_collab.sandbox.paths._walk_no_symlinks",
                    bind_view_walk,
                ),
                mock.patch(
                    "agent_collab.sandbox.paths._revalidate_counted_name",
                    bind_view_revalidate,
                ),
                self.assertRaises(SandboxFailure) as raised,
            ):
                audit_aliases(
                    operations,
                    (),
                    accounting_peer_roots=(peer,),
                    max_entries=1_000_000,
                    timeout_seconds=10,
                    mount_entries=(self._mount(root), peer_file_bind),
                )
            self.assertEqual(raised.exception.code, "outer_sandbox_hardlink_alias")

    def test_revalidation_counts_unique_directory_entries_not_path_views(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            state = root / "state"
            child = state / "child"
            protected = root / "secret"
            child.mkdir(parents=True)
            protected.write_text("x", encoding="utf-8")
            state_name = state / "alias"
            os.link(protected, state_name)
            identity = _identity(state_name)
            candidates = {
                (identity.device, identity.inode, identity.file_type): [
                    state_name,
                    child / ".." / state_name.name,
                ]
            }

            self.assertEqual(
                sandbox_paths._revalidate_writable_candidates(candidates),
                {(identity.device, identity.inode, identity.file_type)},
            )

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
