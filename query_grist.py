#!/usr/bin/env python3
"""
Query Grist for Projects, Commitments, and Todoist tasks
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import typer
from pydantic import BaseModel, Field, field_validator

from grist_api import grist_get, grist_patch, grist_post, rows_from_data
from sync_todoist_to_grist import _last_sync_ago, sync_if_due

app = typer.Typer(no_args_is_help=True)
log_app = typer.Typer(no_args_is_help=True)
commitment_app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
app.add_typer(
    log_app, name="log", help="Activity log entries (add, list, search, view, update, delete)"
)
app.add_typer(commitment_app, name="commitment", help="Manage commitments (list, create, update)")
app.add_typer(project_app, name="project", help="Manage projects (list, create, update, status)")


@app.callback()
def _pre_sync() -> None:
    """Sync Todoist data before every command. Respects the 5-minute cooldown."""
    asyncio.run(sync_if_due(quiet=True))


class ProjectStatus(str, Enum):
    active = "active"
    stalled = "stalled"
    waiting = "waiting"
    done = "done"


class Commitment(BaseModel):
    id: int
    title: str = Field("", alias="Title")
    description: str = Field("", alias="Description")


class GristProject(BaseModel):
    id: int
    title: str = Field("", alias="Title")
    description: str = Field("", alias="Description")
    commitment: int | None = Field(None, alias="Commitment")
    status: str | None = Field(None, alias="Status")


class TodoistItem(BaseModel):
    id: int
    todoist_id: str = Field("", alias="TodoistId")
    content: str = Field("", alias="Content")
    checked: bool = Field(False, alias="Checked")
    labels: list[str] = Field([], alias="Labels")
    due_date: str | None = Field(None, alias="DueDate")
    due_string: str = Field("", alias="DueString")
    project_id: str = Field("", alias="ProjectId")
    priority: int = Field(1, alias="Priority")
    added_at: str = Field("", alias="AddedAt")
    updated_at: str = Field("", alias="UpdatedAt")

    @field_validator("labels", mode="before")
    @classmethod
    def coerce_labels(cls, v: object) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        if not v:
            return []
        return [str(v)]


class LogEntry(BaseModel):
    id: int
    log_id: int | None = Field(None, alias="LogId")
    content: str = Field("", alias="Content")
    effective_date: int | str | None = Field(None, alias="EffectiveDate")
    target_project: int | None = Field(None, alias="Target_Project")
    target_commitment: int | None = Field(None, alias="Target_Commitment")
    target_task: int | None = Field(None, alias="Target_Task")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_todoist_index(items: list[TodoistItem]) -> dict[str, list[TodoistItem]]:
    """Build a dict mapping label -> list of tasks from Todoist data."""
    label_index: dict[str, list[TodoistItem]] = {}
    for item in items:
        if len(item.labels) > 1:
            for label in item.labels[1:]:
                label_index.setdefault(label, []).append(item)
    return label_index


def _build_log_index(entries: list[LogEntry]) -> dict[int, list[LogEntry]]:
    """Build a dict mapping target_project_id -> list of log entries."""
    log_index: dict[int, list[LogEntry]] = {}
    for entry in entries:
        if entry.target_project:
            log_index.setdefault(entry.target_project, []).append(entry)
    return log_index


async def _log_id_to_grist_id(log_id: int) -> int | None:
    """Look up a Grist internal ID from a stable LogId."""
    for e in rows_from_data(await grist_get("LogEntries"), LogEntry):
        if e.log_id == log_id:
            return e.id
    return None


async def grist_query_by_label(label_name: str) -> list[TodoistItem]:
    """Get all Todoist tasks with a specific label."""
    items = rows_from_data(await grist_get("Todoist"), TodoistItem)
    index = _build_todoist_index(items)
    return index.get(label_name, [])


async def get_commitment(title: str) -> Commitment | None:
    """Get a commitment by title."""
    for c in rows_from_data(await grist_get("Commitments"), Commitment):
        if c.title == title:
            return c
    return None


async def get_project(title: str) -> GristProject | None:
    """Get a project by title."""
    for p in rows_from_data(await grist_get("Project"), GristProject):
        if p.title == title:
            return p
    return None


async def get_log_entries(
    target_project_id: int | None = None,
    target_commitment_id: int | None = None,
    target_task_id: int | None = None,
    limit: int = 20,
    log_index: dict[int, list[LogEntry]] | None = None,
) -> list[LogEntry]:
    """Get log entries, optionally filtered by target."""
    if log_index is not None and target_project_id:
        entries = log_index.get(target_project_id, [])
        entries.sort(key=lambda x: x.effective_date or "", reverse=True)
        return entries[:limit]

    entries = rows_from_data(await grist_get("LogEntries"), LogEntry)

    if target_project_id:
        entries = [e for e in entries if e.target_project == target_project_id]
    if target_commitment_id:
        entries = [e for e in entries if e.target_commitment == target_commitment_id]
    if target_task_id:
        entries = [e for e in entries if e.target_task == target_task_id]

    entries.sort(key=lambda x: x.effective_date or "", reverse=True)
    return entries[:limit]


def format_timestamp(ts: Any) -> str:
    """Format a Unix timestamp (int) or ISO string to YYYY-MM-DD."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return str(ts)[:10]


