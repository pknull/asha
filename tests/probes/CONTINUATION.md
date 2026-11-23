# Continuation and tool-recovery fixture kit

These probes prepare synthetic projects and score exported evidence. They do
not launch a harness, contact a provider, change live Memory, install tools, or
read private conversations. Preparation and unit tests are offline.

The first supported producer is an existing **structured Codex Control worker**.
It runs native Codex through Asha's production transport, but that worker omits
automatic Room Memory startup context. This kit therefore tests explicit,
source-grounded continuation. It does **not** qualify native Room startup,
Memory compression, a new default, or a model's consumption of startup text.
`startup_delivery` remains `unknown`, including on a passing task.

## Prepare

Use a new output directory for every run:

```bash
python3 tests/probes/memory_continuation.py prepare --case facts --output /tmp/continuation-facts-1
python3 tests/probes/memory_continuation.py prepare --case operation --output /tmp/continuation-operation-1
python3 tests/probes/tool_recovery.py prepare --case failed-read --output /tmp/recovery-failed-read-1
python3 tests/probes/tool_recovery.py prepare --case spill-readback --output /tmp/recovery-spill-1
```

Each run contains:

- `project/`: the only project directory supplied to the model, initialized
  through the real Memory v2 initializer and accepted by Control's project
  resolver. The continuation cases also use its real CAS publisher and reader.
- `task.md`: the exact assignment, with a unique run token and explicit final
  output format. It tells the worker to leave inputs unchanged, write
  `result.json`, then read it back as the last native command.
- `expected.json`: expected values, outside the supplied project.
- `manifest.json`: fixture, task, expected-answer, project-input and declared
  implementation-source hashes. `comparison_key` excludes the random project
  ID and run token; it identifies the same fixture/case, not equal model settings.
- `startup-preview.txt` for the continuation cases: output of the real startup
  renderer, for inspection only. It is not submitted as an extra system prompt.

Expected-answer placement is staging separation, **not filesystem isolation**.
The native sandbox and permissions govern access. This kit supplies no separate
containment boundary and makes no claim against a model reading sibling paths.
Use only the synthetic project. Do not substitute manuscripts, secrets or live
project Memory. An existing output directory is refused and never overwritten.

The `facts` case asks three explicit retained facts. The `operation` case asks
for the correct next action under the retained publication constraint and a
contradictory current status file. It does not require reciting unrelated facts.
Both carry the same Memory facts in the same order; decisions fit entirely
inside the current 2,048-byte excerpt. They are different tasks, not two arms of
an experiment. Source retrieval is legitimate. No placement experiment or
generated-versus-reference comparison is claimed here.

The `failed-read` case asks for one unavailable input attempt, then recovery
through an index to the replacement source. The `spill-readback` case uses the
small original `workload.py` fixture: it writes a complete spill log and emits
an explicit incomplete preview. This is an ordinary synthetic workload, not a
wrapper installed around native tools. Control's `output_truncated` flag means
its retained display body was truncated; that flag alone is never accepted as
proof of a workload spill or of truncation seen by the model.

## Collect a separately authorized native run

Select the model, native configuration and budget before running inference.
The commands below describe the existing producer; neither probe executes them.
Use a fresh session for each prepared run. Do not resume an old conversation.

To preserve the exact prompt bytes without shell substitution, a collector can
read `task.md` and invoke the existing CLI with an argument list:

```python
from pathlib import Path
import subprocess

run = Path('/tmp/continuation-facts-1').resolve()
subprocess.run([
    'asha', 'control', 'session', 'launch',
    '--project', str(run / 'project'),
    '--prompt', (run / 'task.md').read_text(),
    '--harness', 'codex', '--profile', 'worker', '--transport', 'structured',
    '--model', 'EXPLICITLY_SELECTED_MODEL', '--no-learning', '--json',
], check=True)
```

`--no-learning` keeps the opening queued assignment byte-identical to `task.md`;
automatic selected guidance would append context and require a separately
declared experiment. The scorer requires the actual opening message digest and
session-created prefix, then binds the first reserved turn to that message.
A later-turn suffix cannot pass merely because the page says `complete`.

