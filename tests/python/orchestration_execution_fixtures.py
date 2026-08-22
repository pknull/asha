from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lib.control.jj import ImmutableTree, RepositoryFacts
from lib.control.orchestration.cli import _approve, _create, _plan
from lib.control.orchestration.config import load_config
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.store import InitiativeStore
from tests.python.test_control_config_model import task_record
from tests.python.test_orchestration_graph import valid_plan


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class ExecutionFixture:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = {
            "HOME": str(self.root / "home"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        self.repo = self.root / "repo"
        (self.repo / ".asha").mkdir(parents=True)
        (self.repo / "Memory").mkdir()
        (self.repo / "Work/session-state").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / ".asha/config.json").write_text(json.dumps({
            "initialized": True,
            "memory_version": 2,
            "project_id": "orchestration-execution",
        }) + "\n")
        (self.repo / "Memory/activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        )
        (self.repo / "Memory/decisions.md").write_text("# Decisions\n\n- One.\n")
        self.config = load_config(self.env)
        self.store = InitiativeStore(self.config)
        jj = mock.Mock()
        jj.preflight.return_value = RepositoryFacts(
            root=self.repo, git_root=self.repo / ".git"
        )
        jj.immutable_tree.return_value = ImmutableTree(
            commit_id="b" * 40, digest="c" * 64, entries=(),
        )
        initiative = _create([
            "--repo", str(self.repo), "--slug", "execution-test",
            "--label", "Execution test", "--objective", "Execute one node.",
        ], self.config, self.store, jj)["initiative"]
        plan_value = valid_plan()
        plan_value["initiative_id"] = initiative["initiative_id"]
        plan_value["repositories"] = [copy.deepcopy(initiative["scope"]["repository"])]
        repository_id = initiative["scope"]["repository"]["repository_id"]
        for node in plan_value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(plan_value))
        plan, _ = _plan(
            [initiative["initiative_id"], "--file", str(plan_file)],
            self.store, self.config, jj=jj,
        )
        approved, _ = _approve(
            [initiative["initiative_id"], "--digest", plan["digest"]], self.store,
        )
        self.initiative_id = initiative["initiative_id"]
        self.plan = plan
        if getattr(self, "start_running", True):
            self.set_running(approved["initiative"])

    def set_running(self, approved: dict) -> None:
        running = copy.deepcopy(approved)
        running.update({
            "state": "running",
            "state_revision": approved["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(running, expected_digest=record_digest(approved))
        for node in self.store.list_nodes_snapshot(self.initiative_id):
            changed = copy.deepcopy(node)
            changed["state"] = "ready" if node["node_id"] == "implementation-a" else "blocked"
            self.store.save_node(
                self.initiative_id, changed, expected_digest=record_digest(node),
            )

    def initiative(self) -> dict:
        return self.store.peek(self.initiative_id)

    def install_historical_active_plan(self) -> tuple[dict, bytes]:
        """Replace the fixture plan with the retained Increment 1 gate shape."""
        retained = copy.deepcopy(self.plan)
        for gate in retained["declared_gates"]:
            if gate["kind"] == "verification":
                gate.pop("commands")
                gate.pop("environment_policy")
        content = dict(retained)
        content.pop("digest")
        content.pop("status")
        retained["digest"] = hashlib.sha256(json.dumps(
            content, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        raw = json.dumps(
            retained, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode() + b"\n"
        path = (
            self.config.initiatives_dir / self.initiative_id / "plans" / "0001.json"
        )
        path.write_bytes(raw)
        path.chmod(0o600)
        initiative = self.initiative()
        changed = copy.deepcopy(initiative)
        changed["active_plan"]["digest"] = retained["digest"]
        changed.update({
            "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(
            changed, expected_digest=record_digest(initiative),
        )
        self.plan = retained
        return retained, raw

    def control_payload(self, argv: list[str], *, existing: bool = False) -> dict:
        task_id = argv[argv.index("--task-id") + 1]
        attempt_id = Path(argv[argv.index("--goal") + 1].rsplit(" ", 1)[1]).stem
        task = task_record(
            task_id=task_id,
            repository_root=str(self.repo),
            workspace_path=str(self.root / "workspaces" / task_id),
        )
        task["label"] = f"assignment {attempt_id}"
        task["jj"]["requested_base"] = argv[argv.index("--base") + 1]
        task["runs"][0]["harness"] = argv[argv.index("--harness") + 1]
        task["runs"][0]["role"] = argv[argv.index("--role") + 1]
        return {
            "contract": "asha.control-task-start.v1",
            "task": task,
            "run": task["runs"][0],
            "workspace": {
                "name": task["jj"]["workspace_name"],
                "path": task["jj"]["workspace_path"],
                "change_id": task["jj"]["change_id"],
            },
            "session": task["tmux"]["session"],
            "pane": task["runs"][0]["pane_id"],
            "attach": "tmux attach",
            "source_mutations": [],
            "existing": existing,
        }