async def get_last_action_for_project(
    project_id: int,
    project_title: str,
    todoist_index: dict[str, list[TodoistItem]],
    log_index: dict[int, list[LogEntry]],
) -> dict[str, str] | None:
    """Get the most recent action for a project."""
    best: dict[str, str] | None = None
    best_date: str | None = None

    for t in todoist_index.get(project_title, []):
        if t.checked and t.due_date and (best_date is None or t.due_date > best_date):
            best_date = t.due_date
            best = {"type": "Todoist", "content": t.content[:50], "date": t.due_date}

    activity = await get_log_entries(target_project_id=project_id, limit=1, log_index=log_index)
    if activity:
        log_date = format_timestamp(activity[0].effective_date)
        if log_date and (best_date is None or log_date > best_date):
            best = {"type": "Activity", "content": activity[0].content[:50], "date": log_date}

    return best


async def _get_next_log_id() -> int:
    """Find the next available LogId by taking max existing + 1."""
    entries = rows_from_data(await grist_get("LogEntries"), LogEntry)
    valid = [e.log_id for e in entries if e.log_id is not None]
    return max(valid) + 1 if valid else 1


# ---------------------------------------------------------------------------
# Mutations — no display branching needed
# ---------------------------------------------------------------------------


async def add_log_entry(
    content: str,
    project_id: int | None = None,
    commitment_id: int | None = None,
    activity_date: str | None = None,
) -> bool:
    """Create a log entry in Grist's LogEntries table."""
    log_id = await _get_next_log_id()
    fields: dict[str, Any] = {"Content": content, "LogId": log_id}

    if project_id:
        fields["Target_Project"] = project_id
    if activity_date:
        fields["ActivityDate"] = activity_date
    if commitment_id:
        fields["Target_Commitment"] = commitment_id

    resp = await grist_post("tables/LogEntries/records", {"records": [{"fields": fields}]})

    if resp.status_code in (200, 201):
        print(f"\u2713 Log entry created (L#{log_id})")
        return True
    else:
        print(f"\u2717 Failed to create log entry: {resp.status_code}")
        print(f"  {resp.text}")
        return False


async def update_log_entry(
    log_id: int, content: str | None = None, activity_date: str | None = None
) -> bool:
    """Update a log entry's content and/or date using its stable LogId."""
    grist_id = await _log_id_to_grist_id(log_id)
    if grist_id is None:
        print(f"Error: No log entry found with LogId {log_id}")
        return False

    fields: dict[str, Any] = {}
    if content is not None:
        fields["Content"] = content
    if activity_date is not None:
        fields["ActivityDate"] = activity_date

    if not fields:
        print("Error: Nothing to update")
        return False

    resp = await grist_patch("LogEntries", [{"id": grist_id, "fields": fields}])

    if resp.status_code in (200, 201):
        print(f"\u2713 Log entry L#{log_id} updated")
        return True
    else:
        print(f"\u2717 Failed to update log entry: {resp.status_code}")
        print(f"  {resp.text}")
        return False


async def delete_log_entry(log_id: int) -> bool:
    """Delete a log entry from Grist using its stable LogId."""
    grist_id = await _log_id_to_grist_id(log_id)
    if grist_id is None:
        print(f"Error: No log entry found with LogId {log_id}")
        return False

    resp = await grist_post("apply", [["BulkRemoveRecord", "LogEntries", [grist_id]]])

    if resp.status_code in (200, 201):
        print(f"\u2713 Log entry L#{log_id} deleted")
        return True
    else:
        print(f"\u2717 Failed to delete log entry: {resp.status_code}")
        print(f"  {resp.text}")
        return False


# ---------------------------------------------------------------------------
# Data layer — async functions returning JSON-serializable structures
# ---------------------------------------------------------------------------


async def _get_commitments_data() -> list[dict]:
    """Fetch all commitments as plain dicts."""
    items = rows_from_data(await grist_get("Commitments"), Commitment)
    return [{"id": c.id, "title": c.title, "description": c.description} for c in items]


async def _get_projects_data() -> list[dict]:
    """Fetch all projects with commitment, status, and last action."""
    project_raw, todoist_raw, log_raw, commit_raw = await asyncio.gather(
        grist_get("Project"),
        grist_get("Todoist"),
        grist_get("LogEntries"),
        grist_get("Commitments"),
    )

    project_list = rows_from_data(project_raw, GristProject)
    if not project_list:
        return []

    todoist_index = _build_todoist_index(rows_from_data(todoist_raw, TodoistItem))
    log_index = _build_log_index(rows_from_data(log_raw, LogEntry))
    commitments = {c.id: c.title for c in rows_from_data(commit_raw, Commitment)}

    data = []
    for p in project_list:
        c_name = commitments.get(p.commitment) if p.commitment else None
        last = await get_last_action_for_project(p.id, p.title, todoist_index, log_index)
        data.append({
            "id": p.id,
            "title": p.title,
            "description": p.description,
            "status": p.status,
            "commitment": c_name,
            "last_action": last,
        })
    return data


