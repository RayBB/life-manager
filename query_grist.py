#!/usr/bin/env python3
"""
Query Grist for Projects, Commitments, and Todoist tasks
"""

from datetime import UTC
from enum import Enum
from typing import Any

import httpx
import typer

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


def grist_patch(table, records):
    """Update records in Grist via PATCH."""
    with httpx.Client(timeout=10.0) as client:
        resp = client.patch(
            f"{GRIST_BASE_URL}/tables/{table}/records",
            headers={"Authorization": f"Bearer {settings.grist_api_key}"},
            json={"records": records},
        )
        return resp


def _build_todoist_index(todoist_data):
    """Build a dict mapping label → list of tasks from pre-fetched Todoist data."""
    if not todoist_data:
        return {}

    label_index = {}
    content = todoist_data.get("Content", [])
    labels = todoist_data.get("Labels", [])
    ids = todoist_data.get("id", [])

    for i in range(len(content)):
        l = labels[i] if i < len(labels) else None
        if l and isinstance(l, list) and len(l) > 1:
            task = {
                "grist_id": ids[i] if i < len(ids) else None,
                "content": content[i],
                "labels": l,
                "checked": todoist_data.get("Checked", [])[i]
                if i < len(todoist_data.get("Checked", []))
                else False,
                "due_date": todoist_data.get("DueDate", [])[i]
                if i < len(todoist_data.get("DueDate", []))
                else None,
                "due_string": todoist_data.get("DueString", [])[i]
                if i < len(todoist_data.get("DueString", []))
                else None,
            }
            for label in l[1:]:
                label_index.setdefault(label, []).append(task)

    return label_index


def _log_id_to_grist_id(log_id):
    """Look up a Grist internal ID from a stable LogId.
    Reads LogEntries and finds the entry with matching LogId.
    """
    data = grist_get("LogEntries")
    if not data:
        return None

    log_ids = data.get("LogId", [])
    grist_ids = data.get("id", [])
    for i in range(len(grist_ids)):
        lid = log_ids[i] if i < len(log_ids) else None
        if lid == log_id:
            return grist_ids[i]
    return None


def _build_log_index(log_data):
    """Build a dict mapping target_project_id → list of log entries from pre-fetched data."""
    if not log_data:
        return {}

    log_index = {}
    target_projects = log_data.get("Target_Project", [])

    for i in range(len(log_data.get("Content", []))):
        tp = target_projects[i] if i < len(target_projects) else None
        log_ids = log_data.get("LogId", [])
        entry = {
            "id": log_data["id"][i],
            "log_id": log_ids[i] if i < len(log_ids) else None,
            "content": log_data["Content"][i],
            "effective_date": log_data.get("EffectiveDate", [])[i]
            if i < len(log_data.get("EffectiveDate", []))
            else None,
        }
        if tp:
            log_index.setdefault(tp, []).append(entry)

    return log_index


def grist_query_by_label(label_name):
    """Get all Todoist tasks with a specific label. Returns list of dicts."""
    data = grist_get("Todoist")
    if not data:
        return []

    index = _build_todoist_index(data)
    return index.get(label_name, [])


def get_commitment(title):
    """Get a commitment by title. Returns dict with id, title, project_ids."""
    data = grist_get("Commitments")
    if not data:
        return None

    titles = data.get("Title", [])
    for i in range(len(titles)):
        if titles[i] == title:
            return {
                "id": data["id"][i],
                "title": title,
                "description": data.get("Description", [])[i]
                if i < len(data.get("Description", []))
                else None,
                "project_ids": data.get("Project", [])[i]
                if i < len(data.get("Project", []))
                else None,
            }
    return None


def get_project(title):
    """Get a project by title. Returns dict with id, title, commitment_id, status."""
    data = grist_get("Project")
    if not data:
        return None

    titles = data.get("Title", [])
    for i in range(len(titles)):
        if titles[i] == title:
            return {
                "id": data["id"][i],
                "title": title,
                "description": data.get("Description", [])[i]
                if i < len(data.get("Description", []))
                else None,
                "commitment_id": data.get("Commitment", [])[i]
                if i < len(data.get("Commitment", []))
                else None,
                "status": data.get("Status", [])[i] if i < len(data.get("Status", [])) else None,
            }
    return None


