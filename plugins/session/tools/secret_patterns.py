#!/usr/bin/env python3
"""Shared credential patterns for session tools that redact or reject secrets.

Recovery snapshots, workspace knowledge and workspace work items all use this
one set, so their coverage cannot drift apart again (#118). It defines only
what a credential looks like; each caller keeps its own replacement text.
"""

from __future__ import annotations

import re


_CREDENTIAL = r"[A-Za-z0-9._~+/=-]"
# An Authorization scheme and its credential. Group 1 is the scheme word. So
# that prose such as "basic configuration" passes, the credential needs a digit,
# one of + ~ =, two lower-to-upper case changes as base64 has, or 24 characters.
AUTH_SCHEME_RE = re.compile(
    rf"\b((?i:Bearer|Basic))\s+"
    rf"(?:(?={_CREDENTIAL}*?(?:[0-9+~=]|[a-z][A-Z]{_CREDENTIAL}*?[a-z][A-Z]))"
    rf"{_CREDENTIAL}{{8,}}|{_CREDENTIAL}{{24,}})"
)

# npm tokens are npm_ plus 36 alphanumerics; npm_config_* names never get close.
_PREFIXED_TOKEN = (
    r"\b(?:gh[opsur]_|github_pat_|glpat-|sk-|xox[abprs]-|AIza)[A-Za-z0-9_-]{12,}"
    r"|\bnpm_[A-Za-z0-9]{32,}"
)
_JWT = r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
_AWS_ACCESS_KEY = r"\bAKIA[A-Z0-9]{16}\b"

PREFIXED_TOKEN_RE = re.compile(_PREFIXED_TOKEN)
JWT_RE = re.compile(_JWT)
AWS_ACCESS_KEY_RE = re.compile(_AWS_ACCESS_KEY)
# The three bare-token families above, for callers with one token label.
TOKEN_RE = re.compile(f"{_PREFIXED_TOKEN}|{_JWT}|{_AWS_ACCESS_KEY}")

# A PEM private-key block; one with no END line runs to the end of the text.
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)

FAMILIES = (AUTH_SCHEME_RE, PREFIXED_TOKEN_RE, JWT_RE, AWS_ACCESS_KEY_RE, PRIVATE_KEY_RE)