async def _get_commitment_data(name: str) -> dict | None:
    """Fetch a commitment with its projects, activity, and tasks. Returns None if not found."""
    commitment = await get_commitment(name)
    if not commitment:
        return None

    project_raw, log_raw, todoist_raw = await asyncio.gather(
        grist_get("Project"),
        grist_get("LogEntries"),
        grist_get("Todoist"),
    )

    matching = [
        p for p in rows_from_data(project_raw, GristProject) if p.commitment == commitment.id
    ]

    all_logs = rows_from_data(log_raw, LogEntry)
    activity = [e for e in all_logs if e.target_commitment == commitment.id]
    activity.sort(key=lambda x: x.effective_date or "", reverse=True)
    activity = activity[:10]

    tasks = _build_todoist_index(rows_from_data(todoist_raw, TodoistItem)).get(name, [])
    upcoming = [t for t in tasks if not t.checked]
    completed = [t for t in tasks if t.checked]

    return {
        "type": "commitment",
        "title": commitment.title,
        "description": commitment.description,
        "projects": [{"title": p.title, "status": p.status} for p in matching],
        "recent_activity": [
            {
                "log_id": a.log_id,
                "date": format_timestamp(a.effective_date),
                "content": a.content,
            }
            for a in activity
        ],
        "todoist_tasks": {
            "upcoming": [
                {
                    "id": t.id,
                    "content": t.content,
                    "due_date": t.due_date,
                    "due_string": t.due_string,
                    "priority": t.priority,
                }
                for t in upcoming
            ],
            "completed": [
                {
                    "id": t.id,
                    "content": t.content,
                    "completed_at": t.updated_at,
                }
                for t in completed
            ],
        },
    }


async def _get_project_data(name: str) -> dict | None:
    """Fetch a project with its commitment, activity, and tasks. Returns None if not found."""
    project = await get_project(name)
    if not project:
        return None

    commit_raw, log_raw, todoist_raw = await asyncio.gather(
        grist_get("Commitments"),
        grist_get("LogEntries"),
        grist_get("Todoist"),
    )

    all_commitments = rows_from_data(commit_raw, Commitment)
    commitment_name = None
    if project.commitment:
        for c in all_commitments:
            if c.id == project.commitment:
                commitment_name = c.title
                break

    all_logs = rows_from_data(log_raw, LogEntry)
    activity = [e for e in all_logs if e.target_project == project.id]
    activity.sort(key=lambda x: x.effective_date or "", reverse=True)
    activity = activity[:10]

    todoist_index = _build_todoist_index(rows_from_data(todoist_raw, TodoistItem))
    project_tasks = todoist_index.get(name, [])
    commitment_tasks: list[TodoistItem] = []
    if project.commitment and commitment_name:
        commitment_tasks = todoist_index.get(commitment_name, [])

    seen: dict[int, TodoistItem] = {}
    for t in project_tasks + commitment_tasks:
        seen[t.id] = t
    all_tasks = list(seen.values())
    upcoming = [t for t in all_tasks if not t.checked]
    completed = [t for t in all_tasks if t.checked]

    return {
        "type": "project",
        "title": project.title,
        "description": project.description,
        "status": project.status,
        "commitment": commitment_name,
        "recent_activity": [
            {
                "log_id": a.log_id,
                "date": format_timestamp(a.effective_date),
                "content": a.content,
            }
            for a in activity
        ],
        "todoist_tasks": {
            "upcoming": [
                {
                    "id": t.id,
                    "content": t.content,
                    "due_date": t.due_date,
                    "due_string": t.due_string,
                    "priority": t.priority,
                }
                for t in upcoming
            ],
            "completed": [
                {
                    "id": t.id,
                    "content": t.content,
                    "completed_at": t.updated_at,
                }
                for t in completed
            ],
        },
    }


async def _get_logs_data(limit: int = 30, project_filter: str | None = None) -> list[dict]:
    """Fetch log entries with project names. Returns a list of dicts."""
    log_raw, project_raw = await asyncio.gather(
        grist_get("LogEntries"),
        grist_get("Project"),
    )
    entries = rows_from_data(log_raw, LogEntry)
    if not entries:
        return []

    project_titles = {p.id: p.title for p in rows_from_data(project_raw, GristProject)}

    result = []
    for e in entries:
        proj_name = project_titles.get(e.target_project, "") if e.target_project else ""
        if project_filter and project_filter.lower() not in proj_name.lower():
            continue
        result.append({
            "log_id": e.log_id,
            "content": e.content,
            "date": format_timestamp(e.effective_date),
            "project": proj_name,
        })

    result.sort(key=lambda x: x["date"], reverse=True)
    return result[:limit]


async def _get_search_logs_data(query: str, limit: int = 20) -> list[dict]:
    """Search log entries by content. Returns a list of dicts."""
    log_raw, project_raw = await asyncio.gather(
        grist_get("LogEntries"),
        grist_get("Project"),
    )
    entries = rows_from_data(log_raw, LogEntry)
    if not entries:
        return []

    project_titles = {p.id: p.title for p in rows_from_data(project_raw, GristProject)}
    q = query.lower()

    matches = []
    for e in entries:
        if q in e.content.lower():
            proj_name = project_titles.get(e.target_project, "") if e.target_project else ""
            matches.append({
                "log_id": e.log_id,
                "content": e.content,
                "date": format_timestamp(e.effective_date),
                "project": proj_name,
            })

    matches.sort(key=lambda x: x["date"], reverse=True)
    return matches[:limit]