def get_log_entries(
    target_project_id=None,
    target_commitment_id=None,
    target_task_id=None,
    limit=20,
    _log_index=None,
):
    """Get log entries, optionally filtered by target.

    If _log_index is provided (from _build_log_index), uses pre-fetched data instead of making a new API call.
    """
    if _log_index is not None and target_project_id:
        entries = _log_index.get(target_project_id, [])
        entries.sort(key=lambda x: x.get("effective_date") or "", reverse=True)
        return entries[:limit]

    data = grist_get("LogEntries")
    if not data:
        return []

    results = []
    target_projects = data.get("Target_Project", [])
    target_commitments = data.get("Target_Commitment", [])
    target_tasks = data.get("Target_Task", [])

    for i in range(len(data.get("Content", []))):
        tp = target_projects[i] if i < len(target_projects) else None
        tc = target_commitments[i] if i < len(target_commitments) else None
        tt = target_tasks[i] if i < len(target_tasks) else None

        if target_project_id and tp != target_project_id:
            continue
        if target_commitment_id and tc != target_commitment_id:
            continue
        if target_task_id and tt != target_task_id:
            continue

        log_ids = data.get("LogId", [])
        results.append({
            "id": data["id"][i],
            "log_id": log_ids[i] if i < len(log_ids) else None,
            "content": data["Content"][i],
            "created_at": data.get("CreatedAt", [])[i]
            if i < len(data.get("CreatedAt", []))
            else None,
            "effective_date": data.get("EffectiveDate", [])[i]
            if i < len(data.get("EffectiveDate", []))
            else None,
            "target_project": tp,
            "target_commitment": tc,
        })

    results.sort(key=lambda x: x.get("effective_date") or "", reverse=True)
    return results[:limit]


