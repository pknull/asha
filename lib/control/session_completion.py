"""Project-memory contracts for terminal sessions, plus the scope check a handoff publishes under.

Best-effort close (D3) retired completion receipts: "saved" is read from the
``hub_memory_publications`` rows, and ``report --state finished`` is ungated.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from . import session_closure as closure
from .store import StoreError


WORKER_INSTRUCTION = (
    'Project-memory contract: before work, use the project-memory skill to read this project\'s '
    'Memory v2 through the existing reader and verify relevant claims against live sources. '
    'Do not load chair context or private recovery/transcript stores. Before explicit completion, '
    'publish authorized durable findings, or attest no durable update with `asha control session handoff '
    '--outcome no-durable-update --detail "WHY" --json`; Control shows the session as saved. '
    'Then report completion and end the turn: `asha control session report --state finished --text "RESULT"` '
    '(the row reads finished, saved or unsaved). A close asks for this save and waits a bounded time. '
    'If the skill is unavailable, use `asha control session handoff --read --json` '
    'and the named Memory v2 reader/validator; if memory, scope or native permissions block publication, '
    'report `handoff --outcome blocked --detail "REASON"` and needs-input, never claim completion. '
    'This workflow does not authorize Git commit/push, private-memory publication or permission bypass.'
)


# #105: a Room is an ongoing conversation; its saves are checkpoints, never completion.
ROOM_INSTRUCTION = (
    'Project-memory contract (Room): this Room is an ongoing conversation, not a one-task assignment. '
    'Before work, use the project-memory skill to read this project\'s Memory v2 through the existing reader '
    'and verify relevant claims against live sources. Do not load chair context or private recovery/transcript stores. '
    'When the Keeper asks for a save or durable project knowledge settles, publish it '
    '(`asha control session handoff --read --json` prints the digests, then `asha control session handoff '
    '--active-file ACTIVE --decisions-file DECISIONS --expected-active DIGEST --expected-decisions DIGEST --json`). '
    'A save is a checkpoint: Control shows the Room as saved and it stays open. After a save, keep the '
    'conversation going; do not report completion and do not end the Room. The Room ends when the Keeper closes '
    'it: Control asks for a save, waits a bounded time, then closes; follow that request\'s own instructions. '
    'If memory, scope or native permissions block publication, say so and report '
    '`asha control session report --state needs-input --text "REASON"`. '
    'This workflow does not authorize Git commit/push, private-memory publication or permission bypass.'
)


def instruction(profile):
    """The project-memory contract appended to a terminal session's assignment."""
    return ROOM_INSTRUCTION if profile == 'room' else WORKER_INSTRUCTION


def _scope(row):
    import save_scope
    root = closure.secure_project_root(Path(row['project']))
    plane, errors = save_scope.resolve_effective_plane('none', start=root)
    if not plane or Path(plane['plane_base']) != root:
        raise StoreError('project-memory scope unavailable: ' + (errors[0]['message'] if errors else 'different plane'))
    config = closure.memory_v2.require_v2_config(root)
    if config['project_id'] != row['project_id']:
        raise StoreError('project-memory identity changed')
    closure.memory_v2._assert_persistence_enabled(root)
    return root


@contextmanager
def snapshot(row):
    """Check scope, identity and silence, then hold the publication lock over the published digests."""
    root = _scope(row)
    with closure.memory_v2._publication_lock(root):
        _scope(row)
        value = closure.memory_v2._read_published_snapshot_unlocked(root)
        yield closure.memory_v2.snapshot_digests(value)
