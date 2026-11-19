#!/usr/bin/env python3
"""Fail when a tracked file, its path, or a commit about to be pushed names a private project.

asha is public: its plugins, tests, CHANGELOG and docs must not name the owner's
campaigns, characters, places, books or private repositories. The names live in a
local config that is never committed, so the guard reports a skip (exit 0) on a
machine without one, such as CI.

Config: --config, else $ASHA_PRIVATE_NAMES (either must exist), else
private-names.conf in $ASHA_HOME, ~/.asha, or the account's own ~/.asha (found
through the password database, so a test run under a sandbox HOME still finds
it). One directive a line:
  name: <text>    a name, matched in any letter case
  term: <text>    a name that is also an English word ("Gorse"), matched as
                  written, in capitals or as a lowercase slug (below), so the
                  word itself is not flagged
  stems: <dir>    every *.md note name and folder name under <dir> (hidden ones
                  skipped) is a term; "The X" is also matched as "X". Skipped: a
                  single dictionary word, "The <dictionary word>", anything under
                  four characters, a month and day without a year, a day file
                  (day-NNNN), anything without a letter, index and log notes. A
                  missing <dir>, or no dictionary to filter with, is an error,
                  never a smaller check.
  allow: <text>   never flag this name
A multi-word name also matches joined by "-", "_", "%20" or nothing. A match may
sit inside an identifier (myName, nameState, Name2) or a URL-encoded path, but not
inside a longer word (a lowercase tail, or capitals after a match in capitals). A
one-word term or note name also matches as a lowercase slug ("- name:",
"character:name", "name.md", ".../name/...").

Not caught: plurals and other inflections, spelling variants (list each), a name
wrapped across lines, a multi-word note name in sentence case ("Old mill road"),
and note names that are dictionary words or "The <dictionary word>" (list a real
name among them with term:).

Usage: check-private-names.py [--root DIR] [--config FILE] [--dict FILE]
                              [--rev REV | --pre-push [--remote R] [--url U]] [--locations]
File paths are checked as well as contents. --rev checks one commit's files (a
git checkout is needed). --pre-push, for a git pre-push hook (tests/hooks/pre-push
passes the remote's name and the URL being pushed to), checks each pushed remote
ref name (a deletion is allowed), each annotated tag's message, and every new
commit's message, changed paths (renamed, binary and empty files included) and
added text lines, so a name added and removed within one push is still caught;
binary file content is not scanned. A commit is new unless `git ls-remote` on the
push URL lists it (or, for tips this clone has not fetched, the remote's tracking
refs hold it); offline, the tracking refs decide. A new remote or mirror therefore
gets the whole history checked, and asha's own early history refuses that push.
Not checked: the content of a tag of a tree or blob. jj git push runs no git
hooks. --locations prints file:line instead of the matched text (a path that is
itself the hit still shows). Exit 0 clean or skipped, 1 on a hit, 2 when it
cannot check.
"""
import argparse
import io
import os
import pwd
import re
import subprocess
import sys
import tarfile
from pathlib import Path

START = r"(?:(?<![A-Za-z])|(?<=\\[ntr])|(?<=%[0-9A-Fa-f]{2})|(?<=[a-z])(?=[A-Z]))"
END = r"(?![a-z])"
SEPS = (" ", "-", "_", "%20", "")


class CheckError(Exception):
    pass


def config_path(explicit, home_only=False):
    if explicit:
        return explicit, True
    if os.environ.get("ASHA_PRIVATE_NAMES"):
        return os.environ["ASHA_PRIVATE_NAMES"], True
    homes = []
    if os.environ.get("ASHA_HOME"):
        homes.append(os.environ["ASHA_HOME"])
    homes.append(os.path.join(os.path.expanduser("~"), ".asha"))
    if not home_only:
        try:
            homes.append(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".asha"))
        except KeyError:
            pass
    candidates = [os.path.join(h, "private-names.conf") for h in homes]
    return next((c for c in candidates if os.path.isfile(c)), candidates[0]), False