def format_timestamp(ts):
    """Format a Unix timestamp (int) or ISO string to YYYY-MM-DD."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        from datetime import datetime

        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return str(ts)[:10]


def query_commitment(name):
    """Query a commitment by name and show its projects, activity, and Todoist tasks."""
    print(f"\n{'=' * 60}")
    print(f"COMMITMENT: {name}")
    print(f"{'=' * 60}")

    commitment = get_commitment(name)
    if not commitment:
        print(f"Commitment '{name}' not found")
        return

    print(f"\nDescription: {commitment['description'] or 'None'}")

    # Get projects that belong to this commitment
    projects_data = grist_get("Project")
    matching_projects = []
    if projects_data:
        titles = projects_data.get("Title", [])
        commitments_col = projects_data.get("Commitment", [])
        statuses = projects_data.get("Status", [])
        for i in range(len(titles)):
            c = commitments_col[i] if i < len(commitments_col) else None
            if c == commitment["id"]:
                status = statuses[i] if i < len(statuses) else None
                matching_projects.append({
                    "id": projects_data["id"][i],
                    "title": titles[i],
                    "description": projects_data.get("Description", [])[i]
                    if i < len(projects_data.get("Description", []))
                    else None,
                    "status": status,
                })

    print(f"\n--- Projects ({len(matching_projects)}) ---")
    for p in matching_projects:
        s_str = f" ({p['status']})" if p.get("status") else ""
        print(f"  • {p['title']}{s_str}")

    # Get recent activity for this commitment
    print("\n--- Recent Activity ---")
    activity = get_log_entries(target_commitment_id=commitment["id"], limit=10)
    if not activity:
        print("  No activity logged")
    else:
        for a in activity:
            date = format_timestamp(a.get("effective_date")) or "unknown"
            log_label = f"L#{a['log_id']}" if a.get("log_id") else f"#{a['id']}"
            print(f"  [{log_label}] [{date}] {a['content'][:60]}")

    # Get Todoist tasks with this commitment's label
    print(f"\n--- Todoist Tasks ({name}) ---")
    tasks = grist_query_by_label(name)
    if not tasks:
        print("  No tasks with this label")
    else:
        upcoming = [t for t in tasks if not t["checked"]]
        completed = [t for t in tasks if t["checked"]]

        upcoming.sort(key=lambda x: x.get("due_date") or "zzz")

        if upcoming:
            print(f"\n  Upcoming ({len(upcoming)}):")
            for t in upcoming:
                due = (
                    f" (due: {t['due_string'] or t['due_date']})"
                    if t["due_string"] or t["due_date"]
                    else ""
                )
                print(f"    ○ {t['content'][:55]}{due}")

        if completed:
            print(f"\n  Recently Completed (showing {min(3, len(completed))}):")
            for t in completed[:3]:
                print(f"    ✓ {t['content'][:55]}")

    print()


def query_project(name):
    """Query a project by name and show its info, activity, and Todoist tasks."""
    print(f"\n{'=' * 60}")
    print(f"PROJECT: {name}")
    print(f"{'=' * 60}")

    project = get_project(name)
    if not project:
        print(f"Project '{name}' not found")
        return

    print(f"\nDescription: {project['description'] or 'None'}")
    if project.get("status"):
        print(f"Status: {project['status']}")

    # Get commitment info
    if project["commitment_id"]:
        commitment_data = grist_get("Commitments")
        if commitment_data:
            titles = commitment_data.get("Title", [])
            for i in range(len(titles)):
                if commitment_data["id"][i] == project["commitment_id"]:
                    print(f"Commitment: {titles[i]}")
                    break

    # Get recent activity for this project
    print("\n--- Recent Activity ---")
    activity = get_log_entries(target_project_id=project["id"], limit=10)
    if not activity:
        print("  No activity logged")
    else:
        for a in activity:
            date = format_timestamp(a.get("effective_date")) or "unknown"
            log_label = f"L#{a['log_id']}" if a.get("log_id") else f"#{a['id']}"
            print(f"  [{log_label}] [{date}] {a['content'][:60]}")

    # Get Todoist tasks
    print("\n--- Todoist Tasks ---")

    project_tasks = grist_query_by_label(name)
    commitment_tasks = []
    if project["commitment_id"]:
        commitment_data = grist_get("Commitments")
        if commitment_data:
            titles = commitment_data.get("Title", [])
            for i in range(len(titles)):
                if commitment_data["id"][i] == project["commitment_id"]:
                    commitment_tasks = grist_query_by_label(titles[i])
                    break

    all_tasks = {}
    for t in project_tasks + commitment_tasks:
        all_tasks[t["grist_id"]] = t

    if not all_tasks:
        print("  No tasks with this project's or commitment's label")
    else:
        upcoming = [t for t in all_tasks.values() if not t["checked"]]
        completed = [t for t in all_tasks.values() if t["checked"]]

        upcoming.sort(key=lambda x: x.get("due_date") or "zzz")

        if upcoming:
            print(f"\n  Upcoming ({len(upcoming)}):")
            for t in upcoming:
                due = (
                    f" (due: {t['due_string'] or t['due_date']})"
                    if t["due_string"] or t["due_date"]
                    else ""
                )
                print(f"    ○ {t['content'][:55]}{due}")

        if completed:
            print(f"\n  Recently Completed (showing {min(3, len(completed))}):")
            for t in completed[:3]:
                print(f"    ✓ {t['content'][:55]}")

    print()


@app.command()
def commitments():
    """List all commitments."""
    data = grist_get("Commitments")
    if not data:
        print("No commitments found")
        return

    titles = data.get("Title", [])
    descriptions = data.get("Description", [])

    print(f"\n{'=' * 60}")
    print(f"ALL COMMITMENTS ({len(titles)})")
    print(f"{'=' * 60}")
    for i in range(len(titles)):
        desc = descriptions[i] if i < len(descriptions) else None
        desc_str = f" - {desc[:40]}..." if desc and len(desc) > 0 else ""
        print(f"  • {titles[i]}{desc_str}")
    print()


def get_last_action_for_project(
    project_id, project_title, todoist_index, log_index, _commitment_title=None
):
    """Get the most recent action for a project: completed Todoist task OR LogEntries entry.
    Uses pre-built indexes to avoid N+1 API calls."""
    best = None
    best_date = None

    project_tasks = todoist_index.get(project_title, [])

    for t in project_tasks:
        if t["checked"]:
            date = t.get("due_date")
            if date and (best_date is None or date > best_date):
                best_date = date
                best = {
                    "type": "Todoist",
                    "content": t["content"][:50],
                    "date": date,
                }

    activity = get_log_entries(target_project_id=project_id, limit=1, _log_index=log_index)
    if activity:
        log_date = format_timestamp(activity[0].get("effective_date"))
        if log_date and (best_date is None or log_date > best_date):
            best_date = log_date
            best = {
                "type": "Activity",
                "content": activity[0]["content"][:50],
                "date": log_date,
            }

    return best


@app.command()
def projects():
    """List all projects with their commitment, status, and last action."""
    # Fetch all data upfront to avoid N+1 queries
    data = grist_get("Project")
    if not data:
        print("No projects found")
        return

    titles = data.get("Title", [])
    ids = data.get("id", [])
    commitments = data.get("Commitment", [])
    statuses = data.get("Status", [])

    # Build in-memory indexes from pre-fetched data
    todoist_index = _build_todoist_index(grist_get("Todoist"))
    log_index = _build_log_index(grist_get("LogEntries"))

    commitment_titles = {}
    comm_data = grist_get("Commitments")
    if comm_data:
        for i in range(len(comm_data.get("Title", []))):
            commitment_titles[comm_data["id"][i]] = comm_data["Title"][i]

    print(f"\n{'=' * 60}")
    print(f"ALL PROJECTS ({len(titles)})")
    print(f"{'=' * 60}")
    for i in range(len(titles)):
        proj_id = ids[i] if i < len(ids) else None
        proj_title = titles[i]
        c_id = commitments[i] if i < len(commitments) else None
        c_name = commitment_titles.get(c_id) if c_id else None

        status = statuses[i] if i < len(statuses) else None
        last = get_last_action_for_project(proj_id, proj_title, todoist_index, log_index, c_name)

        c_str = f" [{c_name}]" if c_name else ""
        s_str = f" ({status})" if status else ""
        print(f"  • {proj_title}{s_str}{c_str}")
        if last:
            print(f"      {last['type']}: {last['content']} ({last['date']})")
    print()


def _get_next_log_id():
    """Find the next available LogId by taking max existing + 1."""
    data = grist_get("LogEntries")
    if not data:
        return 1
    log_ids = data.get("LogId", []) or []
    valid = [lid for lid in log_ids if lid is not None]
    return max(valid) + 1 if valid else 1


def add_log_entry(content, project_id=None, commitment_id=None, activity_date=None):
    """Create a log entry in Grist's LogEntries table."""
    log_id = _get_next_log_id()
    fields = {"Content": content, "LogId": log_id}

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
            print(f"✓ Log entry created (L#{log_id})")
            return True
        else:
            print(f"✗ Failed to create log entry: {resp.status_code}")
            print(f"  {resp.text}")
            return False


