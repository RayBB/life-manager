#!/usr/bin/env python3
"""
Sync Todoist tasks to Grist

Usage:
    uv run python sync_todoist_to_grist.py
"""

from typing import Any

import httpx
import typer
from pydantic import BaseModel, Field

from settings import settings

app = typer.Typer()
GRIST_BASE_URL = f"https://docs.getgrist.com/api/docs/{settings.grist_doc_id}"
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


def parse_due_date(due: TodoistDue | None) -> str | None:
    if due is None:
        return None
    if due.datetime:
        return due.datetime
    if due.date:
        return due.date[:10]
    return None


def format_labels_for_grist(labels: list[str]) -> list[str] | None:
    if not labels:
        return None
    return ["L", *labels]


def task_to_grist_fields(task: TodoistTask) -> GristFields:
    updated_at = task.updated_at
    if task.checked and task.completed_at:
        updated_at = task.completed_at

    return GristFields(
        todoist_id=task.id,
        content=task.content,
        description=task.description,
        priority=task.priority,
        due_date=parse_due_date(task.due),
        due_string=task.due.string if task.due else "",
        project_id=task.project_id,
        labels=format_labels_for_grist(task.labels),
        checked=task.checked,
        added_at=task.added_at,
        updated_at=updated_at,
    )


def get_all_active_tasks() -> list[TodoistTask]:
    headers = {"Authorization": f"Bearer {settings.todoist_api_token}"}
    all_tasks: list[TodoistTask] = []
    cursor: str | None = None

    with httpx.Client(timeout=30.0) as client:
        while True:
            url = f"{TODOIST_BASE_URL}/tasks"
            if cursor:
                url += f"?cursor={cursor}"

            response = client.get(url, headers=headers)
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


def get_all_completed_tasks() -> list[TodoistTask]:
    headers = {"Authorization": f"Bearer {settings.todoist_api_token}"}

    with httpx.Client(timeout=30.0) as client:
        response = client.get(
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


def sync_to_grist(tasks: list[TodoistTask]) -> tuple[int, int]:
    headers = {"Authorization": f"Bearer {settings.grist_api_key}"}

    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            f"{GRIST_BASE_URL}/tables/Todoist/data",
            headers=headers,
        )
        existing_ids: set[str] = set()
        grist_id_map: dict[str, int] = {}

        if response.status_code == 200:
            data: dict[str, Any] = response.json()
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
            fields = task_to_grist_fields(task).model_dump(mode="json", by_alias=True)
            if task.id in existing_ids:
                records_to_update.append({"id": grist_id_map[task.id], "fields": fields})
            else:
                records_to_add.append({"fields": fields})

        if records_to_add:
            add_response = client.post(
                f"{GRIST_BASE_URL}/tables/Todoist/records",
                headers=headers,
                json={"records": records_to_add},
            )
            print(f"Added {len(records_to_add)} new tasks")
            if add_response.status_code not in (200, 201):
                print(f"Add error: {add_response.text}")

        if records_to_update:
            update_response = client.patch(
                f"{GRIST_BASE_URL}/tables/Todoist/records",
                headers=headers,
                json={"records": records_to_update},
            )
            print(f"Updated {len(records_to_update)} existing tasks")
            if update_response.status_code not in (200, 201):
                print(f"Update error: {update_response.text}")

    return len(records_to_add), len(records_to_update)


@app.command()
def sync() -> None:
    print("Fetching active tasks from Todoist API v1...")
    active_tasks = get_all_active_tasks()
    print("Fetching completed tasks from Todoist API v1...")
    completed_tasks = get_all_completed_tasks()
    all_tasks = [*active_tasks, *completed_tasks]
    print(f"Total tasks (active + completed): {len(all_tasks)}")
    if not all_tasks:
        print("No tasks to sync")
        return
    print("Syncing to Grist...")
    added, updated = sync_to_grist(all_tasks)
    print(f"Done! Added: {added}, Updated: {updated}")


if __name__ == "__main__":
    app()
