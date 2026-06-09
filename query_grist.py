#!/usr/bin/env python3
"""
Query Grist for Projects, Commitments, and Todoist tasks
"""

from datetime import UTC
from enum import Enum
from typing import Any

import httpx
import typer
from pydantic import BaseModel, Field, field_validator

from settings import settings

app = typer.Typer(no_args_is_help=True)
log_app = typer.Typer(no_args_is_help=True)
app.add_typer(log_app, name="log")

GRIST_BASE_URL = f"https://docs.getgrist.com/api/docs/{settings.grist_doc_id}"


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
    created_at: Any | None = Field(None, alias="CreatedAt")
    target_project: int | None = Field(None, alias="Target_Project")
    target_commitment: int | None = Field(None, alias="Target_Commitment")
    target_task: int | None = Field(None, alias="Target_Task")


def _rows(data: dict[str, Any] | None, model: type[BaseModel]) -> list[Any]:
    """Convert Grist columnar data to a list of model instances."""
    if not data:
        return []
    ids: list[int] = data.get("id", [])
    results = []
    for i in range(len(ids)):
        row: dict[str, Any] = {}
        for field_name, field_info in model.model_fields.items():
            alias = field_info.alias or field_name
            col = data.get(alias, [])
            val = col[i] if i < len(col) else None
            if val is not None:
                row[alias] = val
        results.append(model.model_validate(row))
    return results