def update_log_entry(log_id, content=None, activity_date=None):
    """Update a log entry's content and/or date using its stable LogId."""
    grist_id = _log_id_to_grist_id(log_id)
    if grist_id is None:
        print(f"Error: No log entry found with LogId {log_id}")
        return False

    fields = {}
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


def delete_log_entry(log_id):
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


def _build_project_titles():
    """Build a dict mapping project id → project title."""
    data = grist_get("Project")
    if not data:
        return {}
    titles = {}
    for i in range(len(data.get("id", []))):
        titles[data["id"][i]] = data.get("Title", [])[i] if i < len(data.get("Title", [])) else None
    return titles


def list_logs(limit=30, project_filter=None):
    """List all log entries with project names."""
    data = grist_get("LogEntries")
    if not data:
        print("No log entries found")
        return

    project_titles = _build_project_titles()

    # Build list of entries with project names
    entries = []
    log_ids = data.get("LogId", [])
    content_list = data.get("Content", [])
    dates = data.get("EffectiveDate", [])
    target_projects = data.get("Target_Project", [])

    for i in range(len(content_list)):
        tp = target_projects[i] if i < len(target_projects) else None
        proj_name = project_titles.get(tp, "") if tp else ""

        if project_filter and project_filter.lower() not in proj_name.lower():
            continue

        entries.append({
            "log_id": log_ids[i] if i < len(log_ids) else None,
            "content": content_list[i],
            "date": format_timestamp(dates[i]) if i < len(dates) else "",
            "project": proj_name,
        })

    # Sort by date descending (most recent first)
    entries.sort(key=lambda x: x["date"], reverse=True)
    entries = entries[:limit]

    if not entries:
        print("No log entries found")
        return

    print(f"\n{'=' * 60}")
    print(f"RECENT LOGS ({len(entries)})")
    print(f"{'=' * 60}")
    for e in entries:
        lid = f"L#{e['log_id']}" if e["log_id"] else "#?"
        proj = f" [{e['project']}]" if e["project"] else ""
        print(f"  [{lid}] [{e['date']}]{proj} {(e.get('content') or '')[:60]}")
    print()


