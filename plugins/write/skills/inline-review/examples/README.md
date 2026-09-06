# Synthetic source-editing demonstration

Open these Markdown files in a plain source editor, not just rendered preview:

1. `annotated.md`: C1 brackets two paragraphs. The compact note is immediately
   beneath the passage; prose and blank lines have not been rewritten.
2. `with-reply.md`: the author-supplied line joins that same C1 thread.
3. `with-followup.md`: an attributed LLM follow-up acknowledges the local reply;
   status stays open because only the author directs closure.
4. `clean.md`: the shared projection of all three files, byte for byte.

These are separate snapshots, not multiple IDs in one manuscript. Every passage
and observation here is synthetic. This is a source convention, not a GUI:
rendered Markdown normally hides the HTML comments and thread discussion.

Run `python3 scripts/inline_review.py show examples/with-followup.md C1` from the
skill directory to read the thread. Run `project examples/with-followup.md --json`
with the same script for clean text and source-line mapping. No live Claude,
Codex or Copilot behavioral smoke was run; deterministic fixtures and static installer
exposure checks are the evidence, not proof of live editorial behavior.