def grist_get(table: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Make a GET request to Grist API."""
    with httpx.Client(timeout=10.0) as client:
        url = f"{GRIST_BASE_URL}/tables/{table}/data"
        resp = client.get(
            url, headers={"Authorization": f"Bearer {settings.grist_api_key}"}, params=params
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"Error fetching {table}: {resp.status_code} - {resp.text}")
            return None


def grist_patch(table: str, records: list[dict[str, Any]]) -> httpx.Response:
    """Update records in Grist via PATCH."""
    with httpx.Client(timeout=10.0) as client:
        return client.patch(
            f"{GRIST_BASE_URL}/tables/{table}/records",
            headers={"Authorization": f"Bearer {settings.grist_api_key}"},
            json={"records": records},
        )


def _build_todoist_index(items: list[TodoistItem]) -> dict[str, list[TodoistItem]]:
    """Build a dict mapping label → list of tasks from Todoist data."""
    label_index: dict[str, list[TodoistItem]] = {}
    for item in items:
        if len(item.labels) > 1:
            for label in item.labels[1:]:
                label_index.setdefault(label, []).append(item)
    return label_index


def _build_log_index(entries: list[LogEntry]) -> dict[int, list[LogEntry]]:
    """Build a dict mapping target_project_id → list of log entries."""
    log_index: dict[int, list[LogEntry]] = {}
    for entry in entries:
        if entry.target_project:
            log_index.setdefault(entry.target_project, []).append(entry)
    return log_index


def _log_id_to_grist_id(log_id: int) -> int | None:
    """Look up a Grist internal ID from a stable LogId."""
    entries = _rows(grist_get("LogEntries"), LogEntry)
    for e in entries:
        if e.log_id == log_id:
            return e.id
    return None


def grist_query_by_label(label_name: str) -> list[TodoistItem]:
    """Get all Todoist tasks with a specific label."""
    items = _rows(grist_get("Todoist"), TodoistItem)
    index = _build_todoist_index(items)
    return index.get(label_name, [])


def get_commitment(title: str) -> Commitment | None:
    """Get a commitment by title."""
    for c in _rows(grist_get("Commitments"), Commitment):
        if c.title == title:
            return c
    return None


def get_project(title: str) -> GristProject | None:
    """Get a project by title."""
    for p in _rows(grist_get("Project"), GristProject):
        if p.title == title:
            return p
    return None


def get_log_entries(
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

    entries = _rows(grist_get("LogEntries"), LogEntry)

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
        from datetime import datetime

        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return str(ts)[:10]


def query_commitment(name: str) -> None:
    """Query a commitment by name and show its projects, activity, and Todoist tasks."""
    commitment = get_commitment(name)
    if not commitment:
        print(f"Commitment '{name}' not found")
        return

    print(f"\n{'=' * 60}")
    print(f"COMMITMENT: {name}")
    print(f"{'=' * 60}")
    print(f"\nDescription: {commitment.description or 'None'}")

    projects = _rows(grist_get("Project"), GristProject)
    matching = [p for p in projects if p.commitment == commitment.id]

    print(f"\n--- Projects ({len(matching)}) ---")
    for p in matching:
        s_str = f" ({p.status})" if p.status else ""
        print(f"  \u2022 {p.title}{s_str}")

    print("\n--- Recent Activity ---")
    activity = get_log_entries(target_commitment_id=commitment.id, limit=10)
    if not activity:
        print("  No activity logged")
    else:
        for a in activity:
            date = format_timestamp(a.effective_date) or "unknown"
            log_label = f"L#{a.log_id}" if a.log_id else f"#{a.id}"
            print(f"  [{log_label}] [{date}] {a.content[:60]}")

    print(f"\n--- Todoist Tasks ({name}) ---")
    tasks = grist_query_by_label(name)
    if not tasks:
        print("  No tasks with this label")
    else:
        upcoming = [t for t in tasks if not t.checked]
        completed = [t for t in tasks if t.checked]
        upcoming.sort(key=lambda x: x.due_date or "zzz")

        if upcoming:
            print(f"\n  Upcoming ({len(upcoming)}):")
            for t in upcoming:
                due = f" (due: {t.due_string or t.due_date})" if t.due_string or t.due_date else ""
                print(f"    \u25cb {t.content[:55]}{due}")

        if completed:
            print(f"\n  Recently Completed (showing {min(3, len(completed))}):")
            for t in completed[:3]:
                print(f"    \u2713 {t.content[:55]}")

    print()


def query_project(name: str) -> None:
    """Query a project by name and show its info, activity, and Todoist tasks."""
    project = get_project(name)
    if not project:
        print(f"Project '{name}' not found")
        return

    print(f"\n{'=' * 60}")
    print(f"PROJECT: {name}")
    print(f"{'=' * 60}")
    print(f"\nDescription: {project.description or 'None'}")
    if project.status:
        print(f"Status: {project.status}")

    if project.commitment:
        commitments = _rows(grist_get("Commitments"), Commitment)
        for c in commitments:
            if c.id == project.commitment:
                print(f"Commitment: {c.title}")
                break

    print("\n--- Recent Activity ---")
    activity = get_log_entries(target_project_id=project.id, limit=10)
    if not activity:
        print("  No activity logged")
    else:
        for a in activity:
            date = format_timestamp(a.effective_date) or "unknown"
            log_label = f"L#{a.log_id}" if a.log_id else f"#{a.id}"
            print(f"  [{log_label}] [{date}] {a.content[:60]}")

    print("\n--- Todoist Tasks ---")
    project_tasks = grist_query_by_label(name)
    commitment_tasks: list[TodoistItem] = []
    if project.commitment:
        commitments = _rows(grist_get("Commitments"), Commitment)
        for c in commitments:
            if c.id == project.commitment:
                commitment_tasks = grist_query_by_label(c.title)
                break

    seen: dict[int, TodoistItem] = {}
    for t in project_tasks + commitment_tasks:
        seen[t.id] = t

    if not seen:
        print("  No tasks with this project's or commitment's label")
    else:
        upcoming = [t for t in seen.values() if not t.checked]
        completed = [t for t in seen.values() if t.checked]
        upcoming.sort(key=lambda x: x.due_date or "zzz")

        if upcoming:
            print(f"\n  Upcoming ({len(upcoming)}):")
            for t in upcoming:
                due = f" (due: {t.due_string or t.due_date})" if t.due_string or t.due_date else ""
                print(f"    \u25cb {t.content[:55]}{due}")

        if completed:
            print(f"\n  Recently Completed (showing {min(3, len(completed))}):")
            for t in completed[:3]:
                print(f"    \u2713 {t.content[:55]}")

    print()


@app.command()
def commitments() -> None:
    """List all commitments."""
    items = _rows(grist_get("Commitments"), Commitment)
    if not items:
        print("No commitments found")
        return

    print(f"\n{'=' * 60}")
    print(f"ALL COMMITMENTS ({len(items)})")
    print(f"{'=' * 60}")
    for c in items:
        desc_str = f" - {c.description[:40]}..." if c.description else ""
        print(f"  \u2022 {c.title}{desc_str}")
    print()


def get_last_action_for_project(
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

    activity = get_log_entries(target_project_id=project_id, limit=1, log_index=log_index)
    if activity:
        log_date = format_timestamp(activity[0].effective_date)
        if log_date and (best_date is None or log_date > best_date):
            best = {"type": "Activity", "content": activity[0].content[:50], "date": log_date}

    return best


@app.command()
def projects() -> None:
    """List all projects with their commitment, status, and last action."""
    project_list = _rows(grist_get("Project"), GristProject)
    if not project_list:
        print("No projects found")
        return

    todoist_index = _build_todoist_index(_rows(grist_get("Todoist"), TodoistItem))
    log_index = _build_log_index(_rows(grist_get("LogEntries"), LogEntry))
    commitments = {c.id: c.title for c in _rows(grist_get("Commitments"), Commitment)}

    print(f"\n{'=' * 60}")
    print(f"ALL PROJECTS ({len(project_list)})")
    print(f"{'=' * 60}")
    for p in project_list:
        c_name = commitments.get(p.commitment) if p.commitment else None
        c_str = f" [{c_name}]" if c_name else ""
        s_str = f" ({p.status})" if p.status else ""
        print(f"  \u2022 {p.title}{s_str}{c_str}")

        last = get_last_action_for_project(p.id, p.title, todoist_index, log_index)
        if last:
            print(f"      {last['type']}: {last['content']} ({last['date']})")
    print()


def _get_next_log_id() -> int:
    """Find the next available LogId by taking max existing + 1."""
    entries = _rows(grist_get("LogEntries"), LogEntry)
    valid = [e.log_id for e in entries if e.log_id is not None]
    return max(valid) + 1 if valid else 1


def add_log_entry(
    content: str,
    project_id: int | None = None,
    commitment_id: int | None = None,
    activity_date: str | None = None,
) -> bool:
    """Create a log entry in Grist's LogEntries table."""
    log_id = _get_next_log_id()
    fields: dict[str, Any] = {"Content": content, "LogId": log_id}

    if project_id:
        fields["Target_Project"] = project_id
    if activity_date:
        fields["ActivityDate"] = activity_date
    if commitment_id:
        fields["Target_Commitment"] = commitment_id

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{GRIST_BASE_URL}/tables/LogEntries/records",
            headers={"Authorization": f"Bearer {settings.grist_api_key}"},
            json={"records": [{"fields": fields}]},
        )

        if resp.status_code in (200, 201):
            print(f"\u2713 Log entry created (L#{log_id})")
            return True
        else:
            print(f"\u2717 Failed to create log entry: {resp.status_code}")
            print(f"  {resp.text}")
            return False