def search_logs(query, limit=20):
    """Search log entries by content (case-insensitive)."""
    data = grist_get("LogEntries")
    if not data:
        print("No log entries found")
        return

    project_titles = _build_project_titles()

    matches = []
    log_ids = data.get("LogId", [])
    content_list = data.get("Content", [])
    dates = data.get("EffectiveDate", [])
    target_projects = data.get("Target_Project", [])

    q = query.lower()
    for i in range(len(content_list)):
        if q in content_list[i].lower():
            tp = target_projects[i] if i < len(target_projects) else None
            proj_name = project_titles.get(tp, "") if tp else ""
            matches.append({
                "log_id": log_ids[i] if i < len(log_ids) else None,
                "content": content_list[i],
                "date": format_timestamp(dates[i]) if i < len(dates) else "",
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
):
    """Set the status of a project."""
    project = get_project(project_name)
    if not project:
        print(f"Error: Project '{project_name}' not found")
        raise typer.Exit(1)

    resp = grist_patch("Project", [{"id": project["id"], "fields": {"Status": new_status.value}}])

    if resp.status_code in (200, 201):
        print(f"✓ {project_name} → {new_status.value}")
    else:
        print(f"✗ Failed to update status: {resp.status_code}")
        print(f"  {resp.text}")
        raise typer.Exit(1)


@app.command()
def query(
    name: str = typer.Argument(help="Commitment or project name"),
):
    """Query a commitment or project by name."""
    commitment = get_commitment(name)
    if commitment:
        query_commitment(name)
    else:
        project = get_project(name)
        if project:
            query_project(name)
        else:
            print(f"'{name}' not found as a commitment or project")


@log_app.command()
def add(
    content: str = typer.Argument(help="Log message content"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project name"),
    commitment: str | None = typer.Option(None, "--commitment", "-c", help="Commitment name"),
    date: str | None = typer.Option(None, "--date", "-d", help="Activity date"),
):
    """Add a log entry."""
    if not project and not commitment:
        print("Error: You must provide --project or --commitment")
        raise typer.Exit(1)
    if project and commitment:
        print("Error: Provide --project OR --commitment, not both")
        raise typer.Exit(1)

    project_id = None
    if project:
        p = get_project(project)
        if not p:
            print(f"Error: Project '{project}' not found")
            raise typer.Exit(1)
        project_id = p["id"]
        print(f"Logging to project: {project}")

    commitment_id = None
    if commitment:
        c = get_commitment(commitment)
        if not c:
            print(f"Error: Commitment '{commitment}' not found")
            raise typer.Exit(1)
        commitment_id = c["id"]
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
):
    """List recent log entries."""
    list_logs(limit=limit, project_filter=project)


@log_app.command()
def view(
    query: str = typer.Argument(help="Search term"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number of entries"),
):
    """Search log entries by content."""
    search_logs(query, limit=limit)


@log_app.command()
def update(
    log_id: int = typer.Argument(help="Log entry ID"),
    content: str | None = typer.Option(None, "--content", "-c", help="New content"),
    date: str | None = typer.Option(None, "--date", "-d", help="New date"),
):
    """Update a log entry."""
    update_log_entry(log_id, content=content, activity_date=date)


@log_app.command()
def delete(
    log_id: int = typer.Argument(help="Log entry ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a log entry."""
    if not yes:
        data = grist_get("LogEntries")
        if data:
            log_ids = data.get("LogId", [])
            content_list = data.get("Content", [])
            dates = data.get("EffectiveDate", [])
            for i in range(len(log_ids)):
                if log_ids[i] == log_id:
                    from datetime import datetime

                    ts = dates[i] if i < len(dates) else None
                    date_str = ""
                    if isinstance(ts, (int, float)):
                        date_str = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
                    print(f"Log entry L#{log_id}: [{date_str}] {content_list[i][:80]}")
                    break
        print("Use --yes to confirm deletion")
        raise typer.Exit(1)

    delete_log_entry(log_id)


if __name__ == "__main__":
    app()