async def _get_log_data(log_id: int) -> dict | None:
    """Fetch a single log entry by its LogId. Returns None if not found."""
    log_raw, project_raw = await asyncio.gather(
        grist_get("LogEntries"),
        grist_get("Project"),
    )
    project_titles = (
        {p.id: p.title for p in rows_from_data(project_raw, GristProject)} if project_raw else {}
    )

    for e in rows_from_data(log_raw, LogEntry):
        if e.log_id == log_id:
            proj_name = project_titles.get(e.target_project, "") if e.target_project else ""
            return {
                "log_id": e.log_id,
                "content": e.content,
                "date": format_timestamp(e.effective_date),
                "effective_date_raw": e.effective_date,
                "project": proj_name,
                "target_project_id": e.target_project,
                "target_commitment_id": e.target_commitment,
                "target_task_id": e.target_task,
            }
    return None


async def _get_overview_data() -> dict:
    """Fetch everything needed for the daily overview dashboard."""
    project_raw, todoist_raw, log_raw, commit_raw = await asyncio.gather(
        grist_get("Project"),
        grist_get("Todoist"),
        grist_get("LogEntries"),
        grist_get("Commitments"),
    )

    projects = rows_from_data(project_raw, GristProject) if project_raw else []
    commitments = rows_from_data(commit_raw, Commitment) if commit_raw else []
    todoist_items = rows_from_data(todoist_raw, TodoistItem) if todoist_raw else []
    log_entries = rows_from_data(log_raw, LogEntry) if log_raw else []

    todoist_index = _build_todoist_index(todoist_items)
    log_index = _build_log_index(log_entries)
    commitment_map = {c.id: c.title for c in commitments}
    project_titles = {p.id: p.title for p in projects}

    today = datetime.now(UTC)
    today_str = today.strftime("%Y-%m-%d")
    week_later_str = (today + timedelta(days=7)).strftime("%Y-%m-%d")

    stalled_or_untouched: list[dict[str, Any]] = []
    tasks_needing_attention: list[dict[str, Any]] = []
    total_upcoming = 0
    total_due_this_week = 0
    total_overdue = 0
    active_project_count = 0
    seen_task_ids: set[int] = set()

    commitment_stats: dict[str, dict[str, int]] = {}

    for p in projects:
        if p.status == "done":
            continue
        active_project_count += 1
        c_name = commitment_map.get(p.commitment) if p.commitment else None
        last = await get_last_action_for_project(p.id, p.title, todoist_index, log_index)

        # Days since last activity
        days_since: int | None = None
        if last and last.get("date"):
            try:
                d = datetime.strptime(last["date"], "%Y-%m-%d").replace(tzinfo=UTC)
                days_since = (today - d).days
            except ValueError:
                pass

        # Tasks for this project (project label + commitment label)
        # Track whether each task matched via project label or commitment label
        project_tasks: dict[int, TodoistItem] = {}
        task_source: dict[int, str] = {}
        for t in todoist_index.get(p.title, []):
            project_tasks[t.id] = t
            task_source[t.id] = "project"
        if c_name:
            for t in todoist_index.get(c_name, []):
                if t.id not in project_tasks:
                    project_tasks[t.id] = t
                    task_source[t.id] = "commitment"

        upcoming = [t for t in project_tasks.values() if not t.checked]
        # Deduplicate across projects (same task may have multiple labels)
        unique_upcoming = [t for t in upcoming if t.id not in seen_task_ids]
        for t in unique_upcoming:
            seen_task_ids.add(t.id)
        upcoming = unique_upcoming

        total_upcoming += len(upcoming)

        # Classify tasks by urgency
        due_this_week: list[dict[str, Any]] = []
        overdue: list[dict[str, Any]] = []
        no_due_date: list[dict[str, Any]] = []
        for t in upcoming:
            source = task_source.get(t.id, "project")
            project_label = (
                f"{c_name} (commitment)" if source == "commitment" and c_name else p.title
            )
            info: dict[str, Any] = {
                "id": t.id,
                "content": t.content,
                "priority": t.priority,
                "project": project_label,
                "commitment": c_name,
            }
            if t.due_date:
                d = t.due_date[:10]
                info["due_date"] = d
                if d < today_str:
                    info["status"] = "overdue"
                    overdue.append(info)
                elif d <= week_later_str:
                    info["status"] = "due_this_week"
                    due_this_week.append(info)
            else:
                info["status"] = "no_due_date"
                no_due_date.append(info)

        total_due_this_week += len(due_this_week)
        total_overdue += len(overdue)
        tasks_needing_attention.extend(overdue + due_this_week + no_due_date)

        # Track stalled / untouched
        is_stalled = p.status == "stalled"
        is_untouched = days_since is not None and days_since >= 7
        no_activity = last is None

        if is_stalled or is_untouched or no_activity:
            stalled_or_untouched.append({
                "title": p.title,
                "commitment": c_name,
                "status": p.status or "active",
                "days_since_activity": days_since,
                "no_activity_ever": no_activity,
            })

        # Commitment rollup
        if c_name:
            if c_name not in commitment_stats:
                commitment_stats[c_name] = {
                    "projects": 0,
                    "stalled": 0,
                    "untouched_7d": 0,
                    "total_upcoming": 0,
                    "due_this_week": 0,
                    "overdue": 0,
                }
            commitment_stats[c_name]["projects"] += 1
            if is_stalled:
                commitment_stats[c_name]["stalled"] += 1
            if is_untouched or no_activity:
                commitment_stats[c_name]["untouched_7d"] += 1
            commitment_stats[c_name]["total_upcoming"] += len(upcoming)
            commitment_stats[c_name]["due_this_week"] += len(due_this_week)
            commitment_stats[c_name]["overdue"] += len(overdue)

    # Build commitment list
    commitment_list = []
    for c in commitments:
        stats = commitment_stats.get(
            c.title,
            {
                "projects": 0,
                "stalled": 0,
                "untouched_7d": 0,
                "total_upcoming": 0,
                "due_this_week": 0,
                "overdue": 0,
            },
        )
        commitment_list.append({"title": c.title, **stats})

    # Sort tasks: overdue by date, then due this week by date, then no due date by priority
    overdue_sorted = sorted(
        tasks_needing_attention,
        key=lambda x: (
            0 if x["status"] == "overdue" else 1 if x["status"] == "due_this_week" else 2,
            x.get("due_date", "9999-99-99"),
            x.get("priority", 4),
        ),
    )

    # Recent activity (last 10)
    sorted_logs = sorted(log_entries, key=lambda x: x.effective_date or "", reverse=True)[:10]
    recent = []
    for e in sorted_logs:
        proj_name = project_titles.get(e.target_project, "") if e.target_project else ""
        recent.append({
            "log_id": e.log_id,
            "content": e.content[:120],
            "date": format_timestamp(e.effective_date),
            "project": proj_name,
        })

    # Count untouched (active projects with no activity in 7+ days)
    untouched_count = sum(
        1
        for s in stalled_or_untouched
        if s["status"] != "stalled"
        and (s["days_since_activity"] is not None or s["no_activity_ever"])
    )
    stalled_count = sum(1 for s in stalled_or_untouched if s["status"] == "stalled")

    ago_t = _last_sync_ago()
    sync_str = f"{int(ago_t)}s ago" if ago_t is not None else "never"

    return {
        "generated_at": today.isoformat(),
        "last_sync": sync_str,
        "summary": {
            "commitments": len(commitments),
            "projects": active_project_count,
            "stalled": stalled_count,
            "untouched_7d": untouched_count,
            "total_upcoming": total_upcoming,
            "due_this_week": total_due_this_week,
            "overdue": total_overdue,
        },
        "commitments": commitment_list,
        "stalled_or_untouched": stalled_or_untouched,
        "tasks_needing_attention": overdue_sorted,
        "recent_activity": recent,
    }


