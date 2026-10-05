"""Fake credential specimens for the session-tool redaction tests (#118).

Every specimen is assembled at runtime, so no complete credential-shaped
literal sits in the source for a secret scanner to flag. All values are fake.
"""

from typing import NamedTuple


class Specimen(NamedTuple):
    kind: str    # auth, token, jwt, aws or private_key
    text: str    # the credential as it would appear in text
    secret: str  # the part that must not survive redaction


def _token(prefix: str, body: str) -> Specimen:
    return Specimen("token", prefix + body, body)


def _private_key() -> Specimen:
    label = "ENCRYPTED " + "PRIVATE" + " KEY"
    body = "MIIExampleOnlyNotAKey0000000000"
    return Specimen(
        "private_key",
        f"-----BEGIN {label}-----\n{body}\n-----END {label}-----",
        body,
    )


def _jwt() -> Specimen:
    header = "ey" + "JhbGciOiJIUzI1NiJ9"
    payload = "ey" + "JzdWIiOiJleGFtcGxlIn0"
    signature = "ExampleSignatureNotReal0000_-x"
    return Specimen("jwt", f"{header}.{payload}.{signature}", signature)


# Specimens for every prefix family recovery_state covered when #118 was filed.
SPECIMENS = {
    "bearer": Specimen("auth", "Bearer ExampleBearerCredential000",
                       "ExampleBearerCredential000"),
    "basic": Specimen("auth", "Basic ZXhhbXBsZTpub3QtYS1yZWFsLXBhc3N3b3Jk",
                      "ZXhhbXBsZTpub3QtYS1yZWFsLXBhc3N3b3Jk"),
    "bearer_hex": Specimen("auth", "Bearer 3f9a1c0b7e5d42a8b6c1", "3f9a1c0b7e5d42a8b6c1"),
    # Letters-only credentials: base64 of "user:pass", and a long opaque token.
    "basic_letters": Specimen("auth", "Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    "bearer_letters": Specimen("auth", "Bearer opaquelowercasebearertokenvalue",
                               "opaquelowercasebearertokenvalue"),
    **{f"gh{kind}": _token(f"gh{kind}_", f"ExampleGh{kind}Token000000000000000001")
       for kind in "pousr"},
    "github_pat": _token("github" + "_pat_", "11EXAMPLE000000000000_ExamplePatBody0000000"),
    "glpat": _token("gl" + "pat-", "ExampleGitLab0000000"),
    "npm": _token("np" + "m_", "ExampleNpmToken000000000000000000000"),
    "sk": _token("s" + "k-", "proj-ExampleOpenAiKey0000000000000000"),
    **{f"xox{kind}": _token(f"xox{kind}-", "123456789012-ExampleSlackBody0000000000")
       for kind in "baprs"},
    "aiza": _token("AI" + "za", "SyExampleGoogleKey00000000000000000"),
    "jwt": _jwt(),
    "aws": Specimen("aws", "AK" + "IA" + "IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    "private_key": _private_key(),
}

# Ordinary prose with hex commit IDs; no family may redact any of it.
PROSE = (
    "Fixed the retry loop in commit 09df536fd7cf086105339297cee2be01f3bf2223 "
    "and its follow-up 18aef121338f; the scheduler now waits for its lease "
    "before handing work to a runner. Rotate the signing key whenever a token "
    "or secret leaks, then rerun the sk-learn notebook and npm install.\n"
)

# Prose that sits next to the Basic/Bearer and npm_ shapes without a credential.
PROSE_NEAR_MISSES = (
    "Basic configuration is enough for local runs.\n",
    "A Basic Configuration section follows.\n",
    "Start from a basic setup and extend it.\n",
    "The guide assumes basic understanding of OAuth.\n",
    "It also assumes basic JavaScript knowledge.\n",
    "Bearer tokens are described in RFC 6750.\n",
    "Most services accept Bearer authentication.\n",
    "See the npm_config docs before publishing.\n",
    "Read npm_package_version and npm_lifecycle_event in scripts.\n",
)
