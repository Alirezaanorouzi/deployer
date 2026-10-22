"""Resolve Liara project ids for any authenticated user."""

from __future__ import annotations

from typing import Any


def project_ids_from_list_payload(data: dict[str, Any] | None) -> list[str]:
    if not data:
        return []
    projects = data.get("projects", data)
    if not isinstance(projects, list):
        return []
    out: list[str] = []
    for p in projects:
        if isinstance(p, dict) and p.get("project_id"):
            out.append(str(p["project_id"]))
    return out


def resolve_project_id(
    hint: str | None,
    available: list[str],
) -> tuple[str | None, str | None]:
    """
    Match user/app hint to a Liara project_id.
    Returns (project_id, error_message).
    """
    ids = [x.strip() for x in available if x and str(x).strip()]
    if not ids:
        return None, "No Liara apps found on this account."

    if not hint or not str(hint).strip():
        if len(ids) == 1:
            return ids[0], None
        return None, f"Multiple apps: {', '.join(ids[:15])}. Specify which app (project_id)."

    h = str(hint).strip().lower()
    exact = [i for i in ids if i.lower() == h]
    if len(exact) == 1:
        return exact[0], None
    partial = [i for i in ids if h in i.lower()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, f"Ambiguous app name '{hint}'. Matches: {', '.join(partial[:10])}"
    return None, f"App '{hint}' not found. Your apps: {', '.join(ids[:15])}"