def update_log_entry(
    log_id: int, content: str | None = None, activity_date: str | None = None
) -> bool:
    """Update a log entry's content and/or date using its stable LogId."""
    grist_id = _log_id_to_grist_id(log_id)
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

    resp = grist_patch("LogEntries", [{"id": grist_id, "fields": fields}])

    if resp.status_code in (200, 201):
        print(f"\u2713 Log entry L#{log_id} updated")
        return True
    else:
        print(f"\u2717 Failed to update log entry: {resp.status_code}")
        print(f"  {resp.text}")
        return False


def delete_log_entry(log_id: int) -> bool:
    """Delete a log entry from Grist using its stable LogId."""
    grist_id = _log_id_to_grist_id(log_id)
    if grist_id is None:
        print(f"Error: No log entry found with LogId {log_id}")
        return False

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{GRIST_BASE_URL}/apply",
            headers={"Authorization": f"Bearer {settings.grist_api_key}"},
            json=[["BulkRemoveRecord", "LogEntries", [grist_id]]],
        )

        if resp.status_code in (200, 201):
            print(f"\u2713 Log entry L#{log_id} deleted")
            return True
        else:
            print(f"\u2717 Failed to delete log entry: {resp.status_code}")
            print(f"  {resp.text}")
            return False


def _build_project_titles() -> dict[int, str]:
    """Build a dict mapping project id → project title."""
    return {p.id: p.title for p in _rows(grist_get("Project"), GristProject)}


def list_logs(limit: int = 30, project_filter: str | None = None) -> None:
    """List all log entries with project names."""
    entries = _rows(grist_get("LogEntries"), LogEntry)
    if not entries:
        print("No log entries found")
        return

    project_titles = _build_project_titles()

    display = []
    for e in entries:
        proj_name = project_titles.get(e.target_project, "") if e.target_project else ""
        if project_filter and project_filter.lower() not in proj_name.lower():
            continue
        display.append({
            "log_id": e.log_id,
            "content": e.content,
            "date": format_timestamp(e.effective_date),
            "project": proj_name,
        })

    display.sort(key=lambda x: x["date"], reverse=True)
    display = display[:limit]

    if not display:
        print("No log entries found")
        return

    print(f"\n{'=' * 60}")
    print(f"RECENT LOGS ({len(display)})")
    print(f"{'=' * 60}")
    for e in display:
        lid = f"L#{e['log_id']}" if e["log_id"] else "#?"
        proj = f" [{e['project']}]" if e["project"] else ""
        print(f"  [{lid}] [{e['date']}]{proj} {(e.get('content') or '')[:60]}")
    print()


