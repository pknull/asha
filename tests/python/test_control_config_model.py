from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.config import (
    ConfigError,
    load_config,
    namespace_ancestor_problem,
    namespace_safety_step,
    validate_workspace_root,
)
from lib.control.model import (
    ModelError,
    TASK_LIFECYCLE_TRANSITIONS,
    RUN_STATE_TRANSITIONS,
    canonical_uuid,
    new_uuid,
    require_run_transition,
    require_task_transition,
    validate_slug,
    validate_task,
)


def task_record(
    *,
    task_id: str | None = None,
    slug: str = "control-test",
    repository_root: str = "/tmp/source",
    workspace_path: str = "/tmp/workspaces/control-test",
) -> dict:
    task_id = task_id or str(uuid.uuid4())
    return {
        "contract": "asha.control-task.v1",
        "task_id": task_id,
        "slug": slug,
        "label": "Control test",
        "created_at": "2026-08-14T18:00:00Z",
        "updated_at": "2026-08-14T18:00:01Z",
        "lifecycle": "running",
        "repository": {"root": repository_root, "identity": "repo-123"},
        "source": {"kind": "ad-hoc", "number": None, "url": None},
        "jj": {
            "workspace_name": "asha-control-test-12345678",
            "workspace_path": workspace_path,
            "requested_base": "trunk()",
            "base_commit_id": "a" * 40,
            "change_id": "k" * 32,
            "working_commit_id": "c" * 40,
        },
        "tmux": {
            "socket": "default",
            "session": "asha-control-test-12345678",
            "window": "work",
        },
        "runs": [
            {
                "contract": "asha.control-run.v1",
                "run_id": str(uuid.uuid4()),
                "harness": "codex",
                "role": "implementer",
                "pane_id": "%23",
                "pid": 12345,
                "process_start_identity": "linux-proc-start:123",
                "harness_session_id": None,
                "state": "starting",
                "evidence": "controller launch",
                "evidence_at": "2026-08-14T18:00:01Z",
            }
        ],
    }


def write_config(path: Path, value: str) -> None:
    path.write_text(value)
    path.chmod(0o600)


