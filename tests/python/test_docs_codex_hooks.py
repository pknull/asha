"""Current documentation describes Codex hooks as the adapter writes them (#123).

The Codex adapter publishes an installer-owned hooks.json and only reads the
native config.toml. Release notes (CHANGELOG.md, plugin README histories) and
delivered proposals keep their historical wording and are not scanned.

The phrase check is a lint for the known wordings, not a parser: it flags
hooks followed closely by TOML, "TOML hooks", and hooks placed in config.toml
(but not the hooks feature flag or hook trust, which do live there). A true
sentence that pairs hooks with one format and something else with TOML
("hooks as JSON and agents as TOML") also trips it; split it.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT = ('README.md', 'INSTALLER.md', 'AGENTS.md', 'CLAUDE.md',
           *sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'docs').glob('*.md')),
           *sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'plugins').glob('*/skills/*/SKILL.md')))
# Spans stop at a period or a table cell; TOML is case-sensitive, so the
# native config.toml file name never counts as the format.
TOML_HOOKS = (
    re.compile(r'(?i:\bhooks?\b)[^.|]{0,80}\bTOML\b'),
    re.compile(r'\bTOML(?:[- ]\w+)?\s+(?i:hooks?)\b'),
    re.compile(r'(?i)\bhooks?\b[^|]{0,60}?\b(?:in|into|to)\s+(?:the\s+)?(?:live\s+|native\s+|shared\s+)?'
               r'`?(?:~/\.codex/|\$CODEX_HOME/)?config\.toml'),
)
# The feature flag and hook trust are what config.toml does keep.
NATIVE_SETTING = re.compile(r'(?i)trust|feature|flag')


def paragraphs(relative):
    return [' '.join(p.split()) for p in re.split(r'\n\s*\n', (ROOT / relative).read_text())]


def toml_hook_claims(text):
    return [m.group(0) for pattern in TOML_HOOKS for m in pattern.finditer(text)
            if pattern is not TOML_HOOKS[-1] or not NATIVE_SETTING.search(text[m.start():m.end() + 40])]


class CodexHookDocumentationTests(unittest.TestCase):
    def test_current_documentation_never_describes_codex_hooks_as_toml(self):
        for relative in CURRENT:
            with self.subTest(document=relative):
                self.assertEqual([claim for text in paragraphs(relative) for claim in toml_hook_claims(text)], [])

    def test_the_memory_guide_names_the_hook_file_the_codex_adapter_writes(self):
        adapter = (ROOT / 'harnesses/codex.sh').read_text()
        written = re.search(r'^CODEX_HOOKS_FILE="\$CODEX_HOME/([^"]+)"$', adapter, re.M).group(1)
        guide = ' '.join((ROOT / 'docs/memory-architecture.md').read_text().split())
        seams = guide.split('## Harness seams', 1)[1].split(' ## ', 1)[0]
        codex = re.search(r'- Codex (.*?)(?= - [A-Z]|$)', seams).group(1)
        self.assertIn(f'`{written}`', codex)
        self.assertIn('`config.toml`', codex)


if __name__ == '__main__':
    unittest.main()