Keep the returned session ID. Once its native work finishes, capture the stdout
of each existing read command unchanged into the named files:

```text
asha control session events SESSION_ID --after 0 --limit 1000 --json
asha control session show SESSION_ID --json
```

Save the first response as `events.json` and the second as `show.json` beside
`manifest.json`. Use the full session ID. Do not set an event consumer or
acknowledge delivery. A response with `complete: false`, retention gaps, or more
than one turn is unsupported by this bounded checker; do not splice pages or
drop records to force acceptance. The fixture should fit in one 1,000-event
page. A failed or unfinished process remains unknown even if a native final
answer said it succeeded. No cleanup or close action is performed by the kit.

Producer definitions checked in this repository:

- `lib/control/codex_protocol.py`: native app-server item IDs, statuses, final
  answer and normalized command outputs.
- `lib/control/session_harness.py`: native terminal result plus clean process
  exit; a native final does not suppress later transport failure.
- `lib/control/sessions.py` and `session_store.py`: turn ownership, native
  observations, `turn-finished`, and the exported event page.
- `lib/control/session_hub.py`: `show.activity` and the structured-worker-only
  profile boundary. This second read matters because a retained native success
  can coexist with later process failure.

Only this producer is supported. Claude structured exports, Codex exec JSON,
interactive Rooms, Copilot and OpenCode remain unsupported by this scorer.
Do not translate them by guessing event meanings. A later native Room study
needs a verified capture of the actual Room startup path, its effective context,
scoped tool events and completed process/turn. A renderer output or TUI screen
alone supplies none of that consumption evidence. The existing OpenCode chair
probe's `--pure` path omits production hooks, so it cannot fill this gap.

## Score

```bash
python3 tests/probes/memory_continuation.py score --run /tmp/continuation-facts-1 --events /tmp/continuation-facts-1/events.json --session /tmp/continuation-facts-1/show.json
python3 tests/probes/tool_recovery.py score --run /tmp/recovery-failed-read-1 --events /tmp/recovery-failed-read-1/events.json --session /tmp/recovery-failed-read-1/show.json
```

Scoring is read-only. It first validates every declared input and source hash,
then binds the project and exact assignment to the session, its single turn,
native identity, paired tool starts/completions, final answer and process
completion. A changed fixture, task, implementation source or expected-answer
file invalidates the run. Missing, duplicate, foreign or incomplete evidence
cannot pass. The first version deliberately requires a complete single page.

`task: pass` has a narrow structural meaning: the actual `result.json` has the
expected typed JSON values, the last completed native command returned matching
untruncated JSON, and the native turn/process finished successfully. Recovery
also requires the relevant failure/preview output before that matching output;
the spill file must match its prepared hash. A plausible final answer without
these observations is insufficient. A declared `partial` is distinct from a
completed task; incorrect literal values yield `fail`.

**Readback limit:** the existing normalized Control export omits command
arguments. It proves observed command outputs and current artifact bytes, not
which read command ran, whether the command read rather than printed those
bytes, or why the model recovered. Tool intent and semantic attribution require
manual review and remain unmeasured here. No cryptographic attestation or
protection against same-user fabrication is claimed.

Results separate `infrastructure`, `task`, `startup_delivery`, and
`semantic_review`. Counts and elapsed time are descriptive and never override
correctness. A comparison also needs the same selected/observed model,
configuration and environment; matching `comparison_key` alone proves only
fixture identity. No token cost estimate, production qualification, recall
penalty for source reads, or default-policy recommendation is generated.

Exit status is 0 for a literal task pass or successful preparation, 1 for a
literal failure/partial outcome, and 2 for unknown evidence. No live model
usefulness has been measured by this build's offline tests.

## Offline checks

```bash
python3 -m unittest tests.python.test_memory_continuation_probe tests.python.test_tool_recovery_probe
```

Tests use real temporary files, the actual Memory initializer/publisher/reader
and Control project resolver, plus hand-written fixtures in the existing native
export shape. They do not claim that those synthetic events were emitted by a
live model. Broad repository verification remains `./tests/run-tests.sh`.