class ControlConfigTests(unittest.TestCase):
    def test_defaults_use_isolated_asha_home_and_launcher_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {
                "HOME": str(root / "home"),
                "ASHA_HOME": str(root / "asha"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "ASHA_CONFIG": str(root / "missing.json"),
            }

            config = load_config(env)

            self.assertEqual(config.default_harness, "claude")
            self.assertEqual(config.tasks_dir, root / "asha/state/control/tasks")
            self.assertEqual(config.workspace_root, root / "asha/workspaces")
            self.assertEqual(config.runtime_dir, root / "runtime/asha-control")
            self.assertEqual(config.popup_width, "90%")
            self.assertEqual(config.session_prefix, "asha-")
            self.assertEqual(config.event_staleness_seconds, 1800)

    def test_event_staleness_seconds_parses_and_is_bounded(self) -> None:
        def load(value_json: str):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "home").mkdir()
                (root / "home").chmod(0o700)
                config_path = root / "config.json"
                write_config(
                    config_path,
                    '{"control":{"event_staleness_seconds":' + value_json + '}}',
                )
                return load_config({
                    "HOME": str(root / "home"),
                    "ASHA_CONFIG": str(config_path),
                    "ASHA_HOME": str(root / "asha"),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                })

        self.assertEqual(load("60").event_staleness_seconds, 60)
        self.assertEqual(load("86400").event_staleness_seconds, 86400)
        for invalid in ("0", "-1", "86401", "true", "1.5", '"1800"', "null"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ConfigError, "event_staleness_seconds",
            ):
                load(invalid)

    def test_nested_default_harness_precedes_existing_root_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            config_path = root / "config.json"
            write_config(config_path, json.dumps({
                "default_harness": "claude",
                "control": {"default_harness": "codex", "workspace_root": str(root / "ws")},
            }))
            config = load_config({
                "HOME": str(home),
                "ASHA_CONFIG": str(config_path),
                "ASHA_HOME": str(root / "asha"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            })
            self.assertEqual(config.default_harness, "codex")
            self.assertEqual(config.workspace_root, root / "ws")

    def test_explicit_null_default_harness_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home").mkdir()
            (root / "home").chmod(0o700)
            config_path = root / "config.json"
            write_config(config_path, '{"control":{"default_harness":null}}')
            with self.assertRaisesRegex(ConfigError, "default_harness"):
                load_config({"HOME": str(root / "home"), "ASHA_CONFIG": str(config_path)})

    def test_invalid_control_config_fails_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            config_path = root / "config.json"
            write_config(config_path, '{"control":{"tmux":{"popup_width":"wide"}}}')
            with self.assertRaisesRegex(ConfigError, "popup_width"):
                load_config({
                    "HOME": str(home),
                    "ASHA_CONFIG": str(config_path),
                    "ASHA_HOME": str(root / "asha"),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                })
            self.assertFalse((root / "state").exists())

    def test_default_config_supports_an_owned_relative_leaf_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o755)
            home = root / "home"
            config_parent = home / ".asha"
            target = home / "dotfiles/asha/.asha/config.json"
            config_parent.mkdir(parents=True, mode=0o700)
            target.parent.mkdir(parents=True, mode=0o700)
            home.chmod(0o750)
            # 0755: group-READABLE stays fine; the old 0775 group-writable
            # tolerance is retired — a writable .asha now refuses everywhere,
            # because the state tree lives beneath it.
            config_parent.chmod(0o755)
            target.parent.chmod(0o755)
            write_config(target, '{"control":{"default_harness":"codex"}}')
            (config_parent / "config.json").symlink_to(
                "../dotfiles/asha/.asha/config.json"
            )

            config = load_config({
                "HOME": str(home),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            })

            self.assertEqual(config.config_path, config_parent / "config.json")
            self.assertEqual(config.default_harness, "codex")

    def test_config_rejects_group_or_world_writable_opened_file_inode(self) -> None:
        for topology in ("direct", "leaf-symlink"):
            for unsafe_mode in (0o620, 0o602):
                with (
                    self.subTest(topology=topology, unsafe_mode=oct(unsafe_mode)),
                    tempfile.TemporaryDirectory() as td,
                ):
                    root = Path(td)
                    root.chmod(0o755)
                    home = root / "home"
                    config_parent = home / ".asha"
                    target_parent = home / "dotfiles/asha/.asha"
                    config_parent.mkdir(parents=True, mode=0o700)
                    target_parent.mkdir(parents=True, mode=0o700)
                    home.chmod(0o750)
                    config_parent.chmod(0o775)
                    target_parent.chmod(0o775)
                    target = (
                        config_parent / "config.json"
                        if topology == "direct"
                        else target_parent / "config.json"
                    )
                    write_config(target, "{}")
                    target.chmod(unsafe_mode)
                    config_path = config_parent / "config.json"
                    if topology == "leaf-symlink":
                        config_path.symlink_to("../dotfiles/asha/.asha/config.json")

                    # Hide the unsafe bits from pathname inspection.  The
                    # opened descriptor's fstat remains authoritative.
                    real_lstat = Path.lstat
                    target_inode = target.stat().st_ino

                    def safe_path_metadata(path: Path):
                        metadata = real_lstat(path)
                        if metadata.st_ino == target_inode:
                            fields = list(metadata)
                            fields[0] = stat.S_IFREG | 0o600
                            return os.stat_result(fields)
                        return metadata

                    with mock.patch("pathlib.Path.lstat", new=safe_path_metadata):
                        with self.assertRaisesRegex(ConfigError, "group/world-writable"):
                            load_config({
                                "HOME": str(home),
                                "ASHA_CONFIG": str(config_path),
                                "ASHA_HOME": str(root / "asha"),
                                "XDG_RUNTIME_DIR": str(root / "runtime"),
                            })

    def test_config_allows_group_and_world_read_bits_on_owned_file(self) -> None:
        for topology in ("direct", "leaf-symlink"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                root.chmod(0o755)
                home = root / "home"
                config_parent = home / ".asha"
                target_parent = home / "dotfiles/asha/.asha"
                config_parent.mkdir(parents=True, mode=0o700)
                target_parent.mkdir(parents=True, mode=0o700)
                home.chmod(0o750)
                config_parent.chmod(0o775)
                target_parent.chmod(0o775)
                target = (
                    config_parent / "config.json"
                    if topology == "direct"
                    else target_parent / "config.json"
                )
                write_config(target, '{"default_harness":"codex"}')
                target.chmod(0o644)
                config_path = config_parent / "config.json"
                if topology == "leaf-symlink":
                    config_path.symlink_to("../dotfiles/asha/.asha/config.json")

                config = load_config({
                    "HOME": str(home),
                    "ASHA_CONFIG": str(config_path),
                    "ASHA_HOME": str(root / "asha"),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                })
                self.assertEqual(config.default_harness, "codex")

    def test_config_rejects_symlinked_input_parents_and_chained_or_foreign_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir(mode=0o700)
            real_parent = home / "real-config-parent"
            real_parent.mkdir(mode=0o700)
            write_config(real_parent / "config.json", "{}")
            (home / ".asha").symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(ConfigError, "symlink"):
                load_config({"HOME": str(home)})

        for target_shape in (
            "chained-leaf", "symlink-parent", "foreign-link", "foreign-parent",
            "foreign-file", "hard-linked-file",
        ):
            with self.subTest(target_shape=target_shape), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                root.chmod(0o755)
                home = root / "home"
                config_parent = home / ".asha"
                target_parent = home / "dotfiles/asha/.asha"
                config_parent.mkdir(parents=True, mode=0o700)
                target_parent.mkdir(parents=True, mode=0o700)
                home.chmod(0o750)
                config_parent.chmod(0o775)
                target_parent.chmod(0o775)
                real_target = target_parent / "real-config.json"
                write_config(real_target, "{}")
                config_link = config_parent / "config.json"
                if target_shape == "chained-leaf":
                    chained = target_parent / "config.json"
                    chained.symlink_to(real_target)
                    config_link.symlink_to(chained)
                elif target_shape == "symlink-parent":
                    alias = home / "dotfiles-alias"
                    alias.symlink_to(home / "dotfiles", target_is_directory=True)
                    config_link.symlink_to(alias / "asha/.asha/real-config.json")
                else:
                    config_link.symlink_to(real_target)
                    if target_shape == "hard-linked-file":
                        os.link(real_target, target_parent / "other-config.json")

                env = {
                    "HOME": str(home),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                }
                if target_shape in {"chained-leaf", "symlink-parent"}:
                    with self.assertRaisesRegex(ConfigError, "symlink"):
                        load_config(env)
                    continue

                if target_shape == "foreign-link":
                    real_lstat = Path.lstat
                    link_inode = config_link.lstat().st_ino

                    def foreign_link(path: Path):
                        metadata = real_lstat(path)
                        if metadata.st_ino == link_inode:
                            fields = list(metadata)
                            fields[4] = os.geteuid() + 1
                            return os.stat_result(fields)
                        return metadata

                    with mock.patch("pathlib.Path.lstat", new=foreign_link):
                        with self.assertRaisesRegex(ConfigError, "owned"):
                            load_config(env)
                    continue

                if target_shape == "foreign-parent":
                    real_fstat = os.fstat
                    parent_inode = target_parent.stat().st_ino

                    def foreign_parent(fd: int):
                        metadata = real_fstat(fd)
                        if metadata.st_ino == parent_inode:
                            fields = list(metadata)
                            fields[4] = os.geteuid() + 1
                            return os.stat_result(fields)
                        return metadata

                    with mock.patch("lib.control.config.os.fstat", side_effect=foreign_parent):
                        with self.assertRaisesRegex(ConfigError, "owned"):
                            load_config(env)
                    continue

                if target_shape == "hard-linked-file":
                    with self.assertRaisesRegex(ConfigError, "link count"):
                        load_config(env)
                    continue

                real_fstat = os.fstat
                target_inode = real_target.stat().st_ino

                def foreign_target(fd: int):
                    metadata = real_fstat(fd)
                    if metadata.st_ino == target_inode:
                        fields = list(metadata)
                        fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return metadata

                with mock.patch("lib.control.config.os.fstat", side_effect=foreign_target):
                    with self.assertRaisesRegex(ConfigError, "owned"):
                        load_config(env)

    def test_asha_home_must_be_absolute_and_have_no_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            with self.assertRaisesRegex(ConfigError, "ASHA_HOME.*absolute"):
                load_config({"HOME": str(home), "ASHA_HOME": "relative"})

            # A symlinked ASHA_HOME was never supported: the config file's own
            # parent guard rejects it, exactly as it rejected a symlinked
            # ~/.asha before consolidation. The dotfiles pattern is a REAL
            # .asha directory holding symlinked leaf files.
            target = root / "target"
            target.mkdir(mode=0o700)
            (target / "config.json").write_text("{}")
            link = root / "asha-link"
            link.symlink_to(target, target_is_directory=True)
            home.chmod(0o700)
            with self.assertRaisesRegex(ConfigError, "symlink component rejected in ASHA_CONFIG parent"):
                load_config({"HOME": str(home), "ASHA_HOME": str(link)})
            with self.assertRaisesRegex(ConfigError, "must not be HOME"):
                load_config({"HOME": str(home), "ASHA_HOME": str(home)})

    def test_paths_require_exact_single_slash_canonical_form(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            home.chmod(0o700)
            base = {"HOME": str(home), "ASHA_CONFIG": str(root / "missing.json")}
            for value in (
                f"//{str(root / 'state').lstrip('/')}",
                str(root / "state") + "/",
                str(root) + "/x/../state",
                str(root) + "/./state",
            ):
                with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "canonical"):
                    load_config({**base, "ASHA_HOME": value})
            with self.assertRaisesRegex(ConfigError, "canonical"):
                load_config({**base, "HOME": f"//{str(home).lstrip('/')}"})
            for config_value in (
                f"//{str(root / 'config.json').lstrip('/')}",
                str(root / "config.json") + "/",
                str(root / "x/../config.json"),
            ):
                with self.subTest(config_value=config_value), self.assertRaisesRegex(
                    ConfigError, "ASHA_CONFIG.*canonical"
                ):
                    load_config({**base, "ASHA_CONFIG": config_value})
            config_path = root / "config.json"
            for workspace in ("~/workspaces/", "~/x/../workspaces", "~/x//workspaces"):
                write_config(config_path, json.dumps({"control": {"workspace_root": workspace}}))
                with self.subTest(workspace=workspace), self.assertRaisesRegex(
                    ConfigError, "canonical"
                ):
                    load_config({
                        **base,
                        "ASHA_CONFIG": str(config_path),
                        "ASHA_HOME": str(root / "asha"),
                        "XDG_RUNTIME_DIR": str(root / "runtime"),
                    })

    def test_home_rejects_tilde_while_workspace_config_may_expand_it(self) -> None:
        for home in ("~", "~/home", "~someone/home"):
            with self.subTest(home=home), self.assertRaisesRegex(ConfigError, "HOME.*absolute"):
                load_config({"HOME": home})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir(mode=0o700)
            home.chmod(0o700)
            config_path = root / "config.json"
            write_config(config_path, '{"control":{"workspace_root":"~/workspaces"}}')
            config = load_config({
                "HOME": str(home),
                "ASHA_CONFIG": str(config_path),
                "ASHA_HOME": str(root / "asha"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            })
            self.assertEqual(config.workspace_root, home / "workspaces")

    def test_double_slash_cannot_bypass_workspace_relationship_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            repo = home / "Code/repo"
            repo.mkdir(parents=True)
            home.chmod(0o700)
            config_path = root / "config.json"
            doubled_repo = f"//{str(repo).lstrip('/')}"
            write_config(config_path, json.dumps({"control": {"workspace_root": doubled_repo}}))
            with self.assertRaisesRegex(ConfigError, "canonical"):
                load_config({"HOME": str(home), "ASHA_CONFIG": str(config_path)})

    def test_existing_configured_roots_and_intermediates_must_be_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home_file = root / "home-file"
            home_file.write_text("not a directory")
            with self.assertRaisesRegex(ConfigError, "directory"):
                load_config({"HOME": str(home_file)})

            home = root / "home"
            home.mkdir()
            home.chmod(0o700)
            blocker = root / "blocker"
            blocker.write_text("not a directory")
            with self.assertRaisesRegex(ConfigError, "directory"):
                load_config({"HOME": str(home), "ASHA_HOME": str(blocker / "asha")})

            workspace = root / "workspace-file"
            workspace.write_text("not a directory")
            config_path = root / "config.json"
            write_config(config_path, json.dumps({"control": {"workspace_root": str(workspace)}}))
            with self.assertRaisesRegex(ConfigError, "directory"):
                load_config({"HOME": str(home), "ASHA_CONFIG": str(config_path)})

    def test_empty_env_values_use_unset_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            home.chmod(0o700)
            config = load_config({
                "HOME": str(home),
                "ASHA_CONFIG": "",
                "ASHA_HOME": "",
                "XDG_RUNTIME_DIR": "",
            })
            self.assertEqual(config.asha_home, home / ".asha")
            self.assertEqual(config.config_path, home / ".asha/config.json")
            self.assertEqual(config.tasks_dir, home / ".asha/state/control/tasks")
            self.assertEqual(config.workspace_root, home / ".asha/workspaces")
            self.assertEqual(config.runtime_dir, Path(f"/tmp/user-{os.getuid()}/asha-control"))

    def test_explicit_asha_home_moves_every_derived_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            elsewhere = root / "elsewhere"
            for directory in (home, elsewhere):
                directory.mkdir(mode=0o700)
            config = load_config({"HOME": str(home), "ASHA_HOME": str(elsewhere)})
            self.assertEqual(config.asha_home, elsewhere)
            self.assertEqual(config.config_path, elsewhere / "config.json")
            self.assertEqual(config.tasks_dir, elsewhere / "state/control/tasks")
            self.assertEqual(config.workspace_root, elsewhere / "workspaces")

    def test_xdg_state_and_data_are_ignored_when_set(self) -> None:
        """The toolkit no longer consumes XDG_STATE_HOME/XDG_DATA_HOME.

        A login environment that sets them must not split the single root
        back apart: only ASHA_HOME (and, for the ephemeral runtime dir,
        XDG_RUNTIME_DIR) decides where asha's files live.
        """
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir(mode=0o700)
            config = load_config({
                "HOME": str(home),
                "XDG_STATE_HOME": str(Path(td) / "ignored-state"),
                "XDG_DATA_HOME": str(Path(td) / "ignored-data"),
            })
            self.assertEqual(config.tasks_dir, home / ".asha/state/control/tasks")
            self.assertEqual(config.workspace_root, home / ".asha/workspaces")

    def test_legacy_layout_refuses_until_migrated_and_the_bypass_works(self) -> None:
        """Un-migrated data must fail loudly, never silently re-home."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir(mode=0o700)
            legacy_control = home / ".local/state/asha/control"
            legacy_control.mkdir(parents=True, mode=0o700)
            (legacy_control / "tasks").mkdir(mode=0o700)
            with self.assertRaisesRegex(ConfigError, "legacy Asha state.*asha migrate"):
                load_config({"HOME": str(home)})
            config = load_config({"HOME": str(home)}, check_legacy=False)
            self.assertEqual(config.tasks_dir, home / ".asha/state/control/tasks")
            # An explicit ASHA_HOME is a deliberate redirection and bypasses
            # the gate: the machine's default location is not this session's.
            elsewhere = home / "elsewhere"
            elsewhere.mkdir(mode=0o700)
            load_config({"HOME": str(home), "ASHA_HOME": str(elsewhere)})
            # A banner-only legacy directory is a completed migration, not data.
            for item in ("tasks",):
                (legacy_control / item).rmdir()
            (legacy_control / "ASHA-MOVED.md").write_text("moved\n")
            load_config({"HOME": str(home)})
            # New root present alongside real legacy data: no refusal either —
            # migration is complete or in hand; the doctor reports the leftover.
            (legacy_control / "tasks").mkdir(mode=0o700)
            # mkdir(parents=True) would give the intermediates the umask mode;
            # the ancestor rule needs each level free of group/world write.
            for depth in (".asha", ".asha/state", ".asha/state/control"):
                (home / depth).mkdir(mode=0o700)
            load_config({"HOME": str(home)})

    def test_unsafe_runtime_fallback_names_the_xdg_runtime_dir_remedy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()

            def reject(path: Path, name: str) -> None:
                if name == "XDG_RUNTIME_DIR":
                    raise ConfigError("unsafe fallback runtime path")

            with mock.patch(
                "lib.control.config.reject_unsafe_writable_ancestors",
                side_effect=reject,
            ), self.assertRaisesRegex(
                ConfigError,
                r"unsafe fallback runtime path.*set XDG_RUNTIME_DIR.*private directory",
            ):
                load_config({"HOME": str(home), "XDG_RUNTIME_DIR": ""})

    def test_nonsticky_group_or_world_writable_path_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o775)
            home = root / "home"
            home.mkdir()
            with self.assertRaisesRegex(ConfigError, "writable non-sticky ancestor"):
                load_config({
                    "HOME": str(home),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                })

    def test_private_boundary_rejects_owned_group_writable_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o755)
            home = root / "home"
            private = root / "private"
            home.mkdir(mode=0o700)
            private.mkdir(mode=0o700)
            for mode in (0o770, 0o1770):
                shared = private / f"shared-{mode:o}"
                shared.mkdir(mode=mode)
                shared.chmod(mode)

                with self.subTest(mode=oct(mode)), self.assertRaisesRegex(
                    ConfigError, "writable.*ancestor",
                ):
                    load_config({
                        "HOME": str(home),
                        "ASHA_CONFIG": str(home / "missing.json"),
                        "ASHA_HOME": str(shared),
                        "XDG_RUNTIME_DIR": str(private / "runtime"),
                    })

    def test_group_readable_home_establishes_the_private_boundary(self) -> None:
        """A 0750 home must establish the boundary, not just 0700.

        The boundary answers path SUBSTITUTION, which needs write access. A
        0750 directory already denies creation and replacement to everyone but
        the owner. Requiring 0700 rejected every ordinary home layout, and with
        it every repository beneath one -- `asha task start` could not run
        against any real repository on a standard Linux machine.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o755)
            home = root / "home"
            home.mkdir(mode=0o750)          # group-readable, NOT group-writable
            shared = home / "asha"
            shared.mkdir(mode=0o750)        # group-readable, NOT group-writable

            config = load_config({
                "HOME": str(home),
                "ASHA_CONFIG": str(home / "missing.json"),
                "ASHA_HOME": str(shared),
                "XDG_RUNTIME_DIR": str(home / "runtime"),
            })
            self.assertEqual(config.tasks_dir, shared / "state/control/tasks")

    def test_group_writable_ancestor_never_establishes_the_boundary(self) -> None:
        """Writability is the line: a 0770 home must NOT confer trust."""
        metadata = type("Metadata", (), {
            "st_mode": stat.S_IFDIR | 0o770,
            "st_uid": os.geteuid(),
        })()
        problem, boundary = namespace_safety_step(metadata, os.geteuid(), False)
        self.assertEqual(problem, "writable non-sticky ancestor")
        self.assertFalse(boundary)

    def test_sticky_writable_ancestor_is_allowed_only_before_the_managed_namespace(self) -> None:
        for mode in (0o1770, 0o1777):
            metadata = type("Metadata", (), {
                "st_mode": stat.S_IFDIR | mode,
                "st_uid": os.geteuid(),
            })()
            with self.subTest(mode=oct(mode)):
                problem, boundary = namespace_safety_step(
                    metadata, os.geteuid(), False,
                )
                self.assertIsNone(problem)
                self.assertFalse(boundary)
                self.assertIn(
                    "sticky",
                    namespace_safety_step(metadata, os.geteuid(), True)[0] or "",
                )
                self.assertIn(
                    "sticky",
                    namespace_safety_step(
                        metadata, os.geteuid(), False, namespace_root=True,
                    )[0] or "",
                )

    def test_foreign_owned_namespace_ancestor_is_rejected(self) -> None:
        metadata = type("Metadata", (), {
            "st_mode": stat.S_IFDIR | 0o700,
            "st_uid": os.geteuid() + 1,
        })()
        self.assertIn(
            "owned", namespace_ancestor_problem(metadata, os.geteuid(), root_uid=0) or ""
        )

    def test_control_and_tmux_null_or_unknown_keys_are_errors_but_root_extensions_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home").mkdir()
            env = {
                "HOME": str(root / "home"),
                "ASHA_CONFIG": str(root / "config.json"),
                "ASHA_HOME": str(root / "asha"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }
            invalid = (
                {"control": None},
                {"control": {"tmux": None}},
                {"control": {"hostile\u202ekey": True}},
                {"control": {"tmux": {"hostile\u202ekey": True}}},
            )
            for value in invalid:
                write_config(root / "config.json", json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                    load_config(env)
                self.assertNotIn("hostile", str(caught.exception))
                self.assertNotIn("\u202e", str(caught.exception))
            write_config(
                root / "config.json",
                json.dumps({"unrelated": {"future": True}, "control": {}}),
            )
            self.assertEqual(load_config(env).default_harness, "claude")

    def test_config_rejects_duplicate_keys_and_oversized_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home").mkdir()
            config = root / "config.json"
            env = {"HOME": str(root / "home"), "ASHA_CONFIG": str(config)}
            write_config(config, '{"control":{},"control":{}}')
            with self.assertRaisesRegex(ConfigError, "duplicate JSON key"):
                load_config(env)
            config.write_bytes(b" " * (64 * 1024 + 1))
            with self.assertRaisesRegex(ConfigError, "exceeds"):
                load_config(env)

    def test_config_rejects_excessive_json_nesting_with_fixed_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "home").mkdir()
            config = root / "config.json"
            write_config(config, "[" * 10000 + "0" + "]" * 10000)
            with self.assertRaises(ConfigError) as caught:
                load_config({"HOME": str(root / "home"), "ASHA_CONFIG": str(config)})
            message = str(caught.exception)
            self.assertIn("nesting", message)
            self.assertNotIn("[[[", message)

    def test_workspace_root_rejects_dangerous_relationship_to_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home = root / "home"
            repo = home / "Code/project"
            repo.mkdir(parents=True)
            for directory in (home, home / "Code", repo):
                directory.chmod(0o700)
            with self.assertRaisesRegex(ConfigError, "HOME"):
                validate_workspace_root(home, home=home, repository=repo)
            with self.assertRaisesRegex(ConfigError, "ancestor"):
                validate_workspace_root(home / "Code", home=home, repository=repo)
            with self.assertRaisesRegex(ConfigError, "source repository"):
                validate_workspace_root(repo, home=home, repository=repo)
            doubled_repo = Path(f"//{str(repo).lstrip('/')}")
            with self.assertRaisesRegex(ConfigError, "canonical"):
                validate_workspace_root(root / "workspaces", home=home, repository=doubled_repo)
            self.assertEqual(
                validate_workspace_root(root / "workspaces", home=home, repository=repo),
                root / "workspaces",
            )


class ControlModelTests(unittest.TestCase):
    def test_uuid_must_be_canonical_lowercase_hyphenated(self) -> None:
        value = str(uuid.uuid4())
        self.assertEqual(canonical_uuid(value), value)
        generated = new_uuid()
        self.assertEqual(canonical_uuid(generated), generated)
        for invalid in (value.upper(), value.replace("-", ""), "not-a-uuid"):
            with self.subTest(invalid=invalid), self.assertRaises(ModelError):
                canonical_uuid(invalid)

    def test_slug_uses_restricted_bounded_grammar(self) -> None:
        for valid in ("a", "thallus-pr-34", "a" * 64):
            self.assertEqual(validate_slug(valid), valid)
        for invalid in ("", "Upper", "has space", "-leading", "trailing-", "a" * 65, "../escape"):
            with self.subTest(invalid=invalid), self.assertRaises(ModelError):
                validate_slug(invalid)

    def test_task_and_run_transition_graphs_are_explicit_and_separate(self) -> None:
        self.assertEqual(TASK_LIFECYCLE_TRANSITIONS["creating"], frozenset({"running", "failed"}))
        self.assertEqual(TASK_LIFECYCLE_TRANSITIONS["running"], frozenset({"ended", "failed"}))
        self.assertNotIn("working", TASK_LIFECYCLE_TRANSITIONS)
        self.assertIn("working", RUN_STATE_TRANSITIONS["starting"])
        self.assertNotIn("archived", RUN_STATE_TRANSITIONS)
        self.assertEqual(RUN_STATE_TRANSITIONS["exited"], frozenset())
        self.assertEqual(RUN_STATE_TRANSITIONS["failed"], frozenset())
        # 2026-08-18 amendment: a failed task without a live run may be archived
        # out of the working list, and unarchive restores failed or ended.
        self.assertEqual(TASK_LIFECYCLE_TRANSITIONS["failed"], frozenset({"archived"}))
        self.assertEqual(TASK_LIFECYCLE_TRANSITIONS["archived"], frozenset({"ended", "failed"}))
        for state in ("working", "needs-input", "idle", "unknown"):
            self.assertNotIn("starting", RUN_STATE_TRANSITIONS[state])
        self.assertIn("starting", RUN_STATE_TRANSITIONS["stale"])

    def test_every_task_and_run_state_pair_obeys_only_the_declared_graph(self) -> None:
        for current, legal in TASK_LIFECYCLE_TRANSITIONS.items():
            for requested in TASK_LIFECYCLE_TRANSITIONS:
                with self.subTest(kind="task", current=current, requested=requested):
                    if requested in legal:
                        require_task_transition(current, requested)
                    else:
                        with self.assertRaises(ModelError):
                            require_task_transition(current, requested)
        for current, legal in RUN_STATE_TRANSITIONS.items():
            for requested in RUN_STATE_TRANSITIONS:
                with self.subTest(kind="run", current=current, requested=requested):
                    if requested in legal:
                        require_run_transition(current, requested)
                    else:
                        with self.assertRaises(ModelError):
                            require_run_transition(current, requested)

    def test_minimum_versioned_task_record_validates(self) -> None:
        record = task_record()
        self.assertEqual(validate_task(record), record)

    def test_jj_identities_require_full_lowercase_object_and_change_ids(self) -> None:
        record = task_record()
        for field, invalid in (
            ("base_commit_id", "a" * 12),
            ("base_commit_id", "A" * 40),
            ("working_commit_id", "c" * 39),
            ("working_commit_id", "g" * 40),
            ("change_id", "k" * 12),
            ("change_id", "a" * 32),
            ("change_id", "k" * 31 + "-"),
        ):
            changed = json.loads(json.dumps(record))
            changed["jj"][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ModelError):
                validate_task(changed)
        record["jj"]["base_commit_id"] = "a" * 64
        record["jj"]["working_commit_id"] = "c" * 64
        self.assertEqual(validate_task(record), record)

    def test_creating_record_can_predeclare_paths_before_jj_or_run_identity_exists(self) -> None:
        record = task_record()
        record["lifecycle"] = "creating"
        record["jj"]["change_id"] = None
        record["jj"]["working_commit_id"] = None
        record["runs"] = []
        self.assertEqual(validate_task(record), record)

        record["lifecycle"] = "running"
        with self.assertRaisesRegex(ModelError, "running task"):
            validate_task(record)

    def test_record_rejects_unknown_fields_noncanonical_ids_and_bad_run_states(self) -> None:
        record = task_record()
        record["prompt"] = "must never be stored"
        with self.assertRaisesRegex(ModelError, "unexpected"):
            validate_task(record)

        record = task_record(task_id=str(uuid.uuid4()).upper())
        with self.assertRaisesRegex(ModelError, "canonical UUID"):
            validate_task(record)

        record = task_record()
        record["runs"][0]["state"] = "review-ready"
        with self.assertRaisesRegex(ModelError, "run state"):
            validate_task(record)

    def test_persisted_text_rejects_unicode_control_format_and_surrogate_characters(self) -> None:
        for forbidden in ("\u009b", "\u202e", "\ud800"):
            record = task_record()
            record["label"] = f"unsafe{forbidden}text"
            with self.subTest(forbidden=repr(forbidden)), self.assertRaises(ModelError):
                validate_task(record)

    def test_timestamps_use_exact_bounded_ascii_rfc3339_utc_grammar(self) -> None:
        invalid = (
            "2026-08-14 18:00:00Z",
            "2026-08-14T18:00:00.1234567Z",
            "2026-08-14T18:00:00Z\n",
            "2026-08-14\u009b18:00:00Z",
            "2026-08-14\u202e18:00:00Z",
            "2026-08-14\ud80018:00:00Z",
        )
        for timestamp in invalid:
            record = task_record()
            record["created_at"] = timestamp
            with self.subTest(timestamp=repr(timestamp)), self.assertRaises(ModelError):
                validate_task(record)
        record = task_record()
        record["created_at"] = "2026-08-14T18:00:00.123456Z"
        self.assertEqual(validate_task(record), record)

    def test_schema_errors_never_echo_untrusted_object_keys(self) -> None:
        record = task_record()
        record["raw-secret\u202e"] = "x"
        with self.assertRaises(ModelError) as caught:
            validate_task(record)
        message = str(caught.exception)
        self.assertNotIn("raw-secret", message)
        self.assertNotIn("\u202e", message)

    def test_lifecycle_requires_coherent_run_history(self) -> None:
        base = task_record()
        cases: list[tuple[str, dict, bool]] = []

        creating = json.loads(json.dumps(base))
        creating["lifecycle"] = "creating"
        creating["jj"]["change_id"] = None
        creating["jj"]["working_commit_id"] = None
        creating["runs"] = []
        cases.append(("creating-empty", creating, True))
        creating_with_run = json.loads(json.dumps(creating))
        creating_with_run["runs"] = json.loads(json.dumps(base["runs"]))
        cases.append(("creating-run", creating_with_run, False))

        cases.append(("running-active", base, True))
        running_terminal = json.loads(json.dumps(base))
        running_terminal["runs"][0]["state"] = "exited"
        cases.append(("running-terminal", running_terminal, False))
        running_history = json.loads(json.dumps(base))
        previous = json.loads(json.dumps(base["runs"][0]))
        previous["run_id"] = str(uuid.uuid4())
        previous["state"] = "exited"
        running_history["runs"].insert(0, previous)
        cases.append(("running-terminal-history", json.loads(json.dumps(running_history)), True))
        running_history["runs"][0]["state"] = "idle"
        cases.append(("running-active-history", running_history, False))

        for lifecycle in ("ended", "archived"):
            terminal = json.loads(json.dumps(base))
            terminal["lifecycle"] = lifecycle
            terminal["runs"][0]["state"] = "exited"
            cases.append((f"{lifecycle}-exited", terminal, True))
            terminal_failed = json.loads(json.dumps(terminal))
            terminal_failed["runs"][0]["state"] = "failed"
            cases.append((f"{lifecycle}-failed", terminal_failed, True))

        failed_empty = json.loads(json.dumps(creating))
        failed_empty["lifecycle"] = "failed"
        cases.append(("failed-prelaunch", failed_empty, True))
        failed_run = json.loads(json.dumps(base))
        failed_run["lifecycle"] = "failed"
        failed_run["runs"][0]["state"] = "failed"
        cases.append(("failed-terminal", failed_run, True))
        failed_live = json.loads(json.dumps(base))
        failed_live["lifecycle"] = "failed"
        failed_live["runs"][0]["state"] = "working"
        cases.append(("failed-postlaunch-live", failed_live, True))
        failed_all_exited = json.loads(json.dumps(failed_run))
        failed_all_exited["runs"][0]["state"] = "exited"
        cases.append(("failed-controller-with-successful-run", failed_all_exited, True))
        failed_without_identity = json.loads(json.dumps(failed_run))
        failed_without_identity["jj"]["change_id"] = None
        failed_without_identity["jj"]["working_commit_id"] = None
        cases.append(("failed-run-without-identity", failed_without_identity, False))

        for name, record, valid in cases:
            with self.subTest(name=name):
                if valid:
                    self.assertEqual(validate_task(record), record)
                else:
                    with self.assertRaises(ModelError):
                        validate_task(record)

    def test_run_evidence_timestamps_are_within_task_chronology(self) -> None:
        for timestamp in ("2026-08-14T18:00:00Z", "2026-08-14T18:00:01Z"):
            record = task_record()
            record["runs"][0]["evidence_at"] = timestamp
            self.assertEqual(validate_task(record), record)
        for timestamp in ("2026-08-14T17:59:59Z", "2026-08-14T18:00:02Z"):
            record = task_record()
            record["runs"][0]["evidence_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(ModelError, "chronology"):
                validate_task(record)

    def test_persisted_paths_reject_lexical_and_symlink_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            for field, value in (
                (("repository", "root"), f"//{str(root / 'repo').lstrip('/')}"),
                (("repository", "root"), str(root / "repo") + "/"),
                (("jj", "workspace_path"), str(root / "x/../workspace")),
                (("jj", "workspace_path"), str(alias / "workspace")),
            ):
                record = task_record()
                record[field[0]][field[1]] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ModelError, "canonical"):
                    validate_task(record)


if __name__ == "__main__":
    unittest.main()
