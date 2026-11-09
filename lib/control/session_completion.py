"""Project-memory contracts for terminal sessions, plus the scope check a handoff publishes under.

Best-effort close (D3) retired completion receipts: "saved" is read from the
``hub_memory_publications`` rows, and ``report --state finished`` is ungated.
"""
from __future__ import annotations

import os
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
    'Write drafts with your edit tool and run each `asha control session` command on its own with literal '
    'arguments: no `&&`, pipes, redirection, `$VAR` or heredoc. '
    'If the skill is unavailable, take the save digests from `asha control session handoff --read --json` first, '
    'then reread the pair with the named Memory v2 reader/validator; if memory, scope or native permissions block publication, '
    'report `handoff --outcome blocked --detail "REASON"` and needs-input, never claim completion. '
    'This workflow does not authorize Git commit/push, private-memory publication or permission bypass.'
)


# #105: a Room is an ongoing conversation; its saves are checkpoints, never completion.
ROOM_INSTRUCTION = (
    'Project-memory contract (Room): this Room is an ongoing conversation, not a one-task assignment. '
    'Before work, use the project-memory skill to read this project\'s Memory v2 through the existing reader '
    'and verify relevant claims against live sources. Do not load chair context or private recovery/transcript stores. '
    'When the Keeper asks for a save or durable project knowledge settles, publish it: take the digests first with '
    '`asha control session handoff --read --json`, then reread both Memory files and carry their current content '
    'into your drafts, then run `asha control session handoff --active-file ACTIVE --decisions-file DECISIONS '
    '--expected-active DIGEST --expected-decisions DIGEST --json`; if it refuses a changed preimage, take the '
    'digests again, then reread and merge; never only refresh the digests. '
    'Write the drafts with your edit tool and run each `asha control session` command on its own with literal '
    'arguments: no `&&`, pipes, redirection, `$VAR` or heredoc. '
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


def _scope(row, *, silence=True):
    import save_scope
    root = closure.secure_project_root(Path(row['project']))
    plane, errors = save_scope.resolve_effective_plane('none', start=root)
    if not plane or Path(plane['plane_base']) != root:
        raise StoreError('project-memory scope unavailable: ' + (errors[0]['message'] if errors else 'different plane'))
    config = closure.memory_v2.require_v2_config(root)
    if config['project_id'] != row['project_id']:
        raise StoreError('project-memory identity changed')
    if silence:
        closure.memory_v2._assert_persistence_enabled(root)
    return root


def component_scope(row, path, *, silence=True):
    """#130: the nested Memory v2 project ``path`` names, inside the acting session's own project.

    The folder structure decides (Keeper, revised 2026-11-03); no manifest is
    read. ``path`` is resolved as the OS resolves it (a relative ``path``
    against the root), and only that real path is judged: it must lie strictly
    inside the session's own root, no folder of it below the root (it
    included) may be hidden (a name starting with ``.``), and it must be its
    own Memory v2 project and plane with a ``project_id`` different from the
    session's. The spelling is never judged, so a hidden name cancelled by
    ``..`` or a link through a hidden link is accepted when the real path
    qualifies, and a plain link to a hidden real path is refused. With
    ``silence`` (the publication path), either plane silenced refuses; the
    read reports silence instead.
    """
    import save_scope
    root = _scope(row, silence=silence)
    if not isinstance(path, str) or not path or '\x00' in path:
        raise StoreError('a component path is required')
    given = Path(path) if os.path.isabs(path) else root / path
    try:
        requested = given.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StoreError(f'component path does not exist: {path}') from exc
    if requested == root:
        raise StoreError(f"component path {path} is this session's own project root")
    # Control runs this outside a sandboxed agent's sandbox, so a link the agent
    # planted inside the root must not carry the write out of it.
    if root not in requested.parents:
        raise StoreError(f"component path {path} resolves outside this session's project root: {requested}")
    relative = requested.relative_to(root)
    hidden = next((part for part in relative.parts if part.startswith('.')), None)
    if hidden is not None:
        raise StoreError(f'component path {path} is in or under the hidden folder {hidden}')
    try:
        config = closure.memory_v2.require_v2_config(requested)
    except ValueError as exc:
        raise StoreError(f'component {relative} has no initialized Memory v2 config: {exc}') from exc
    if config['project_id'].strip() == row['project_id'].strip():
        raise StoreError(f"component {relative} shares this session's project_id")
    plane, errors = save_scope.resolve_effective_plane('none', start=requested)
    if not plane or Path(plane['plane_base']) != requested:
        raise StoreError(f'component {relative} is not its own Memory plane: '
                         + (errors[0]['message'] if errors else 'different plane'))
    if silence:
        closure.memory_v2._assert_persistence_enabled(requested)
    return {'root': root, 'child': requested, 'path': str(relative), 'project_id': config['project_id']}


@contextmanager
def snapshot(row):
    """Check scope, identity and silence, then hold the publication lock over the published digests."""
    root = _scope(row)
    with closure.memory_v2._publication_lock(root):
        _scope(row)
        value = closure.memory_v2._read_published_snapshot_unlocked(root)
        yield closure.memory_v2.snapshot_digests(value)
