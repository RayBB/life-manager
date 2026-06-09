# Life Manager

CLI for managing commitments, projects, and activity logs — designed for an AI life manager. Tasks live in Todoist (managed via `td` CLI); this tool syncs them in and provides the higher-level view.

## For an AI Life Manager

### Daily check-in

```bash
uv run python query_grist.py overview --json
```

This one call gives you everything: stalled projects, untouched projects, tasks needing attention (overdue / due this week / no due date), recent activity, and per-commitment rollups. Use `--json` to parse the structure.

### Drill into anything

```bash
uv run python query_grist.py query "Commitment or Project Name" --json
```

### Create tasks (Todoist CLI)

Tasks are created in Todoist and linked to commitments/projects via labels:

```bash
# Quick add — labels, priority, and due date all via natural language
td task quickadd "Review PR tomorrow p1 #GarageCity"

# Or use flags for explicit control
td task add "Write chapter 3 outline" --labels "Mobility Language Matters,Lab of Thought" --due "Jun 15" --priority p2

# Complete a task (use the ID from overview --json)
td task complete 6gqV993Wqrf3jGRj

# Update priority (use the ID from overview --json)
td task update 6gqV993Wqrf3jGRj --priority p1
```

**Getting task IDs:** Run `overview --json` or `query "Project Name" --json` — both include an `"id"` field for each task.

### Log activity

```bash
uv run python query_grist.py log add "Had a great planning session" --project "Mobility Language Matters"
uv run python query_grist.py log add "Kickoff meeting" --commitment "hackNY" --date "2026-06-10"
```

### Manage commitments and projects

```bash
uv run python query_grist.py commitment create "Urbanism Now" --description "Working on urbanist projects"
uv run python query_grist.py project create "Mobility Language Matters" --commitment "Lab of Thought"
uv run python query_grist.py project update "Mobility Language Matters" --description "It's a book."
uv run python query_grist.py project update "Mobility Language Matters" --commitment "New Commitment"
uv run python query_grist.py project status "Garage City" --stalled
```

### Browse logs

```bash
uv run python query_grist.py log list --limit 10 --json
uv run python query_grist.py log search "meeting" --json
uv run python query_grist.py log view 42
```

## The mental model

```
Commitment (e.g. "Lab of Thought")
  └── Projects (e.g. "Mobility Language Matters")
        ├── LogEntries — notes about what happened
        └── Todoist tasks — linked via labels like "Mobility Language Matters"
```

- **Commitments** are high-level areas. Create/rename them here.
- **Projects** sit under commitments. Create, rename, reparent, set status here.
- **Log entries** are freeform notes about activity. Add/search/view them here.
- **Tasks** live in Todoist. Create, complete, prioritize them via `td`.

All commands auto-sync Todoist data before running (5-minute cooldown).

## Command reference

### `uv run python query_grist.py`

| Command | `--json` |
|---|---|
| `overview` | ✅ |
| `query <name>` | ✅ |
| `commitment list` | ✅ |
| `commitment create <title>` | |
| `commitment update <name>` | |
| `project list` | ✅ |
| `project create <title>` | |
| `project update <name>` | |
| `project status <name>` | |
| `log add <content>` | |
| `log list` | ✅ |
| `log search <term>` | ✅ |
| `log view <id>` | ✅ |
| `log update <id>` | |
| `log delete <id>` | |

### `uv run python sync_todoist_to_grist.py`

`sync` — Sync Todoist tasks to Grist (5-min cooldown, use `--force` to override). You usually don't need this — queries auto-sync.

### `td` (Todoist CLI)

| Command | Purpose |
|---|---|
| `td task add <content> --labels <a,b>` | Create a task |
| `td task quickadd <text>` | Natural language create |
| `td task complete <id>` | Mark done (use ID from `overview --json`) |
| `td task update <id> --priority p1` | Change priority (use ID from `overview --json`) |
| `td task list` | List tasks |
