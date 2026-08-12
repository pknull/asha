# Admin Plugin

**Version**: 0.3.0

Direct integrations for personal administration, grounded search, computation, and knowledge management.

## How to use it

Admin contains skills, not slash commands or agents. Ask for the operation
directly; the harness selects the matching skill. Name the skill when the task
could route to more than one integration.

```text
Use todoist to create a task due tomorrow.
Use gemini to find current primary sources for this claim.
Use wolfram to verify this calculation.
Use bookstack to find the deployment runbook.
Use proton-mail to search for the invoice, but do not send anything.
```

The same request form works on Claude, Codex, Copilot, and OpenCode. Skill names may be
prefixed by the installer in the target catalogue, but their trigger descriptions
remain available to natural-language routing.

## Skills

| Skill | Purpose | Requirement |
|---|---|---|
| `bookstack` | Search and manage a BookStack instance through its REST API | `BOOKSTACK_BASE_URL`, `BOOKSTACK_API_TOKEN` |
| `gemini` | Single-shot Google-grounded search with citations | Gemini API credentials documented by the skill |
| `proton-mail` | Read and manage Proton Mail through localhost-only Proton Mail Bridge IMAP/SMTP | `PROTON_BRIDGE_USERNAME`, `PROTON_BRIDGE_PASSWORD`; optional CA certificate |
| `todoist` | Create, find, update, and complete Todoist tasks | `TODOIST_API_TOKEN` |
| `wolfram` | Computational and factual queries through Wolfram | Wolfram credentials documented by the skill |

Each skill is self-contained under `skills/<name>/SKILL.md`. Invoke it by name or describe a matching task and allow the harness to select it.

## Read and write boundaries

The matching skill owns authentication, request construction, and result
formatting. Read requests may execute immediately. Creating, sending, editing,
moving, completing, or deleting external data follows the skill's confirmation
and preview rules. Credentials remain in environment/configuration channels and
must not be copied into prompts, reports, or repository files.

## Installation

```bash
./install.sh --only admin --target claude
./install.sh --only admin --target codex
./install.sh --only admin --target copilot
```

## License

MIT