# ---------------------------------------------------------------------------
# Display layer — sync functions that print formatted text
# ---------------------------------------------------------------------------


def _display_commitments(data: list[dict]) -> None:
    if not data:
        print("No commitments found")
        return
    print(f"\n{'=' * 60}")
    print(f"ALL COMMITMENTS ({len(data)})")
    print(f"{'=' * 60}")
    for c in data:
        desc_str = f" - {c['description'][:40]}..." if c["description"] else ""
        print(f"  \u2022 {c['title']}{desc_str}")
    print()


def _display_projects(data: list[dict]) -> None:
    if not data:
        print("No projects found")
        return
    print(f"\n{'=' * 60}")
    print(f"ALL PROJECTS ({len(data)})")
    print(f"{'=' * 60}")
    for p in data:
        c_str = f" [{p['commitment']}]" if p["commitment"] else ""
        s_str = f" ({p['status']})" if p["status"] else ""
        print(f"  \u2022 {p['title']}{s_str}{c_str}")
        last = p.get("last_action")
        if last:
            print(f"      {last['type']}: {last['content']} ({last['date']})")
    print()


def _display_commitment(data: dict) -> None:
    title = data["title"]
    print(f"\n{'=' * 60}")
    print(f"COMMITMENT: {title}")
    print(f"{'=' * 60}")
    print(f"\nDescription: {data['description'] or 'None'}")

    projects = data["projects"]
    print(f"\n--- Projects ({len(projects)}) ---")
    for p in projects:
        s_str = f" ({p['status']})" if p["status"] else ""
        print(f"  \u2022 {p['title']}{s_str}")

    print("\n--- Recent Activity ---")
    activity = data["recent_activity"]
    if not activity:
        print("  No activity logged")
    else:
        for a in activity:
            date = a["date"] or "unknown"
            lid = a["log_id"]
            log_label = f"L#{lid}" if lid else "#?"
            print(f"  [{log_label}] [{date}] {a['content'][:60]}")

    print(f"\n--- Todoist Tasks ({title}) ---")
    _display_todoist_summary(data["todoist_tasks"])
    print()


def _display_project(data: dict) -> None:
    title = data["title"]
    print(f"\n{'=' * 60}")
    print(f"PROJECT: {title}")
    print(f"{'=' * 60}")
    print(f"\nDescription: {data['description'] or 'None'}")
    if data["status"]:
        print(f"Status: {data['status']}")
    if data["commitment"]:
        print(f"Commitment: {data['commitment']}")

    print("\n--- Recent Activity ---")
    activity = data["recent_activity"]
    if not activity:
        print("  No activity logged")
    else:
        for a in activity:
            date = a["date"] or "unknown"
            lid = a["log_id"]
            log_label = f"L#{lid}" if lid else "#?"
            print(f"  [{log_label}] [{date}] {a['content'][:60]}")

    print("\n--- Todoist Tasks ---")
    todoist = data["todoist_tasks"]
    has_any = len(todoist["upcoming"]) + len(todoist["completed"]) > 0
    if not has_any:
        print("  No tasks with this project's or commitment's label")
    else:
        _display_todoist_summary(todoist)
    print()