def load_words(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return {w.strip().lower() for w in fh if w.strip()}
    except OSError:
        return set()


MONTH_DAY = re.compile(r"(?i)(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(st|nd|rd|th)?")


def usable(name, words):
    parts = name.split()
    if len(name) < 4 or name.lower() in ("index", "log") or MONTH_DAY.fullmatch(name):
        return False  # a month and day without a year names nothing
    if re.fullmatch(r"(?i)day-\d+", name) or not re.search(r"[^\W\d_]", name):
        return False  # a day file or a bare date names nothing
    if len(parts) == 1:
        return name.lower() not in words
    if len(parts) == 2 and parts[0] == "The":
        return parts[1].lower() not in words
    return True


def stem_names(directory, words):
    if not os.path.isdir(directory):
        raise CheckError(f"stems directory missing: {directory}")
    names = set()
    for _base, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        candidates = list(dirs) + [f[:-3] for f in files if f.endswith(".md") and not f.startswith(".")]
        for stem in candidates:
            stem = " ".join(stem.split())
            if usable(stem, words):
                names.add(stem)
            parts = stem.split()
            if len(parts) > 1 and parts[0] == "The" and usable(" ".join(parts[1:]), words):
                names.add(" ".join(parts[1:]))
    return names


def read_config(path, dict_path):
    names, terms, stems, allow = set(), set(), [], set()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except UnicodeDecodeError:
        raise CheckError(f"{path} is not UTF-8")
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        value = " ".join(value.split())
        key = key.strip()
        if not sep or not value or key not in ("name", "term", "stems", "allow"):
            raise CheckError(f"{path}:{n}: expected 'name:', 'term:', 'stems:' or 'allow:'")
        {"name": names, "term": terms, "allow": allow}.get(key, set()).add(value)
        if key == "stems":
            stems.append(os.path.expanduser(value))
    if stems:
        words = load_words(dict_path)
        if not words:
            raise CheckError(f"no dictionary at {dict_path}: note names cannot be filtered; pass --dict")
        for directory in stems:
            terms |= stem_names(directory, words)
    allowed = {a.lower() for a in allow}
    names = {n for n in names if n.lower() not in allowed}
    lowered = {n.lower() for n in names}
    terms = {t for t in terms if t.lower() not in allowed and t.lower() not in lowered}
    return names, terms


def trie_pattern(literals):
    """A regex alternation of the literals, folded into a prefix tree so it is fast to try."""
    trie = {}
    for lit in literals:
        node = trie
        for ch in lit:
            node = node.setdefault(ch, {})
        node[""] = {}

    def build(node):
        end = "" in node
        branches = [re.escape(ch) + build(child) for ch, child in sorted(node.items()) if ch]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        return "(?:" + body + ")?" if end else body

    return build(trie)


def joined(words):
    return {sep.join(words) for sep in SEPS}


def build_regexes(names, terms):
    loose = set()
    for name in names:
        loose |= joined(name.lower().split())
    exact = set()
    for term in terms:
        words = term.split()
        exact |= joined(words) | joined([w.upper() for w in words])
        if len(words) > 1:
            exact |= {"-".join(w.lower() for w in words), "_".join(w.lower() for w in words)}
    slugs = {t.lower() for t in terms if " " not in t}
    out = []
    if slugs:  # a lowercase slug: "- name:", "character:name", "name.md", ".../name/..."
        body = trie_pattern(sorted(slugs))
        out.append(re.compile(r"(?:(?<=character:)(" + body + r")(?![a-z])|(?<![A-Za-z0-9])(" + body
                              + r")(?=:|\.md\b|/))"))
    if loose:
        out.append(re.compile(START + "((?i:" + trie_pattern(sorted(loose)) + "))" + END))
    if exact:
        out.append(re.compile(START + "(" + trie_pattern(sorted(exact)) + ")" + END))
    return out


def git_files(root):
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        raise CheckError("git ls-files failed: " + out.stderr.decode("utf-8", "replace").strip())
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def tracked_files(root):
    """jj's file list first (in a colocated checkout git's index lags jj's working copy), then git's."""
    if not (root / ".jj").is_dir() and (root / ".git").exists():
        return git_files(root)
    if (root / ".jj").is_dir():
        jj = os.path.expanduser("~/bin/jj")
        jj = jj if os.path.exists(jj) else "jj"
        try:  # never snapshot the working copy: the check must not change the repository
            out = subprocess.run([jj, "--ignore-working-copy", "file", "list"], cwd=root, capture_output=True)
        except FileNotFoundError:
            raise CheckError("this is a jj workspace and jj is not installed")
        if out.returncode != 0:
            raise CheckError("jj file list failed: " + out.stderr.decode("utf-8", "replace").strip())
        return [p for p in out.stdout.decode("utf-8", "replace").split("\n") if p]
    files = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", ".jj")]
        files += [os.path.relpath(os.path.join(base, f), root) for f in names]
    return sorted(files)


def text_lines(label, data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    for n, line in enumerate(text.splitlines(), 1):
        yield f"{label}:{n}", line


def worktree_lines(root):
    for rel in tracked_files(root):
        yield f"{rel}:0", rel
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        yield from text_lines(rel, data)


def rev_lines(root, rev):
    out = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", rev], capture_output=True)
    if out.returncode != 0:
        raise CheckError(f"cannot read {rev}: {out.stderr.decode('utf-8', 'replace').strip()}")
    label = rev[:12] if re.fullmatch(r"[0-9a-f]{40}", rev) else rev
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
        for member in tar:
            fh = tar.extractfile(member) if member.isfile() else None
            if fh is None:
                continue
            yield f"{label}:{member.name}:0", member.name
            yield from text_lines(f"{label}:{member.name}", fh.read())


def git(root, *args, stdin=None, timeout=None, env=None):
    out = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(root), *args], input=stdin,
                         capture_output=True, timeout=timeout, env=env)
    out.text = out.stdout.decode("utf-8", "replace")
    out.err = out.stderr.decode("utf-8", "replace").strip()
    return out


