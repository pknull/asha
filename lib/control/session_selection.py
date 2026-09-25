"""Launch-time model and reasoning-effort selection, and its evidence (#95).

The chair or the Keeper chooses at launch; Asha never picks, routes or
escalates a model. Omitted values mean the harness default and add no flag.
Requested values live in the session spec; values a native stream reports live
in ``selection_reported``. Evidence always says which it is, and an
unreported, unrequested value stays ``unknown``.

Native seams (installed ``--help`` and app-server schema):

=========  =======================  ==============================================
Harness    Model                    Effort
=========  =======================  ==============================================
claude     ``--model M``            ``--effort low|medium|high|xhigh|max``
codex      ``-m M`` / thread model  ``-c model_reasoning_effort="E"`` / turn effort
copilot    ``--model M``            ``--effort none|minimal|low|medium|high|xhigh|max``
opencode   ``-m provider/model``    none in the interactive TUI (refused)
=========  =======================  ==============================================
"""
from __future__ import annotations

import re
import time

from .store import StoreError

FIELDS = ("model", "effort")
HARNESS_EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "copilot": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
}
# Codex effort is "a non-empty value advertised by the model"; checked by shape.
_FREE_EFFORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_REROUTE_LIMIT = 8


def _model(value, harness):
    if (not isinstance(value, str) or not value or value.startswith("-") or len(value.encode("utf-8")) > 256
            or not value.isprintable() or any(c.isspace() for c in value)):
        raise StoreError('model must be printable, without whitespace or a leading "-", at most 256 bytes')
    if harness == "opencode":
        provider, _, name = value.partition("/")
        if not provider or not name:
            raise StoreError("OpenCode model must be provider/model")
    return value


def _effort(value, harness):
    if harness == "opencode":
        raise StoreError("OpenCode interactive sessions have no effort flag; omit --effort")
    if not isinstance(value, str):
        raise StoreError("effort must be a string")
    allowed = HARNESS_EFFORTS.get(harness)
    if allowed is not None:
        if value not in allowed:
            raise StoreError(f"{harness} effort must be one of: " + ", ".join(allowed))
    elif _FREE_EFFORT.fullmatch(value) is None:
        raise StoreError("effort must be 1-64 letters, digits, '.', '_' or '-'")
    return value


def normalize(harness, transport, model=None, effort=None) -> dict:
    """Validated requested selection; ``{}`` when nothing was requested."""
    del transport  # Structured availability is checked with the transport itself.
    selection = {}
    if model is not None:
        selection["model"] = _model(model, harness)
    if effort is not None:
        selection["effort"] = _effort(effort, harness)
    return selection


def requested(spec) -> dict:
    return {field: spec[field] for field in FIELDS if (spec or {}).get(field) is not None}


def terminal_flags(harness, selection) -> list[str]:
    """Native interactive flags, placed before the prompt argument."""
    model, effort = (selection or {}).get("model"), (selection or {}).get("effort")
    flags: list[str] = []
    if harness == "codex":
        if model is not None:
            flags += ["-m", model]
        if effort is not None:
            flags += ["-c", f'model_reasoning_effort="{effort}"']
    elif harness == "opencode":
        if model is not None:
            flags += ["-m", model]
    else:
        if model is not None:
            flags += ["--model", model]
        if effort is not None:
            flags += ["--effort", effort]
    return flags


def record_reported(row, reported, *, source, reroute=None) -> dict:
    """Return ``row`` with a native report merged into ``selection_reported``.

    A reported value never refuses the session; a mismatch with the request
    (reroute or fallback) is retained as evidence.
    """
    current = dict(row.get("selection_reported") or {})
    for field in FIELDS:
        value = (reported or {}).get(field)
        if isinstance(value, str) and value and len(value.encode("utf-8")) <= 256 and value.isprintable():
            current[field] = value
    current.update(source=source, observed_at=time.time(), generation=row.get("generation"))
    if reroute is not None:
        entry = {"from_model": reroute.get("from_model"), "to_model": current.get("model"),
                 "reason": reroute.get("reason"), "observed_at": current["observed_at"]}
        current["reroutes"] = [*current.get("reroutes", []), entry][-_REROUTE_LIMIT:]
    return dict(row, selection_reported=current)


def evidence(row) -> dict:
    """``{model, effort}`` each as ``{requested, effective, provenance}``."""
    spec = (row or {}).get("spec") or {}
    reported = (row or {}).get("selection_reported") or {}
    result = {}
    for field in FIELDS:
        asked, effective = spec.get(field), reported.get(field)
        provenance = "reported" if effective is not None else "requested" if asked is not None else "unknown"
        result[field] = {"requested": asked, "effective": effective, "provenance": provenance}
    return result


def known_model(row):
    """Effective when reported, requested otherwise, else ``None`` (unknown)."""
    selected = evidence(row)["model"]
    return selected["effective"] or selected["requested"]


def label(row, *, compact=False) -> str:
    """Known selection for display; requested-only values are marked as such."""
    selected = (row or {}).get("selection") or evidence(row)
    parts = []
    for field in FIELDS:
        item = selected.get(field) or {}
        value = item.get("effective") or item.get("requested")
        if value is None:
            continue
        marked = value if item.get("provenance") == "reported" else value + (" (req)" if compact else " (requested)")
        parts.append(marked if compact and field == "model" else f"{field} {marked}")
    return (" " if compact else " · ").join(parts)


__all__ = ["FIELDS", "HARNESS_EFFORTS", "evidence", "known_model", "label", "normalize", "record_reported",
           "requested", "terminal_flags"]