def search_logs(query: str, limit: int = 20) -> None:
    """Search log entries by content (case-insensitive)."""
    entries = _rows(grist_get("LogEntries"), LogEntry)
    if not entries:
        print("No log entries found")
        return

    project_titles = _build_project_titles()
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
    matches = matches[:limit]

    if not matches:
        print(f"No log entries matching '{query}'")
        return

    print(f"\n{'=' * 60}")
    print(f"LOGS MATCHING '{query}' ({len(matches)})")
    print(f"{'=' * 60}")
    for e in matches:
        lid = f"L#{e['log_id']}" if e["log_id"] else "#?"
        proj = f" [{e['project']}]" if e["project"] else ""
        print(f"  [{lid}] [{e['date']}]{proj} {(e.get('content') or '')[:60]}")
    print()


@app.command()
def status(
    project_name: str = typer.Argument(help="Name of the project"),
    new_status: ProjectStatus = typer.Argument(help="New status"),
) -> None:
    """Set the status of a project."""
    project = get_project(project_name)
    if not project:
        print(f"Error: Project '{project_name}' not found")
        raise typer.Exit(1)

    resp = grist_patch("Project", [{"id": project.id, "fields": {"Status": new_status.value}}])

    if resp.status_code in (200, 201):
        print(f"\u2713 {project_name} \u2192 {new_status.value}")
    else:
        print(f"\u2717 Failed to update status: {resp.status_code}")
        print(f"  {resp.text}")
        raise typer.Exit(1)


@app.command()
def query(name: str = typer.Argument(help="Commitment or project name")) -> None:
    """Query a commitment or project by name."""
    commitment = get_commitment(name)
    if commitment:
        query_commitment(name)
        return

    project = get_project(name)
    if project:
        query_project(name)
        return

    print(f"'{name}' not found as a commitment or project")


@log_app.command()
def add(
    content: str = typer.Argument(help="Log message content"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project name"),
    commitment: str | None = typer.Option(None, "--commitment", "-c", help="Commitment name"),
    date: str | None = typer.Option(None, "--date", "-d", help="Activity date"),
) -> None:
    """Add a log entry."""
    if not project and not commitment:
        print("Error: You must provide --project or --commitment")
        raise typer.Exit(1)
    if project and commitment:
        print("Error: Provide --project OR --commitment, not both")
        raise typer.Exit(1)

    project_id: int | None = None
    if project:
        p = get_project(project)
        if not p:
            print(f"Error: Project '{project}' not found")
            raise typer.Exit(1)
        project_id = p.id
        print(f"Logging to project: {project}")

    commitment_id: int | None = None
    if commitment:
        c = get_commitment(commitment)
        if not c:
            print(f"Error: Commitment '{commitment}' not found")
            raise typer.Exit(1)
        commitment_id = c.id
        print(f"Logging to commitment: {commitment}")

    if date:
        print(f"Date: {date}")
    print(f"Content: {content}")
    add_log_entry(
        content=content, project_id=project_id, commitment_id=commitment_id, activity_date=date
    )


@log_app.command(name="list")
def list_entries(
    limit: int = typer.Option(30, "--limit", "-l", help="Number of entries"),
    project: str | None = typer.Option(None, "--project", "-p", help="Filter by project name"),
) -> None:
    """List recent log entries."""
    list_logs(limit=limit, project_filter=project)


@log_app.command()
def view(
    query_str: str = typer.Argument(help="Search term"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number of entries"),
) -> None:
    """Search log entries by content."""
    search_logs(query_str, limit=limit)


@log_app.command()
def update(
    log_id: int = typer.Argument(help="Log entry ID"),
    content: str | None = typer.Option(None, "--content", "-c", help="New content"),
    date: str | None = typer.Option(None, "--date", "-d", help="New date"),
) -> None:
    """Update a log entry."""
    update_log_entry(log_id, content=content, activity_date=date)


@log_app.command()
def delete(
    log_id: int = typer.Argument(help="Log entry ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a log entry."""
    if not yes:
        entries = _rows(grist_get("LogEntries"), LogEntry)
        for e in entries:
            if e.log_id == log_id:
                date_str = format_timestamp(e.effective_date)
                print(f"Log entry L#{log_id}: [{date_str}] {e.content[:80]}")
                break
        print("Use --yes to confirm deletion")
        raise typer.Exit(1)

    delete_log_entry(log_id)


if __name__ == "__main__":
    app()