def known_commits(root, shas):
    """The shas this clone holds as commits, in one cat-file call."""
    shas = [s for s in shas if s.strip("0")]
    if not shas:
        return []
    out = git(root, "cat-file", "--batch-check", stdin="".join(f"{s}^{{commit}}\n" for s in shas).encode())
    lines = out.text.splitlines()
    return [s for s, line in zip(shas, lines) if not line.endswith("missing")]


def destination_heads(root, remote, url):
    """Commits the destination already holds, so they are not checked again: what `git ls-remote` on the URL
    being pushed to reports now, plus the remote's tracking refs when it lists commits this clone has not
    fetched; offline or slow, the tracking refs alone; with no remote named, every tracking ref."""
    if remote is None and url is None:
        return ["--remotes"]
    named = remote if remote and remote in git(root, "remote").text.split() else None
    tracking = [f"--remotes={named}"] if named else []
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        listed = git(root, "ls-remote", url or remote, timeout=20, env=env, stdin=b"")
    except subprocess.TimeoutExpired:
        return tracking
    if listed.returncode != 0:
        return tracking
    shas = sorted({line.split()[0] for line in listed.text.splitlines() if line.strip()})
    known = known_commits(root, shas)
    return known + (tracking if len(known) < len(shas) else [])


def ref_lowercase_regex(names, terms):
    """Ref names are slugs: every name and term, lowercase, between non-alphanumerics."""
    forms = set()
    for t in list(names) + list(terms):
        words = t.lower().split()
        forms |= {"-".join(words), "_".join(words), "".join(words)}
    if not forms:
        return None
    return re.compile(r"(?<![a-z0-9])(" + trie_pattern(sorted(forms)) + r")(?![a-z0-9])")


def pushed_lines(root, stdin, remote=None, url=None):
    """What a push sends: each remote ref name, tag message, and new commits' messages, paths and added lines."""
    bases = None
    for raw in stdin:
        parts = raw.split()
        if len(parts) != 4:
            continue
        _local_ref, local, remote_ref, old = parts
        if not local.strip("0"):
            continue  # a deletion sends nothing, and deleting a badly named branch must stay possible
        yield "ref", remote_ref
        obj = local
        while True:
            kind = git(root, "cat-file", "-t", obj)
            if kind.returncode != 0:
                raise CheckError(f"cannot read pushed object {obj}")
            if kind.text.strip() != "tag":
                break
            body = git(root, "cat-file", "tag", obj).text
            head, _, message = body.partition("\n\n")
            for line in message.split("\n"):
                yield f"tag:{remote_ref}", line
            obj = next((l.split()[1] for l in head.splitlines() if l.startswith("object ")), "")
        peeled = git(root, "rev-parse", "--verify", "--quiet", local + "^{commit}")
        if peeled.returncode != 0:
            continue  # a tag of a tree or blob: its message was checked, its content is not
        if bases is None:
            bases = destination_heads(root, remote, url)
        span = "\n".join([peeled.text.strip(), "--not", *bases, *known_commits(root, [old])]) + "\n"
        revs = git(root, "rev-list", "--stdin", stdin=span.encode())
        if revs.returncode != 0:
            raise CheckError("git rev-list failed: " + revs.err)
        for rev in revs.text.split():
            names = git(root, "diff-tree", "-m", "-r", "--root", "--no-commit-id", "--name-only", "-z",
                        "--diff-filter=ACMRT", rev)
            if names.returncode != 0:
                raise CheckError("git diff-tree failed: " + names.err)
            for path in sorted(set(p for p in names.text.split("\0") if p)):
                yield f"{rev[:12]}:{path}:0", path
        out = git(root, "log", "--stdin", "-p", "--root", "--diff-merges=separate", "-U0", "--text", "--no-textconv",
                  "--no-color", "--no-ext-diff", "--format=%x00COMMIT %H%n%B%n%x00END", stdin=span.encode())
        if out.returncode != 0:
            raise CheckError("git log failed: " + out.err)
        yield from parse_log(out.text)