def _display_todoist_summary(tasks: dict) -> None:
    """Print todoist tasks from a structured dict with 'upcoming' and 'completed' keys."""
    upcoming_raw = tasks.get("upcoming", [])
    completed_raw = tasks.get("completed", [])
    if not upcoming_raw and not completed_raw:
        print("  No tasks with this label")
        return

    upcoming = sorted(upcoming_raw, key=lambda x: x.get("due_date") or "zzz")
    completed = completed_raw

    if upcoming:
        print(f"\n  Upcoming ({len(upcoming)}):")
        for t in upcoming:
            due = (
                f" (due: {t.get('due_string') or t.get('due_date')})"
                if t.get("due_string") or t.get("due_date")
                else ""
            )
            print(f"    \u25cb {t['content'][:55]}{due}")

    if completed:
        print(f"\n  Recently Completed (showing {min(3, len(completed))}):")
        for t in completed[:3]:
            print(f"    \u2713 {t['content'][:55]}")


def _display_logs(data: list[dict], header: str | None = None) -> None:
    if not data:
        print("No log entries found")
        return
    print(f"\n{'=' * 60}")
    print(header or f"RECENT LOGS ({len(data)})")
    print(f"{'=' * 60}")
    for e in data:
        lid = f"L#{e['log_id']}" if e["log_id"] else "#?"
        proj = f" [{e['project']}]" if e["project"] else ""
        print(f"  [{lid}] [{e['date']}]{proj} {str(e.get('content', ''))[:60]}")
    print()


def _display_log(data: dict) -> None:
    date = data["date"] or "unknown"
    print(f"\n{'=' * 60}")
    print(f"LOG ENTRY L#{data['log_id']}")
    print(f"{'=' * 60}")
    print(f"Date:    {date}")
    if data["project"]:
        print(f"Project: {data['project']}")
    print(f"\n{data['content']}")
    print()


def _display_overview(data: dict) -> None:
    """Print the daily overview dashboard."""
    summary = data["summary"]
    print(f"\n{'=' * 60}")
    print("          DASHBOARD")
    print(f"{'=' * 60}")

    # Summary line
    print("\nSummary")
    parts = [
        f"{summary['commitments']} commitments",
        f"{summary['projects']} projects",
    ]
    if summary["stalled"]:
        parts.append(f"{summary['stalled']} stalled")
    if summary["untouched_7d"]:
        parts.append(f"{summary['untouched_7d']} untouched (7d+)")
    print(f"  {' \u00b7 '.join(parts)}")

    parts2 = []
    if summary["total_upcoming"]:
        parts2.append(f"{summary['total_upcoming']} upcoming tasks")
    if summary["due_this_week"]:
        parts2.append(f"{summary['due_this_week']} due this week")
    if summary["overdue"]:
        parts2.append(f"{summary['overdue']} overdue")
    if parts2:
        print(f"  {' \u00b7 '.join(parts2)}")
    print(f"  Last sync: {data['last_sync']}")

    # Stalled & untouched
    stalled = [s for s in data["stalled_or_untouched"] if s["status"] == "stalled"]
    untouched = [s for s in data["stalled_or_untouched"] if s["status"] != "stalled"]
    if stalled or untouched:
        print("\n\u26a0 Stalled & Untouched")
        for s in stalled + untouched:
            status_tag = s["status"]
            if s["days_since_activity"] is not None:
                age = f"{s['days_since_activity']}d no activity"
            elif s["no_activity_ever"]:
                age = "no activity ever"
            else:
                age = ""
            c_name = f" [{s['commitment']}]" if s["commitment"] else ""
            print(f"  \u2022 {s['title']:<30} {status_tag:<10} {age}{c_name}")

    # Tasks needing attention
    tasks = data["tasks_needing_attention"]
    if tasks:
        print("\n\U0001f4cb Tasks Needing Attention")
        for status_label in ("overdue", "due_this_week", "no_due_date"):
            group = [t for t in tasks if t["status"] == status_label]
            if not group:
                continue
            label = status_label.replace("_", " ").upper()
            print(f"\n  {label} ({len(group)})")
            for t in group:
                due = f"  {t['due_date']}  " if "due_date" in t else "  " + " " * 13
                pri = f"P{t['priority']}"
                proj = f" [{t['project']}]" if t["project"] else ""
                print(f"    \u25cb {t['content'][:50]:<52} {due}{pri}{proj}")

    # Recent activity
    recent = data["recent_activity"]
    if recent:
        print("\n\U0001f4dd Recent Activity")
        for e in recent[:5]:
            lid = f"L#{e['log_id']}" if e["log_id"] else ""
            proj = f"  [{e['project']}]" if e["project"] else ""
            print(f"  {lid:<8} {e['date']}  {e['content'][:70]}{proj}")
    print()


