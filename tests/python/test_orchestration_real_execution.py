from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lib.control.jj import JjAdapter
from lib.control.prepare import prepare_materialization
from lib.control.orchestration.config import load_config
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.store import InitiativeStore
from lib.control.orchestration.review import tracked_workspace_status
from lib.control.store import TaskStore
from tests.python.orchestration_execution_fixtures import now_text
from tests.python.test_orchestration_graph import graph_node, valid_plan


class RealOrchestrationExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [name for name in ("git", "jj", "tmux") if shutil.which(name) is None]
        if missing:
            raise unittest.SkipTest("missing real integration tools: " + ", ".join(missing))
        probe = f"asha-orchestration-probe-{os.getpid()}"
        started = subprocess.run(
            ["tmux", "new-session", "-d", "-s", probe, "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False,
        )
        if started.returncode != 0:
            raise unittest.SkipTest(
                "tmux is unavailable in this execution environment: "
                + started.stderr.strip()
            )
        subprocess.run(
            ["tmux", "kill-session", "-t", probe],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project_root = Path(__file__).resolve().parents[2]
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir(mode=0o700)
        self.stub_asha_root = self.root / "asha-root"
        (self.stub_asha_root / "bin").mkdir(parents=True, mode=0o700)
        self.env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "TMUX_TMPDIR": str(self.root / "tmux"),
            "ASHA_CONFIG": str(self.root / "config.json"),
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(self.project_root),
            "ASHA_ROOT": str(self.stub_asha_root),
        }
        for key in (
            "HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
            "TMUX_TMPDIR",
        ):
            Path(self.env[key]).mkdir(mode=0o700)
        self.session_prefix = f"o2b-{os.getpid()}-"
        Path(self.env["ASHA_CONFIG"]).write_text(json.dumps({
            "control": {
                "default_harness": "codex",
                "tmux": {"session_prefix": self.session_prefix},
            },
            "orchestration": {
                "contract": "asha.orchestration-config.v1",
                "max_attempts_per_node": 2,
                "result_grace_seconds": 1,
            },
        }))
        Path(self.env["ASHA_CONFIG"]).chmod(0o600)
        self.repo = self.root / "repo"
        self.repo.mkdir(mode=0o755)
        self._initialize_repository()
        self._write_stub_harness()
        self.config = load_config(self.env)
        self.addCleanup(self._kill_control_sessions)

    def _run_command(
        self, argv: list[str], *, cwd: Path | None = None, check: bool = True
    ):
        result = subprocess.run(
            argv, cwd=cwd, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=90,
        )
        if check and result.returncode != 0:
            self.fail(
                f"command failed ({result.returncode}): {argv!r}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def asha_json(self, *args: str) -> dict:
        result = self._run_command(
            [sys.executable, "-m", "lib.control.cli", *args]
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"Asha returned non-JSON for {args!r}: {result.stdout!r}\n{result.stderr}")
            raise exc

    def _initialize_repository(self) -> None:
        (self.repo / ".asha").mkdir()
        (self.repo / "Memory").mkdir()
        (self.repo / "Work/session-state").mkdir(parents=True)
        (self.repo / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "orchestration-real-execution",
        }) + "\n")
        (self.repo / "Memory/activeContext.md").write_text(
            "# Objective\n\nIntegration.\n\n# State\n\nReady.\n\n"
            "# Next\n\n- Run.\n\n# Blockers\n\n- None.\n"
        )
        (self.repo / "Memory/decisions.md").write_text("# Decisions\n\n- Test.\n")
        (self.repo / ".gitignore").write_text(
            ".asha/config.json\n.asha/control-task.json\nMemory/activeContext.md\n"
            "Memory/decisions.md\nWork/session-state/.asha-control-probe\n"
            "__pycache__/\n"
        )
        (self.repo / "seed.txt").write_text("base\n")
        (self.repo / "odd | name.txt").write_text("quoted fileset path\n")
        (self.repo / "target-a.txt").write_text("A\n")
        (self.repo / "target-b.txt").write_text("B\n")
        (self.repo / "outside-link.txt").symlink_to("target-a.txt")
        self._run_command(["git", "init", "-q"], cwd=self.repo)
        self._run_command(["git", "config", "user.email", "integration@example.invalid"], cwd=self.repo)
        self._run_command(["git", "config", "user.name", "Asha Integration"], cwd=self.repo)
        self._run_command(
            [
                "git", "add", ".gitignore", "seed.txt", "odd | name.txt",
                "target-a.txt", "target-b.txt", "outside-link.txt",
            ], cwd=self.repo,
        )
        self._run_command(["git", "commit", "-q", "-m", "base"], cwd=self.repo)
        self._run_command(["jj", "git", "init", "--colocate", "."], cwd=self.repo)
        self._run_command(["jj", "status"], cwd=self.repo)
        self.base_commit = self._run_command(
            ["git", "rev-parse", "HEAD"], cwd=self.repo
        ).stdout.strip()
        self.base_tree_digest = JjAdapter().immutable_tree(self.repo, self.base_commit).digest

    def _write_stub_harness(self) -> None:
        script = self.fake_bin / "codex"
        script.write_text(r'''#!/usr/bin/env bash
set -euo pipefail
assignment=""
for argument in "$@"; do
  case "$argument" in *"/assignments/"*.md) assignment="${argument##* }" ;; esac
done
[[ -n "$assignment" && -f "$assignment" ]]
mode="$(awk '/^## Node goal/{seen=1; next} seen && NF {print; exit}' "$assignment")"
initiative="$(sed -n 's/^- Initiative: .* (\([0-9a-f-]*\))$/\1/p' "$assignment")"
node="$(sed -n 's/^- Node: //p' "$assignment")"
attempt="$(sed -n 's/^- Attempt: //p' "$assignment")"
case "$mode" in
  IN_SCOPE*) path="scope/inside.txt" ;;
  OUT_SCOPE*) path="outside/escaped.txt" ;;
  EXIT_ONE*) path="scope/nonzero.txt" ;;
  NO_SNAPSHOT*) path="scope/unsnapped.txt" ;;
  SYMLINK_OUT*) path="scope/symlink-worker.txt" ;;
  PARALLEL_A*) path="scope/a.txt" ;;
  PARALLEL_B*) path="scope/b.txt" ;;
  COMPOSE*) path="" ;;
  REVIEW*) path="" ;;
  *) exit 89 ;;
esac
if [[ "$mode" == COMPOSE* ]]; then
  python3 -I - "$assignment" <<'COMPOSEPY'
import json, pathlib, subprocess, sys
assignment = pathlib.Path(sys.argv[1]).read_text()
commits = []
for line in assignment.splitlines():
    if not line.startswith("- {"):
        continue
    try:
        value = json.loads(line[2:])
    except json.JSONDecodeError:
        continue
    commit = value.get("jj_commit_id")
    if commit and commit not in commits:
        commits.append(commit)
for target in ("scope/a.txt", "scope/b.txt"):
    for commit in commits:
        result = subprocess.run(
            ["jj", "file", "show", "-r", commit, target],
            capture_output=True, check=False,
        )
        if result.returncode == 0:
            path = pathlib.Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.stdout)
            break
    else:
        raise SystemExit(f"ordered composition input omitted {target}")
COMPOSEPY
elif [[ "$mode" != REVIEW* ]]; then
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$mode" >"$path"
fi
if [[ "$mode" == PARALLEL_A* || "$mode" == PARALLEL_B* ]]; then
  sleep 2
fi
if [[ "$mode" == SYMLINK_OUT* ]]; then
  ln -sfn target-b.txt outside-link.txt
fi
if [[ "$mode" != NO_SNAPSHOT* && "$mode" != REVIEW* ]]; then
  jj status >/dev/null
fi
result_file="$XDG_RUNTIME_DIR/result-$ASHA_CONTROL_TASK_ID.json"
python3 -I - "$result_file" "$initiative" "$node" "$attempt" "$mode" "$path" "$assignment" <<'RESULTPY'
import datetime, json, os, pathlib, sys, uuid
path, initiative, node, attempt, mode, changed, assignment = sys.argv[1:]
if mode.startswith("COMPOSE"):
    changed_files = ["scope/a.txt", "scope/b.txt"]
elif mode.startswith("REVIEW"):
    changed_files = []
else:
    changed_files = [changed]
body = {
    "contract": "asha.orchestration-result.v1",
    "publication_id": str(uuid.uuid4()), "supersedes_result_id": None,
    "initiative_id": initiative, "node_id": node, "attempt_id": attempt,
    "task_id": os.environ["ASHA_CONTROL_TASK_ID"],
    "run_id": os.environ["ASHA_CONTROL_RUN_ID"],
    "claim_status": "completed", "summary": "stub completed claim",
    "files_changed": changed_files, "verification_attestations": [],
    "concerns": [], "follow_up": [],
    "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z"),
}
if mode.startswith("REVIEW"):
    targets = []
    for line in pathlib.Path(assignment).read_text().splitlines():
        if not line.startswith("{") or "specification_digest" not in line:
            continue
        try:
            targets.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if len(targets) != 1:
        raise SystemExit("review assignment has no unique exact target")
    body["review"] = {"verdict": "pass", "findings": [], "target": targets[0]}
pathlib.Path(path).write_text(json.dumps(body))
RESULTPY
"''' + str(self.stub_asha_root / "bin/asha") + r'''" task report --file "$result_file" --json \
  >"$XDG_RUNTIME_DIR/receipt-$ASHA_CONTROL_TASK_ID.json"
[[ "$mode" != EXIT_ONE* ]]
''')
        script.chmod(0o700)
        launcher = self.stub_asha_root / "bin/asha"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ ${1:-} == codex ]]; then\n"
            "  shift\n"
            f"  exec {script} \"$@\"\n"
            "fi\n"
            f"export HOME={self.env['HOME']}\n"
            f"export XDG_STATE_HOME={self.env['XDG_STATE_HOME']}\n"
            f"export XDG_DATA_HOME={self.env['XDG_DATA_HOME']}\n"
            f"export XDG_RUNTIME_DIR={self.env['XDG_RUNTIME_DIR']}\n"
            f"export ASHA_CONFIG={self.env['ASHA_CONFIG']}\n"
            f"export PYTHONPATH={self.project_root}\n"
            f"exec {sys.executable} -m lib.control.cli \"$@\"\n"
        )
        launcher.chmod(0o700)

    def _kill_control_sessions(self) -> None:
        try:
            tasks = TaskStore(self.config.control).list()
        except Exception:
            return
        for task in tasks:
            subprocess.run(
                ["tmux", "kill-session", "-t", task["tmux"]["session"]],
                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )

    def _create_running_initiative(self, mode: str) -> str:
        created = self.asha_json(
            "initiative", "create", "--repo", str(self.repo),
            "--slug", f"real-{mode.lower().replace('_', '-')}",
            "--label", f"Real {mode}", "--objective", f"{mode} execution",
            "--acceptance", "The stub publishes a bound terminal result.",
            "--max-parallel", "1", "--max-total-tasks", "3",
            "--max-attempts-per-node", "1", "--max-repair-cycles", "1", "--json",
        )["initiative"]
        plan = valid_plan()
        plan["initiative_id"] = created["initiative_id"]
        plan["repositories"] = [copy.deepcopy(created["scope"]["repository"])]
        plan["limits"] = copy.deepcopy(created["limits"])
        repository_id = created["scope"]["repository"]["repository_id"]
        origin = {"jj_commit_id": self.base_commit, "tree_digest": self.base_tree_digest}
        for node in plan["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        writer = plan["nodes"][0]
        writer["goal"] = mode
        writer["base"]["scope_origin"] = copy.deepcopy(origin)
        writer["hard_write_scope"] = ["scope"]
        writer["advisory_path_ownership"] = ["scope"]
        plan_path = self.root / f"plan-{mode}.json"
        plan_path.write_text(json.dumps(plan))
        proposed = self.asha_json(
            "initiative", "plan", created["initiative_id"],
            "--file", str(plan_path), "--json",
        )
        self.asha_json(
            "initiative", "approve", created["initiative_id"],
            "--digest", proposed["digest"], "--json",
        )
        store = InitiativeStore(self.config)
        approved = store.peek(created["initiative_id"])
        running = copy.deepcopy(approved)
        running.update({
            "state": "running", "state_revision": approved["state_revision"] + 1,
            "updated_at": now_text(),
        })
        store.save_initiative(running, expected_digest=record_digest(approved))
        for node in store.list_nodes_snapshot(created["initiative_id"]):
            changed = copy.deepcopy(node)
            changed["state"] = "ready" if node["node_id"] == "implementation-a" else "blocked"
            store.save_node(
                created["initiative_id"], changed, expected_digest=record_digest(node),
            )
        return created["initiative_id"]

    def _dispatch_and_wait_for_seal(self, mode: str) -> tuple[dict, dict, dict]:
        initiative_id = self._create_running_initiative(mode)
        action = self.asha_json(
            "initiative", "dispatch", initiative_id,
            "--node", "implementation-a", "--json",
        )
        outcome = json.loads(action["outcome"])
        deadline = time.monotonic() + 8
        last = None
        while time.monotonic() < deadline:
            last = self.asha_json("initiative", "reconcile", initiative_id, "--json")
            store = InitiativeStore(self.config)
            seals = store.list_seals_snapshot(initiative_id)
            if seals:
                return (
                    store.peek(initiative_id),
                    store.read_node(initiative_id, "implementation-a"),
                    seals[0],
                )
            time.sleep(0.2)
        task = TaskStore(self.config.control).read(outcome["control_task_id"])
        run = task["runs"][0]
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", run["pane_id"]],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        try:
            process_argv = Path(f"/proc/{run['pid']}/cmdline").read_bytes().replace(
                b"\0", b" "
            ).decode("utf-8", errors="replace")
        except OSError as exc:
            process_argv = f"unavailable: {exc}"
        self.fail(
            f"timed out waiting for {mode} seal task={outcome['control_task_id']} "
            f"attempt={outcome['attempt_id']}: {last}\n"
            f"task={task}\nprocess={process_argv}\npane={pane.stdout}\n{pane.stderr}"
        )

    def _create_composition_initiative(self) -> str:
        created = self.asha_json(
            "initiative", "create", "--repo", str(self.repo),
            "--slug", "real-composition", "--label", "Real composition",
            "--objective", "Compose two exact parallel candidates and gate the result.",
            "--acceptance", "The composed candidate passes review and controller verification.",
            "--max-parallel", "2", "--max-total-tasks", "5",
            "--max-attempts-per-node", "1", "--max-repair-cycles", "1", "--json",
        )["initiative"]
        repository_id = created["scope"]["repository"]["repository_id"]
        origin = {"jj_commit_id": self.base_commit, "tree_digest": self.base_tree_digest}
        writer_a = graph_node("parallel-a", "work", [])
        writer_b = graph_node("parallel-b", "work", [])
        composer = graph_node(
            "compose-candidate", "compose", ["parallel-a", "parallel-b"], terminal=True,
        )
        review = graph_node("review-composed", "review", ["compose-candidate"])
        verify = graph_node("verify-composed", "verify", ["review-composed"])
        writer_a["goal"], writer_b["goal"] = "PARALLEL_A", "PARALLEL_B"
        composer["goal"], review["goal"] = "COMPOSE", "REVIEW"
        for node in (writer_a, writer_b, composer, review, verify):
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        for node in (writer_a, writer_b, composer):
            node["base"]["scope_origin"] = copy.deepcopy(origin)
            node["hard_write_scope"] = ["scope"]
            node["advisory_path_ownership"] = ["scope"]
        composer["base"]["policy"] = "upstream-seal"
        composer["base"]["upstream_node_ids"] = ["parallel-a", "parallel-b"]
        plan = valid_plan()
        plan.update({
            "initiative_id": created["initiative_id"],
            "repositories": [copy.deepcopy(created["scope"]["repository"])],
            "limits": copy.deepcopy(created["limits"]),
            "nodes": [writer_a, writer_b, composer, review, verify],
            "declared_gates": [
                {"kind": "review", "node_id": "review-composed", "required": True},
                {
                    "kind": "verification", "node_id": "verify-composed", "required": True,
                    "environment_policy": "minimal",
                    "commands": [{
                        "argv": [
                            sys.executable, "-c",
                            "from pathlib import Path; "
                            "Path('__pycache__').mkdir(exist_ok=True); "
                            "Path('__pycache__/gate.pyc').write_bytes(b'ignored'); "
                            "assert Path('scope/a.txt').is_file() and Path('scope/b.txt').is_file()",
                        ],
                        "cwd": ".", "timeout_seconds": 30,
                    }],
                },
            ],
        })
        plan_path = self.root / "plan-composition.json"
        plan_path.write_text(json.dumps(plan))
        proposed = self.asha_json(
            "initiative", "plan", created["initiative_id"],
            "--file", str(plan_path), "--json",
        )
        self.asha_json(
            "initiative", "approve", created["initiative_id"],
            "--digest", proposed["digest"], "--json",
        )
        store = InitiativeStore(self.config)
        approved = store.peek(created["initiative_id"])
        running = copy.deepcopy(approved)
        running.update({
            "state": "running", "state_revision": approved["state_revision"] + 1,
            "updated_at": now_text(),
        })
        store.save_initiative(running, expected_digest=record_digest(approved))
        for node in store.list_nodes_snapshot(created["initiative_id"]):
            changed = copy.deepcopy(node)
            changed["state"] = "ready" if node["node_id"] in {"parallel-a", "parallel-b"} else "blocked"
            store.save_node(
                created["initiative_id"], changed, expected_digest=record_digest(node),
            )
        return created["initiative_id"]

    def _wait_for_node_states(
        self, initiative_id: str, expected: dict[str, str], *, timeout: float = 15,
    ) -> InitiativeStore:
        deadline = time.monotonic() + timeout
        last = None
        store = InitiativeStore(self.config)
        while time.monotonic() < deadline:
            last = self.asha_json("initiative", "reconcile", initiative_id, "--json")
            if all(
                store.read_node(initiative_id, node_id)["state"] == state
                for node_id, state in expected.items()
            ):
                return store
            time.sleep(0.2)
        self.fail(
            f"timed out waiting for node states {expected}: {last}\n"
            f"retained orchestration records:\n"
            f"{self._retained_diagnostics(initiative_id)}\n"
            f"captured worker pane stdout/stderr:\n"
            f"{self._captured_worker_panes(initiative_id)}"
        )

    def _retained_diagnostics(self, initiative_id: str) -> str:
        store = InitiativeStore(self.config)
        value = {
            "initiative": store.peek(initiative_id),
            "nodes": store.list_nodes_snapshot(initiative_id),
            "attempts": store.list_attempts_snapshot(initiative_id),
            "reviews": store.list_reviews_snapshot(initiative_id),
            "results": store.list_results_snapshot(initiative_id),
            "events": store.list_events_snapshot(initiative_id),
        }
        return json.dumps(value, indent=2, sort_keys=True)

    def _captured_worker_panes(self, initiative_id: str) -> str:
        store = InitiativeStore(self.config)
        diagnostics: list[str] = []
        for attempt in store.list_attempts_snapshot(initiative_id):
            try:
                task = TaskStore(self.config.control).read(attempt["task_id"])
                run = task["runs"][0]
                pane = subprocess.run(
                    [
                        "tmux", "capture-pane", "-p", "-S", "-",
                        "-t", run["pane_id"],
                    ],
                    env=self.env, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False,
                )
                diagnostics.append(
                    f"attempt={attempt['attempt_id']} task={attempt['task_id']} "
                    f"pane={run['pane_id']} rc={pane.returncode}\n"
                    f"{pane.stdout}\n{pane.stderr}"
                )
            except Exception as exc:
                diagnostics.append(
                    f"attempt={attempt['attempt_id']} task={attempt['task_id']}: "
                    f"pane capture failed: {exc}"
                )
        return "\n".join(diagnostics) or "no attempts were retained"

    def test_real_control_result_exit_scope_and_dependent_release(self) -> None:
        initiative, node, seal = self._dispatch_and_wait_for_seal("IN_SCOPE")
        self.assertEqual(seal["outcome"], "success")
        self.assertEqual(node["state"], "succeeded")
        store = InitiativeStore(self.config)
        self.assertEqual(len(store.list_results_snapshot(initiative["initiative_id"])), 1)
        self.assertEqual(len(store.list_seals_snapshot(initiative["initiative_id"])), 1)
        self.assertEqual(
            store.read_node(initiative["initiative_id"], "review-a")["state"], "ready",
        )
        self.assertRegex(seal["jj_commit_id"], r"^[0-9a-f]{40,64}$")
        self.assertRegex(seal["tree_digest"], r"^[0-9a-f]{64}$")
        self.assertIn("scope/inside.txt", seal["changed_paths"])

        _, outside_node, outside = self._dispatch_and_wait_for_seal("OUT_SCOPE")
        self.assertEqual(outside["outcome"], "failure")
        self.assertEqual(outside_node["state"], "failed")
        self.assertIn("outside/escaped.txt", outside["cumulative_changed_paths"])

        _, nonzero_node, nonzero = self._dispatch_and_wait_for_seal("EXIT_ONE")
        self.assertEqual(nonzero["outcome"], "failure")
        self.assertEqual(nonzero_node["state"], "failed")
        self.assertNotEqual(nonzero["outcome"], "success")

    def test_real_jj_unsnapshotted_claim_and_symlink_retarget_cannot_succeed(self) -> None:
        _, unsnapped_node, unsnapped = self._dispatch_and_wait_for_seal("NO_SNAPSHOT")
        self.assertEqual(unsnapped["outcome"], "failure")
        self.assertEqual(unsnapped_node["state"], "failed")
        evidence = InitiativeStore(self.config).read_evidence(
            unsnapped["initiative_id"], unsnapped["process_evidence_id"],
        )
        self.assertEqual(
            json.loads(evidence["summary"])["claimed-but-unsealed"],
            ["scope/unsnapped.txt"],
        )

        _, symlink_node, symlink = self._dispatch_and_wait_for_seal("SYMLINK_OUT")
        self.assertEqual(symlink["outcome"], "failure")
        self.assertEqual(symlink_node["state"], "failed")
        self.assertIn("outside-link.txt", symlink["cumulative_changed_paths"])

    def test_parallel_compose_review_controller_verify_reaches_readiness_without_integration(self) -> None:
        source_head = self._run_command(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
        ).stdout.strip()
        source_bookmarks = self._run_command(
            [
                "jj", "--ignore-working-copy", "bookmark", "list", "-T",
                'name ++ " " ++ normal_target.commit_id() ++ "\\n"',
            ],
            cwd=self.repo,
        ).stdout
        source_identity = self._run_command(
            ["jj", "--ignore-working-copy", "log", "-r", "@", "--no-graph", "-T", "change_id ++ \" \" ++ commit_id"],
            cwd=self.repo,
        ).stdout
        initiative_id = self._create_composition_initiative()

        self.asha_json("initiative", "dispatch", initiative_id, "--node", "parallel-a", "--json")
        self.asha_json("initiative", "dispatch", initiative_id, "--node", "parallel-b", "--json")
        store = self._wait_for_node_states(
            initiative_id, {"parallel-a": "succeeded", "parallel-b": "succeeded"},
        )
        writer_seals = {
            item["node_id"]: item for item in store.list_seals_snapshot(initiative_id)
            if item["node_id"] in {"parallel-a", "parallel-b"}
        }
        self.assertEqual(set(writer_seals), {"parallel-a", "parallel-b"})
        self.assertNotEqual(
            writer_seals["parallel-a"]["jj_commit_id"],
            writer_seals["parallel-b"]["jj_commit_id"],
        )

        self.asha_json(
            "initiative", "dispatch", initiative_id, "--node", "compose-candidate", "--json",
        )
        store = self._wait_for_node_states(
            initiative_id, {"compose-candidate": "succeeded"},
        )
        composed = next(
            item for item in store.list_seals_snapshot(initiative_id)
            if item["node_id"] == "compose-candidate"
        )
        self.assertEqual(composed["base"]["kind"], "composition-inputs")
        self.assertEqual(composed["base"]["seal_ids"], [
            writer_seals["parallel-a"]["seal_id"],
            writer_seals["parallel-b"]["seal_id"],
        ])
        self.assertEqual(composed["cumulative_changed_paths"], ["scope/a.txt", "scope/b.txt"])

        self.asha_json(
            "initiative", "dispatch", initiative_id, "--node", "review-composed", "--json",
        )
        store = self._wait_for_node_states(
            initiative_id, {"review-composed": "succeeded"},
        )
        review = store.list_reviews_snapshot(initiative_id)[0]
        self.assertEqual(review["state"], "accepted-pass")
        self.assertEqual(review["target"]["seal_id"], composed["seal_id"])

        before_verification_workspaces = JjAdapter().workspace_identities(self.repo)
        verification_action = self.asha_json(
            "initiative", "dispatch", initiative_id, "--node", "verify-composed", "--json",
        )
        self.assertEqual(verification_action["state"], "completed")
        store = InitiativeStore(self.config)
        self.assertEqual(store.peek(initiative_id)["state"], "ready-for-integration")
        verification = store.list_verifications_snapshot(initiative_id)[0]
        self.assertEqual(verification["state"], "passed")
        command = verification["commands"][0]
        self.assertEqual(command["pre_identity_digest"], command["post_identity_digest"])
        self.assertEqual(command["pre_tree_digest"], composed["tree_digest"])
        command_evidence = store.read_evidence(
            initiative_id, verification["evidence_ids"][0],
        )
        self.assertIn(
            "__pycache__/gate.pyc",
            json.loads(command_evidence["summary"])["non_tracked_paths"],
        )
        bundle = store.list_bundles_snapshot(initiative_id)[0]
        self.assertEqual(bundle["outcome"], "compatible")
        self.assertEqual(bundle["members"][0]["seal_id"], composed["seal_id"])

        after_verification_workspaces = JjAdapter().workspace_identities(self.repo)
        for name, identity in before_verification_workspaces.items():
            self.assertEqual(after_verification_workspaces[name], identity)
        self.assertEqual(
            self._run_command(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip(),
            source_head,
        )
        self.assertEqual(
            self._run_command(
                [
                    "jj", "--ignore-working-copy", "bookmark", "list", "-T",
                    'name ++ " " ++ normal_target.commit_id() ++ "\\n"',
                ],
                cwd=self.repo,
            ).stdout,
            source_bookmarks,
        )
        self.assertEqual(
            self._run_command(
                ["jj", "--ignore-working-copy", "log", "-r", "@", "--no-graph", "-T", "change_id ++ \" \" ++ commit_id"],
                cwd=self.repo,
            ).stdout,
            source_identity,
        )
        self.assertFalse((self.repo / "scope").exists(), "candidate was integrated into source")

    def test_real_jj_commands_accept_ignored_artifacts_and_reject_tracked_edits(self) -> None:
        materialization = prepare_materialization(
            self.config.control, self.repo, self.base_commit,
            f"tracked-predicate-{os.getpid()}", jj=JjAdapter(),
        )
        workspace = Path(materialization["workspace_path"])
        self._run_command(
            [
                sys.executable, "-c",
                "import os; from pathlib import Path; "
                "Path('__pycache__').mkdir(); "
                "Path('__pycache__/probe.pyc').write_bytes(b'ignored'); "
                "Path('.pytest_cache').mkdir(); os.mkfifo('.pytest_cache/fifo'); "
                "os.link('seed.txt', 'hardlink-extra')",
            ],
            cwd=workspace,
        )
        unchanged, non_tracked, truncated = tracked_workspace_status(
            JjAdapter(), workspace, self.repo, self.base_commit,
        )
        self.assertTrue(unchanged)
        self.assertFalse(truncated)
        self.assertIn("__pycache__/probe.pyc", non_tracked)
        self.assertIn(".pytest_cache/fifo", non_tracked)
        self.assertIn("hardlink-extra", non_tracked)

        self._run_command(
            [
                sys.executable, "-c",
                "from pathlib import Path; Path('seed.txt').write_text('mutated\\n')",
            ],
            cwd=workspace,
        )
        unchanged, _non_tracked, _truncated = tracked_workspace_status(
            JjAdapter(), workspace, self.repo, self.base_commit,
        )
        self.assertFalse(unchanged)


if __name__ == "__main__":
    unittest.main()
