"""Shared Grist API client — sync and async wrappers."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from settings import settings

GRIST_BASE_URL = f"https://docs.getgrist.com/api/docs/{settings.grist_doc_id}"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.grist_api_key}"}


# ── Sync wrappers (for CLI queries) ──


def grist_get(table: str) -> dict[str, Any] | None:
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(
            f"{GRIST_BASE_URL}/tables/{table}/data",
            headers=_auth_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
        print(f"Error fetching {table}: {resp.status_code} - {resp.text}")
        return None


def grist_patch(table: str, records: list[dict[str, Any]]) -> httpx.Response:
    with httpx.Client(timeout=10.0) as client:
        return client.patch(
            f"{GRIST_BASE_URL}/tables/{table}/records",
            headers=_auth_headers(),
            json={"records": records},
        )


def grist_post(path: str, json_body: Any) -> httpx.Response:
    with httpx.Client(timeout=10.0) as client:
        return client.post(
            f"{GRIST_BASE_URL}/{path}",
            headers=_auth_headers(),
            json=json_body,
        )


# ── Async wrappers (for sync_todoist_to_grist.py) ──


async def async_grist_get(table: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    resp = await client.get(
        f"{GRIST_BASE_URL}/tables/{table}/data",
        headers=_auth_headers(),
    )
    if resp.status_code == 200:
        return resp.json()
    print(f"Failed to read Grist table {table}: {resp.status_code}")
    return None


async def async_grist_post(
    table: str, records: list[dict[str, Any]], client: httpx.AsyncClient
) -> httpx.Response:
    return await client.post(
        f"{GRIST_BASE_URL}/tables/{table}/records",
        headers=_auth_headers(),
        json={"records": records},
    )


async def async_grist_patch(
    table: str, records: list[dict[str, Any]], client: httpx.AsyncClient
) -> httpx.Response:
    return await client.patch(
        f"{GRIST_BASE_URL}/tables/{table}/records",
        headers=_auth_headers(),
        json={"records": records},
    )


# ── Data conversion ──


def rows_from_data[M: BaseModel](data: dict[str, Any] | None, model: type[M]) -> list[M]:
    """Convert Grist columnar data (column-name → list-of-values) to model instances."""
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
