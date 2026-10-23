"""Infer GitHub repo + Liara app id from natural deploy chat (no owner/name required)."""

import re
from typing import Any


def infer_liara_app_name(user_message: str) -> str | None:
    patterns = (
        r"name of (?:the )?app(?:\s+would be|\s+is)\s+([a-z0-9][a-z0-9-]*)",
        r"app(?:\s+name)?(?:\s+would be|\s+is|:)\s*([a-z0-9][a-z0-9-]*)",
        r"اسم\s*برنامه[^a-z0-9\u0600-\u06ff]{0,24}(?:بزار|بذار|باشه?)\s*([a-z0-9][a-z0-9-]*)",
        r"برنامه[^a-z0-9]{0,12}(?:بزار|بذار)\s*([a-z0-9][a-z0-9-]*)",
    )
    for pat in patterns:
        mobj = re.search(pat, user_message, re.I)
        if mobj:
            return mobj.group(1).lower()
    return None


def infer_deploy_repo(user_message: str, github_repos: list[str] | None) -> str | None:
    repos = [str(r) for r in (github_repos or []) if r]
    if not repos:
        return None
    m = user_message.lower()
    for r in repos:
        if r.lower().endswith("/deployer"):
            return r
    deployish = any(
        x in m or x in user_message
        for x in (
            "deploy",
            "liara",
            "publish",
            "new project",
            "my project",
            "this project",
            "this repo",
            "my new",
            "پروژه",
            "منتشر",
            "لیارا",
            "دیپلوی",
        )
    )
    if not deployish:
        return None
    if len(repos) == 1:
        return repos[0]
    for r in repos:
        if "deployer" in r.lower():
            return r
    return None


def enrich_deploy_params(user_message: str, params: dict[str, Any], github_repos: list[str] | None) -> None:
    if github_repos:
        params.setdefault("github_repos", github_repos[:25])
    app = infer_liara_app_name(user_message)
    if app and not params.get("liara_app_name"):
        params["liara_app_name"] = app
        params.setdefault("app_name", app)
    if not params.get("repo"):
        picked = infer_deploy_repo(user_message, github_repos)
        if picked:
            params["repo"] = picked