# ---------------------------------------------------------------------------
# Helper: format and emit JSON
# ---------------------------------------------------------------------------


def _emit_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


# ============================================================
# COMMANDS
# ============================================================


# --- commitment ---


@commitment_app.command(name="list")
def list_commitments(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List all commitments."""

    async def _run() -> None:
        data = await _get_commitments_data()
        if json_output:
            _emit_json(data)
        else:
            _display_commitments(data)

    asyncio.run(_run())


@commitment_app.command(name="create")
def create_commitment(
    title: str = typer.Argument(help="Title of the commitment"),
    description: str = typer.Option("", "--description", "-d", help="Description"),
) -> None:
    """Create a new commitment."""

    async def _run() -> None:
        resp = await grist_post(
            "tables/Commitments/records",
            {"records": [{"fields": {"Title": title, "Description": description}}]},
        )
        if resp.status_code in (200, 201):
            print(f"\u2713 Commitment '{title}' created")
        else:
            print(f"\u2717 Failed to create commitment: {resp.status_code}")
            print(f"  {resp.text}")
            raise typer.Exit(1)

    asyncio.run(_run())


@commitment_app.command(name="update")
def update_commitment(
    name: str = typer.Argument(help="Current name of the commitment"),
    new_title: str | None = typer.Option(None, "--title", "-t", help="New title"),
    description: str | None = typer.Option(None, "--description", "-d", help="New description"),
) -> None:
    """Update a commitment's title and/or description."""

    async def _run() -> None:
        commitment = await get_commitment(name)
        if not commitment:
            print(f"Error: Commitment '{name}' not found")
            raise typer.Exit(1)

        fields: dict[str, Any] = {}
        if new_title is not None:
            fields["Title"] = new_title
        if description is not None:
            fields["Description"] = description

        if not fields:
            print("Error: Nothing to update (provide --title and/or --description)")
            raise typer.Exit(1)

        resp = await grist_patch("Commitments", [{"id": commitment.id, "fields": fields}])
        if resp.status_code in (200, 201):
            parts = []
            if new_title:
                parts.append(f"title -> '{new_title}'")
            if description is not None:
                parts.append("description updated")
            print(f"\u2713 Commitment '{name}' updated ({', '.join(parts)})")
        else:
            print(f"\u2717 Failed to update commitment: {resp.status_code}")
            print(f"  {resp.text}")
            raise typer.Exit(1)

    asyncio.run(_run())


# --- project ---


@project_app.command(name="list")
def list_projects(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List all projects with their commitment, status, and last action."""

    async def _run() -> None:
        data = await _get_projects_data()
        if json_output:
            _emit_json(data)
        else:
            _display_projects(data)

    asyncio.run(_run())


@project_app.command(name="create")
def create_project(
    title: str = typer.Argument(help="Title of the project"),
    commitment_name: str = typer.Option(..., "--commitment", "-c", help="Commitment name"),
    description: str = typer.Option("", "--description", "-d", help="Description"),
) -> None:
    """Create a new project under a commitment."""

    async def _run() -> None:
        commitment = await get_commitment(commitment_name)
        if not commitment:
            print(f"Error: Commitment '{commitment_name}' not found")
            raise typer.Exit(1)

        fields: dict[str, Any] = {
            "Title": title,
            "Description": description,
            "Commitment": commitment.id,
        }
        resp = await grist_post(
            "tables/Project/records",
            {"records": [{"fields": fields}]},
        )
        if resp.status_code in (200, 201):
            print(f"\u2713 Project '{title}' created under commitment '{commitment_name}'")
        else:
            print(f"\u2717 Failed to create project: {resp.status_code}")
            print(f"  {resp.text}")
            raise typer.Exit(1)

    asyncio.run(_run())


@project_app.command(name="update")
def update_project(
    name: str = typer.Argument(help="Current name of the project"),
    new_title: str | None = typer.Option(None, "--title", "-t", help="New title"),
    description: str | None = typer.Option(None, "--description", "-d", help="New description"),
    commitment_name: str | None = typer.Option(
        None, "--commitment", "-c", help="Move to a different commitment"
    ),
) -> None:
    """Update a project's title, description, and/or parent commitment."""

    async def _run() -> None:
        project = await get_project(name)
        if not project:
            print(f"Error: Project '{name}' not found")
            raise typer.Exit(1)

        fields: dict[str, Any] = {}
        if new_title is not None:
            fields["Title"] = new_title
        if description is not None:
            fields["Description"] = description
        if commitment_name is not None:
            commitment = await get_commitment(commitment_name)
            if not commitment:
                print(f"Error: Commitment '{commitment_name}' not found")
                raise typer.Exit(1)
            fields["Commitment"] = commitment.id

        if not fields:
            print("Error: Nothing to update (provide --title, --description, and/or --commitment)")
            raise typer.Exit(1)

        resp = await grist_patch("Project", [{"id": project.id, "fields": fields}])
        if resp.status_code in (200, 201):
            parts = []
            if new_title:
                parts.append(f"title -> '{new_title}'")
            if description is not None:
                parts.append("description updated")
            if commitment_name is not None:
                parts.append(f"commitment -> '{commitment_name}'")
            print(f"\u2713 Project '{name}' updated ({', '.join(parts)})")
        else:
            print(f"\u2717 Failed to update project: {resp.status_code}")
            print(f"  {resp.text}")
            raise typer.Exit(1)

    asyncio.run(_run())


@project_app.command()
def status(
    project_name: str = typer.Argument(help="Name of the project"),
    new_status: ProjectStatus = typer.Option(..., "--status", "-s", help="New status"),
) -> None:
    """Set the status of a project."""

    async def _run() -> None:
        project = await get_project(project_name)
        if not project:
            print(f"Error: Project '{project_name}' not found")
            raise typer.Exit(1)

        resp = await grist_patch(
            "Project", [{"id": project.id, "fields": {"Status": new_status.value}}]
        )

        if resp.status_code in (200, 201):
            print(f"\u2713 {project_name} \u2192 {new_status.value}")
        else:
            print(f"\u2717 Failed to update status: {resp.status_code}")
            print(f"  {resp.text}")
            raise typer.Exit(1)

    asyncio.run(_run())


# --- query ---


@app.command()
def query(
    name: str = typer.Argument(help="Commitment or project name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Query a commitment or project by name."""

    async def _run() -> None:
        data = await _get_commitment_data(name)
        if data is not None:
            if json_output:
                _emit_json(data)
            else:
                _display_commitment(data)
            return

        data = await _get_project_data(name)
        if data is not None:
            if json_output:
                _emit_json(data)
            else:
                _display_project(data)
            return

        if json_output:
            _emit_json({"error": f"'{name}' not found as a commitment or project"})
        else:
            print(f"'{name}' not found as a commitment or project")

    asyncio.run(_run())


@app.command()
def overview(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Daily overview dashboard — stalled projects, tasks needing attention, recent activity."""

    async def _run() -> None:
        data = await _get_overview_data()
        if json_output:
            _emit_json(data)
        else:
            _display_overview(data)

    asyncio.run(_run())


# --- log ---


@log_app.command()
def add(
    content: str = typer.Argument(help="Log message content"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project name"),
    commitment: str | None = typer.Option(None, "--commitment", "-c", help="Commitment name"),
    date: str | None = typer.Option(None, "--date", "-d", help="Activity date"),
) -> None:
    """Add a log entry."""

    async def _run() -> None:
        if not project and not commitment:
            print("Error: You must provide --project or --commitment")
            raise typer.Exit(1)
        if project and commitment:
            print("Error: Provide --project OR --commitment, not both")
            raise typer.Exit(1)

        project_id: int | None = None
        if project:
            p = await get_project(project)
            if not p:
                print(f"Error: Project '{project}' not found")
                raise typer.Exit(1)
            project_id = p.id
            print(f"Logging to project: {project}")

        commitment_id: int | None = None
        if commitment:
            c = await get_commitment(commitment)
            if not c:
                print(f"Error: Commitment '{commitment}' not found")
                raise typer.Exit(1)
            commitment_id = c.id
            print(f"Logging to commitment: {commitment}")

        if date:
            print(f"Date: {date}")
        print(f"Content: {content}")
        await add_log_entry(
            content=content, project_id=project_id, commitment_id=commitment_id, activity_date=date
        )

    asyncio.run(_run())


@log_app.command(name="list")
def list_entries(
    limit: int = typer.Option(30, "--limit", "-l", help="Number of entries"),
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project name"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List recent log entries."""

    async def _run() -> None:
        data = await _get_logs_data(limit=limit, project_filter=project)
        if json_output:
            _emit_json(data)
        else:
            _display_logs(data)

    asyncio.run(_run())


@log_app.command()
def search(
    query_str: str = typer.Argument(help="Search term"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number of entries"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Search log entries by content."""

    async def _run() -> None:
        data = await _get_search_logs_data(query_str, limit=limit)
        if json_output:
            _emit_json(data)
        else:
            _display_logs(data, header=f"LOGS MATCHING '{query_str}' ({len(data)})")

    asyncio.run(_run())


@log_app.command()
def view(
    log_id: int = typer.Argument(help="Log entry ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """View a single log entry with full content."""

    async def _run() -> None:
        data = await _get_log_data(log_id)
        if data is None:
            if json_output:
                _emit_json({"error": f"Log entry L#{log_id} not found"})
            else:
                print(f"Log entry L#{log_id} not found")
            return

        if json_output:
            _emit_json(data)
        else:
            _display_log(data)

    asyncio.run(_run())


@log_app.command()
def update(
    log_id: int = typer.Argument(help="Log entry ID"),
    content: str | None = typer.Option(None, "--content", "-c", help="New content"),
    date: str | None = typer.Option(None, "--date", "-d", help="New date"),
) -> None:
    """Update a log entry."""

    async def _run() -> None:
        await update_log_entry(log_id, content=content, activity_date=date)

    asyncio.run(_run())


@log_app.command()
def delete(
    log_id: int = typer.Argument(help="Log entry ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a log entry."""

    async def _run() -> None:
        if not yes:
            for e in rows_from_data(await grist_get("LogEntries"), LogEntry):
                if e.log_id == log_id:
                    date_str = format_timestamp(e.effective_date)
                    print(f"Log entry L#{log_id}: [{date_str}] {e.content[:80]}")
                    break
            print("Use --yes to confirm deletion")
            raise typer.Exit(1)

        await delete_log_entry(log_id)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
