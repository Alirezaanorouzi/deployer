"""Local vs cloud Cursor SDK runtime."""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.cursor_agent.model_selection import cursor_model_selection

_DEPLOYER_ROOT = Path(__file__).resolve().parents[2]


def cursor_runtime_mode() -> str:
    raw = (get_settings().cursor_sdk_runtime or "cloud").strip().lower()
    return raw if raw in ("local", "cloud") else "cloud"


def github_repo_url(full_name: str) -> str:
    slug = full_name.strip().removesuffix(".git").strip("/")
    if slug.startswith("http://") or slug.startswith("https://"):
        return slug
    return f"https://github.com/{slug}"


def bridge_workspace_key(mode: str, local_cwd: str) -> str:
    return "__cloud__" if mode == "cloud" else local_cwd


def bridge_workspace_path(mode: str, local_cwd: str) -> str:
    if mode == "cloud":
        return str(_DEPLOYER_ROOT)
    return local_cwd


def build_agent_options(
    *,
    api_key: str,
    model_id: str,
    local_cwd: str | None,
    repo: str | None,
) -> AgentOptions:
    from cursor_sdk import AgentOptions, CloudAgentOptions, CloudRepository, LocalAgentOptions

    settings = get_settings()
    model = cursor_model_selection(model_id or settings.cursor_sdk_model or "composer-2.5")
    mode = cursor_runtime_mode()

    if mode == "cloud":
        if not (repo or "").strip():
            raise ValueError("Select a GitHub repository for Cursor Cloud.")
        branch = (settings.cursor_cloud_branch or "main").strip() or "main"
        return AgentOptions(
            api_key=api_key,
            model=model,
            cloud=CloudAgentOptions(
                repos=[
                    CloudRepository(
                        url=github_repo_url(repo),
                        starting_ref=branch,
                    )
                ],
                auto_create_pr=bool(settings.cursor_cloud_auto_pr),
            ),
        )

    if not local_cwd:
        raise ValueError("No local workspace directory for Cursor agent.")
    return AgentOptions(
        api_key=api_key,
        model=model,
        local=LocalAgentOptions(cwd=local_cwd),
    )


def workspace_label(mode: str, repo: str | None, local_cwd: str) -> str:
    if mode == "cloud":
        return f"cloud:{repo or '?'}"
    return local_cwd
