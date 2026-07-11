# Admin Plugin

**Version**: 0.1.0

Direct integrations for personal administration, grounded search, computation, and knowledge management.

## Skills

| Skill | Purpose | Requirement |
|---|---|---|
| `bookstack` | Search and manage a BookStack instance through its REST API | `BOOKSTACK_BASE_URL`, `BOOKSTACK_API_TOKEN` |
| `gemini` | Single-shot Google-grounded search with citations | Gemini API credentials documented by the skill |
| `todoist` | Create, find, update, and complete Todoist tasks | `TODOIST_API_TOKEN` |
| `wolfram` | Computational and factual queries through Wolfram | Wolfram credentials documented by the skill |

Each skill is self-contained under `skills/<name>/SKILL.md`. Invoke it by name or describe a matching task and allow the harness to select it.

## Usage

Request the administrative operation directly. The matching skill owns API authentication, request construction, and result formatting.

## Installation

```bash
./install.sh --only admin
```

## License

MIT
