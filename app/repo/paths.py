from pathlib import Path


def workspace_root(run_id: str) -> Path:
    return Path("data/workspaces") / run_id


def repo_clone_dir(run_id: str) -> Path:
    return workspace_root(run_id) / "repo"


def normalize_subpath(subpath: str | None) -> str:
    if subpath is None:
        return ""
    return str(subpath).strip().strip("/\\")


def deploy_root(run_id: str, subpath: str | None = "") -> Path:
    base = repo_clone_dir(run_id)
    sp = normalize_subpath(subpath)
    if sp:
        return base / sp
    return base
