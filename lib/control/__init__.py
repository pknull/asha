"""Deterministic Asha Control core.

Increment 3 adds owned tmux sessions, harness launch and process identity,
live infrastructure evidence, attach/stop/archive verbs, and isolated doctor
probes. Semantic harness events and the TUI remain deferred.
"""

from .model import TASK_CONTRACT, RUN_CONTRACT

__all__ = ["TASK_CONTRACT", "RUN_CONTRACT"]