def parse_log(text):
    sha, path, state, section = "", "", "header", []

    def flush():
        # a file section holding a NUL is binary: --text shows it, but it is not scanned as text
        if not any("\0" in line for _, line in section):
            yield from section
        section.clear()

    for line in text.split("\n"):  # never splitlines: \x0c is text here
        if line.startswith("\0COMMIT "):
            yield from flush()
            sha, path, state = line.split()[1][:12], "", "message"
            continue
        if state == "message":
            if line.startswith("\0END"):
                state = "header"
            else:
                yield f"{sha}:message", line
            continue
        if line.startswith("diff --git "):
            yield from flush()
            state = "header"
        elif state == "header" and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            state = "hunk"
        elif state == "hunk" and line.startswith("+"):
            section.append((f"{sha}:{path}", line[1:]))
    yield from flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("--root", help="the repository root (default: this script's checkout)")
    ap.add_argument("--config")
    ap.add_argument("--dict", default="/usr/share/dict/words")
    ap.add_argument("--locations", action="store_true",
                    help="print file:line instead of the matched text (a path that is itself the hit still shows)")
    ap.add_argument("--home-only", action="store_true",
                    help="look for the config only under $ASHA_HOME and $HOME, not the account's home")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--rev", help="check the files of this commit instead of the working copy")
    mode.add_argument("--pre-push", action="store_true",
                      help="as a git pre-push hook: check every pushed ref and new commit named on stdin")
    ap.add_argument("--remote", help="with --pre-push: the remote's name (the hook's $1)")
    ap.add_argument("--url", help="with --pre-push: the URL being pushed to (the hook's $2); what it already "
                                  "holds is not checked again")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    config, explicit = config_path(args.config, args.home_only)
    if not os.path.isfile(config):
        if explicit:
            print(f"check-private-names: no config at {config}", file=sys.stderr)
            return 2
        print(f"check-private-names: skipped (no config at {config})")
        return 0
    try:
        names, terms = read_config(config, args.dict)
        if not names and not terms:
            print("check-private-names: clean: 0 names")
            return 0
        regexes = build_regexes(names, terms)
        ref_rx = None
        if args.pre_push:
            lines = pushed_lines(root, sys.stdin, args.remote, args.url)
            ref_rx = ref_lowercase_regex(names, terms)
        elif args.rev:
            lines = rev_lines(root, args.rev)
        else:
            lines = worktree_lines(root)
        hits = checked = 0
        for where, line in lines:
            checked += 1
            found = []
            for rx in regexes + ([ref_rx] if ref_rx and where == "ref" else []):
                for m in rx.finditer(line):
                    g = next(x for x in m.groups() if x)
                    if not (g.isupper() and m.end() < len(line) and line[m.end()].isupper()):
                        found.append(g)
            if found:
                hits += len(found)
                print(where if args.locations else "\n".join(f"{where}: {f}" for f in found))
    except (OSError, CheckError) as exc:
        print(f"check-private-names: {exc}", file=sys.stderr)
        return 2
    if hits:
        print(f"check-private-names: {hits} private name(s); asha is public", file=sys.stderr)
        return 1
    print(f"check-private-names: clean: {len(names) + len(terms)} names, {checked} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
