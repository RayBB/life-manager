#!/usr/bin/env python3
"""
Sync Todoist tasks to Grist

Usage:
    uv run python sync_todoist_to_grist.py
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import typer
from pydantic import BaseModel, Field

from grist_api import grist_get, grist_patch, grist_post
from settings import settings

app = typer.Typer(no_args_is_help=True)
TODOIST_BASE_URL = "https://api.todoist.com/api/v1"


class TodoistDue(BaseModel):
    datetime: str = ""
    date: str = ""
    string: str = ""


class TodoistTask(BaseModel):
    id: str = ""
    content: str = ""
    description: str = ""
    priority: int = 1
    due: TodoistDue | None = None
    project_id: str = ""
    labels: list[str] = []
    checked: bool = False
    added_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    def to_grist_fields(self) -> GristFields:
        updated_at = self.completed_at if (self.checked and self.completed_at) else self.updated_at
        due_date = None
        if self.due:
            due_date = self.due.datetime or (self.due.date[:10] if self.due.date else None)

        return GristFields(
            todoist_id=self.id,
            content=self.content,
            description=self.description,
            priority=self.priority,
            due_date=due_date,
            due_string=self.due.string if self.due else "",
            project_id=self.project_id,
            labels=["L", *self.labels] if self.labels else None,
            checked=self.checked,
            added_at=self.added_at,
            updated_at=updated_at,
        )


class GristFields(BaseModel):
    todoist_id: str = Field("", serialization_alias="TodoistId")
    content: str = Field("", serialization_alias="Content")
    description: str = Field("", serialization_alias="Description")
    priority: int = Field(1, serialization_alias="Priority")
    due_date: str | None = Field(None, serialization_alias="DueDate")
    due_string: str = Field("", serialization_alias="DueString")
    project_id: str = Field("", serialization_alias="ProjectId")
    labels: list[str] | None = Field(None, serialization_alias="Labels")
    checked: bool = Field(False, serialization_alias="Checked")
    added_at: str = Field("", serialization_alias="AddedAt")
    updated_at: str = Field("", serialization_alias="UpdatedAt")


async def get_all_active_tasks(client: httpx.AsyncClient) -> list[TodoistTask]:
    headers = {"Authorization": f"Bearer {settings.todoist_api_token}"}
    all_tasks: list[TodoistTask] = []
    cursor: str | None = None

    while True:
        url = f"{TODOIST_BASE_URL}/tasks"
        if cursor:
            url += f"?cursor={cursor}"

        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Failed to get active tasks: {response.status_code}")
            return []

        data: dict[str, Any] = response.json()
        results: list[dict[str, Any]] = data.get("results", [])
        all_tasks.extend(TodoistTask.model_validate(r) for r in results)
        cursor = data.get("next_cursor")
        if not cursor:
            break

    print(f"Found {len(all_tasks)} active tasks")
    return all_tasks


async def get_all_completed_tasks(client: httpx.AsyncClient) -> list[TodoistTask]:
    headers = {"Authorization": f"Bearer {settings.todoist_api_token}"}

    response = await client.get(
        f"{TODOIST_BASE_URL}/tasks/completed?limit=100",
        headers=headers,
    )

    if response.status_code != 200:
        print(f"Failed to get completed tasks: {response.status_code}")
        return []

    data: dict[str, Any] = response.json()
    items: list[dict[str, Any]] = data.get("items", [])
    transformed: list[TodoistTask] = []
    for item in items:
        transformed.append(
            TodoistTask(
                id=item["task_id"],
                content=item["content"],
                checked=True,
                updated_at=item.get("completed_at", ""),
                project_id=item.get("project_id", ""),
            )
        )

    print(f"Found {len(transformed)} completed tasks")
    return transformed


async def sync_to_grist(tasks: list[TodoistTask]) -> tuple[int, int]:
    data = await grist_get("Todoist")
    if data is None:
        return 0, 0

    existing_ids: set[str] = set()
    grist_id_map: dict[str, int] = {}

    todoist_ids: list[str] = data.get("TodoistId", [])
    grist_ids: list[int] = data.get("id", [])
    for tid, gid in zip(todoist_ids, grist_ids):
        if tid:
            existing_ids.add(tid)
            grist_id_map[tid] = gid

    records_to_add: list[dict[str, Any]] = []
    records_to_update: list[dict[str, Any]] = []

    for task in tasks:
        if not task.id:
            continue
        fields = task.to_grist_fields().model_dump(mode="json", by_alias=True)
        if task.id in existing_ids:
            records_to_update.append({"id": grist_id_map[task.id], "fields": fields})
        else:
            records_to_add.append({"fields": fields})

    if records_to_add:
        add_response = await grist_post("tables/Todoist/records", {"records": records_to_add})
        print(f"Added {len(records_to_add)} new tasks")
        if add_response.status_code not in (200, 201):
            print(f"Add error: {add_response.text}")

    if records_to_update:
        update_response = await grist_patch("Todoist", records_to_update)
        print(f"Updated {len(records_to_update)} existing tasks")
        if update_response.status_code not in (200, 201):
            print(f"Update error: {update_response.text}")

    return len(records_to_add), len(records_to_update)


_LAST_SYNC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_sync")
_SYNC_COOLDOWN_MINUTES = 5


def _last_sync_ago() -> float | None:
    """Return seconds since last sync, or None if never synced."""
    try:
        mtime = os.path.getmtime(_LAST_SYNC_FILE)
        return time.time() - mtime
    except FileNotFoundError:
        return None


def _mark_synced() -> None:
    """Write a timestamp so the next call knows when we ran."""
    with open(_LAST_SYNC_FILE, "w") as f:
        f.write(str(time.time()))


async def sync_if_due(force: bool = False, quiet: bool = False) -> None:
    """Sync Todoist -> Grist, respecting the cooldown.

    Args:
        force: Skip the cooldown check.
        quiet: When True, suppress verbose output and swallow non-fatal
               errors (network blips shouldn't crash a query command).
               Still shows a single "Synced" line if sync actually runs.
    """
    try:
        if not force:
            ago_t = _last_sync_ago()
            if ago_t is not None and ago_t < _SYNC_COOLDOWN_MINUTES * 60:
                if not quiet:
                    remaining = int(_SYNC_COOLDOWN_MINUTES * 60 - ago_t)
                    print(
                        f"Sync ran {int(ago_t)}s ago. Skipping"
                        f" (cooldown: {_SYNC_COOLDOWN_MINUTES}min)."
                    )
                    print(f"Use --force to sync now, or wait {remaining}s.")
                return

        async with httpx.AsyncClient(timeout=30.0) as client:
            active_tasks, completed_tasks = await asyncio.gather(
                get_all_active_tasks(client),
                get_all_completed_tasks(client),
            )
            all_tasks = [*active_tasks, *completed_tasks]
            if not all_tasks:
                if not quiet:
                    print("No tasks to sync")
                _mark_synced()
                return

            added, updated = await sync_to_grist(all_tasks)
            _mark_synced()
            if quiet:
                print(f"Synced Todoist tasks (+{added}, ~{updated})")
            else:
                print(f"Total tasks (active + completed): {len(all_tasks)}")
                print(f"Done! Added: {added}, Updated: {updated}")
    except Exception:
        if not quiet:
            raise
        # In quiet mode, sync failures are non-fatal — the caller
        # is a query command that works fine with stale data.


@app.command()
def sync(
    force: bool = typer.Option(False, "--force", "-f", help="Skip the 5-minute cooldown check"),
) -> None:
    """Sync all active and completed Todoist tasks to Grist.

    Skips if a sync ran within the last 5 minutes (use --force to override).
    """

    async def _run() -> None:
        await sync_if_due(force=force, quiet=False)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
