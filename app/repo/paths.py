from pathlib import Path


def workspace_root(run_id: str) -> Path:
    return Path("data/workspaces") / run_id


def repo_clone_dir(run_id: str) -> Path:
    return workspace_root(run_id) / "repo"


def normalize_subpath(subpath: str | None) -> str:
    if subpath is None:
        return ""
    return str(subpath).strip().strip("/\\")


def normalize_github_repo(repo_full: str, candidates: list[str] | None = None) -> str:
    """Resolve owner/name; bare slugs match github_repos when unambiguous."""
    raw = (repo_full or "").strip().removesuffix(".git").strip("/")
    if not raw:
        raise ValueError("CLONE_FAILED: GitHub repo is required (owner/name)")

    if raw.startswith("http://") or raw.startswith("https://"):
        parts = [p for p in raw.split("/") if p and p.lower() not in ("github.com", "www.github.com")]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"

    if "/" in raw:
        owner, name = raw.split("/", 1)
        owner, name = owner.strip(), name.strip()
        if owner and name:
            return f"{owner}/{name}"

    slug = raw
    matches: list[str] = []
    for c in candidates or []:
        c = (c or "").strip().removesuffix(".git")
        if "/" not in c:
            continue
        if c.split("/")[-1].lower() == slug.lower():
            matches.append(c)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"CLONE_FAILED: repo name '{slug}' matches multiple GitHub repos — use full owner/name"
        )
    raise ValueError(
        f"CLONE_FAILED: repo must be owner/name (e.g. your-user/{slug}), not bare '{slug}'"
    )


def deploy_root(run_id: str, subpath: str | None = "") -> Path:
    base = repo_clone_dir(run_id)
    sp = normalize_subpath(subpath)
    if sp:
        return base / sp
    return base
