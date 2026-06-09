# Grist + Todoist Project Manager

A Python CLI toolchain that syncs Todoist tasks into **Grist** (a collaborative spreadsheet/database) and enables querying projects, commitments, activity logs, and Todoist tasks from the terminal.

## Architecture

```
Todoist (task manager)
    │
    │  sync_todoist_to_grist.py  (one-way sync →)
    ▼
Grist (database + UI)
    │
    │  query_grist.py  (read queries)
    ▼
Terminal (CLI output)
```

**Data hierarchy:**

```
Commitment (e.g., "Lab of Thought", "Urbanism Now")
    └── Projects (e.g., "Mobility Language Matters", "NBMI 2026")
            ├── LogEntries (activity log — freeform notes)
            └── Todoist tasks (linked via labels)
```

## Grist Tables

The Grist document contains these tables:

| Table | Purpose | Key Fields |
|---|---|---|
| `Commitments` | High-level goals/areas | `Title`, `Description`, `Project` (RefList) |
| `Project` | Specific projects within commitments | `Title`, `Description`, `Commitment` (Ref) |
| `Todoist` | Synced Todoist tasks | `Content`, `Labels` (list), `Checked`, `DueDate`, `UpdatedAt` |
| `LogEntries` | Activity log / notes | `Content`, `Author`, `EffectiveDate`, `Target_Project` (Ref) |


## Linking Todoist → Projects

Todoist tasks are linked to Grist projects through **Todoist labels**. The recommended approach:

1. **Commitment label** (you already have these): e.g., `Lab of Thought`, `Urbanism Now`, `hackNY`
2. **Project label** (optional, recommended for project-level queries): e.g., `Mobility Language Matters`, `NBMI 2026`

A Todoist task can have **multiple labels**, so a task can belong to both a commitment and a project:

```
Task: "Update citation review skill"
Labels: Lab of Thought, Mobility Language Matters
       ↑ commitment         ↑ project
```

This lets you query at either level:
- `query_grist.py query "Lab of Thought"` → all tasks for that commitment
- `query_grist.py query "Mobility Language Matters"` → only project-specific tasks

## Scripts

### `sync_todoist_to_grist.py` — Sync Todoist → Grist

```bash
uv run python sync_todoist_to_grist.py sync
```

Fetches all active tasks (with pagination) and the last 100 completed tasks from Todoist API v1, then upserts them into the Grist `Todoist` table. Handles the transformation from Todoist's API format to Grist's column schema.

> **Note:** This script also contains query functions internally, but `query_grist.py` (below) is the recommended tool for all read operations.

### `query_grist.py` — Query Grist from the terminal

```bash
# List all commitments
uv run python query_grist.py commitments

# List all projects (with last-action summary)
uv run python query_grist.py projects

# Query a specific commitment or project
uv run python query_grist.py query "Lab of Thought"
uv run python query_grist.py query "Mobility Language Matters"
```

**What `projects` shows:**
```
ALL PROJECTS (10)
============================================================
  • Mobility Language Matters [Lab of Thought]
      Activity: Replied to Ming about commission based compensation (2026-06-03)
  • NBMI 2026 [Lab of Thought]
  • Garage City
      Activity: This is a test comment... (2026-06-04)
  • Urbanism Now Jobs Site [Urbanism Now]
  ...
```
Each project shows its **last action** — the most recent of either a completed Todoist task or a LogEntries activity log entry.

**What `query` shows:**
```
PROJECT: Mobility Language Matters
============================================================
Description: It's a book.
Commitment: Lab of Thought

--- Recent Activity ---
  [2026-06-03] Replied to Ming about commission...

--- Todoist Tasks ---
  Upcoming (2):
    ○ Write chapter 3 outline (due: Jun 15)

  Recently Completed (showing 3):
    ✓ Update paper citation review skill
    ✓ Send LOT D2D invoice
```

## Data Flow

1. **Create/complete tasks in Todoist** (your daily workflow)
2. **Run sync** → `uv run python sync_todoist_to_grist.py sync`
3. **Query** → `uv run python query_grist.py projects` or `query "Project Name"`

The sync is a one-way pull from Todoist into Grist. For Todoist data, Grist is read-only — all task edits happen in Todoist. Activity log entries (LogEntries) are created directly in Grist.

## Activity Logging

Record what you're working on without leaving the terminal:

```bash
# Log to a project
uv run python query_grist.py log "Replied to Ming about commission" --project "Mobility Language Matters"

# Log to a commitment (appears under all its projects)
uv run python query_grist.py log "Kickoff meeting" --commitment "hackNY"

# With a specific date/time for past events
uv run python query_grist.py log "Had a great meeting" --project "Garage City" --date "2026-06-02 2:30pm"

# With an author
uv run python query_grist.py log "Sent the invoice" --project "NBMI 2026" --author "Ray" --date "2026-06-04"
```

Requires either `--project` or `--commitment` (not both). The log entry appears in:
- `projects` output (last action summary)
- `query "Project Name"` → Recent Activity
- `query "Commitment Name"` → Recent Activity

## Setup

Requires Python 3.10+ and `uv` package manager.

```bash
uv run python sync_todoist_to_grist.py sync  # First sync
uv run python query_grist.py projects         # See your projects
```

API keys are currently hardcoded in the scripts. To change them, edit the configuration section at the top of each file.
