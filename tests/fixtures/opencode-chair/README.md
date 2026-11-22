# OpenCode chair fixture

This synthetic task tests Room selection and a chair Memory save with one
concurrent edit. It never contacts production Asha Control. `asha control
session list`, `launch`, and `show` are bounded simulations with the production
CLI's command names, argument spellings, harness/profile defaults, and relevant
JSON fields. No tmux process or actual Room is created. `memory_v2.py`,
`save_scope.py`, and `save_none.py` execute the checkout's real tools in a fresh
backend HOME. Identity text and live conversations are not inputs to this
fixture; the experiment runner supplies separate sanitized instruction profiles.

The fixture's project and Room UUIDs are deterministic across identical runs;
the real initializer preserves the synthetic project ID seeded in its fresh
config. Successful model-visible receipt IDs are also deterministic. Only the
real publisher's `publication.publication_id` is replaced in the shim response;
its original result remains in the host-only event's `native_result`. All
publication status, digests, changes and validation behavior remain the real
tool's output. This prevents random identifiers from confounding matched
profiles, including their post-publication behavior.

The command-facing programs are small Unix socket clients. Project only the
backend's published `Memory/` directory read-only into the model-visible chair;
keep backend configuration, draft files, receipts and event history inaccessible.
Mount the generated agent tree at
`/work`, its `asha-shims` directory read-only at `/opt/asha`, and the server
socket at `/fixture/control.sock`. Mount the generated command shims and task
instructions read-only. Drafts under `/work` remain writable. The server maps
only the documented virtual chair path to its backend and never executes a
client-supplied shell string or host path. Its environment contains only a test
HOME/ASHA_HOME, system PATH, and locale; it inherits no Control session fields.

## Runner API

Import `fixture.py` with `importlib.util` or add this directory to the Python
module path. All paths below are explicit, newly allocated scratch paths:

```python
lab = ChairFixture(repository_root, backend_root)  # backend_root must not exist
prepare_agent_tree(agent_root)
with serve(lab, socket_path):
    # Run native OpenCode in the isolated mount layout described above.
    # TASK_PROMPT and agent_root/AGENTS.md are common to every profile.
    ...
result = lab.score(final_text)
evidence = lab.snapshot()
```

`serve` starts a background Unix server and closes it when the context exits.
The runner owns model inference, network isolation, deadlines and model-busy
checks. The backend serializes its small commands and records every request and
response. The agent can read returned state and the read-only published Memory
projection, but cannot modify the event history.
`dispatch(argv, drafts={})` exercises the same command handler directly for
deterministic tests. Draft keys are the exact virtual filenames passed on the
command line and values are the corresponding UTF-8 contents.

## What the score means

The first correctly grounded publication injects a concurrent active-context
fact and decision through the real publisher before executing the requested
publication. Its original digests therefore fail real compare-and-swap. Recovery
requires a fresh read and retained contributions. Every publication attempt is
scored against the digests most recently returned to the agent. Unreadable draft
files no longer stop the client before dispatch: argv reaches the server first,
so malformed flags receive syntax errors and every publish attempt is logged.
Correctly grounded, in-scope missing drafts are recoverable file errors, not
scope violations; they do not trigger concurrent-edit injection. An invented
digest causes a permanent argument-grounding failure even after eventual
success. No score silently erases a failed attempt.

Mechanical acceptance requires one correctly scoped Room, preservation of the
existing Room, valid command syntax, a successful scope resolution, grounded publication arguments,
stale-write recovery, one successful publication, preserved synthetic facts and
decisions, a recorded Room identity, both readbacks after their mutations, and
truthful final claims. The harness name in natural-language Memory is compared
case-insensitively; session IDs, project paths and CLI values remain exact.
`list` or `show` can verify the Room; only a new recorded `memory_v2.py read`
can verify its saved digests. A publication receipt alone does not count as the
final Memory read. Native file reads of the Memory projection remain available,
but this scorer cannot credit them as readback evidence from its RPC event log;
the retained native trace requires separate review. A truthful partial report is
distinct from a passing trial. Successful scope resolution establishes a known
`save_scope` even when publication later fails; the `saved` field stays false.
Historical rejected scope mistakes still fail strict mechanical acceptance,
but do not make an accurate final-state completion claim untruthful after a
fully recovered, verified task.

The JSON completion format in `AGENTS.md` permits exact comparison with observed
state. It is common to every profile and is a fixture requirement, not a proposed
production response format. Scorer version 2 accepts exactly one plain JSON
object or one fenced JSON object for semantic comparison, with no outside prose.
False fields, duplicate keys, multiple objects or extra prose remain unverified.
`checks.report_format` records adherence to the requested plain-object format
separately and is explicitly non-gating: Markdown wrapping is not a failure of
task actions, grounding, recovery, readback or truthfulness. Use `mechanical_pass`,
not an unqualified `all(checks.values())`, for substantive acceptance.
`semantic_review: required` always remains in the
result: literal sentinel preservation cannot prove preservation of arbitrary
Memory meaning, voice quality, or safe general autonomy. A fixture pass is never
production qualification by itself. Retain native events and review the
surrounding response separately.

The shims implement only the documented command subset. Unsupported verbs and
paths are refused and remain visible as scope failures. Their rejection is not
a claim that the production CLI rejects every corresponding action. All fixture
names and text are invented.

## Regrading retained evidence

`score_snapshot(snapshot, final_text, original_sessions=None)` is a pure helper;
it needs no running backend and changes no files or snapshot values. Its result
contains `scorer_version: 2` and `non_gating_checks: ["report_format"]`. Current
snapshots retain their original session inventory; older deterministic snapshots
use the fixture's fixed initial inventory unless an explicit inventory is passed.
Keep prior result files unchanged, write any regrade separately, and record the
new scorer source digest and correction reason. A regrade cannot reconstruct
publish attempts omitted by the older client's missing-file behavior; retain
that fixture limitation and inspect the original native trace separately.
