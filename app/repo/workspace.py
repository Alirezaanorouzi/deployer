import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import get_settings
from app.repo.paths import deploy_root, repo_clone_dir, workspace_root
from app.sandbox.docker_runner import run_in_sandbox


def slug_app_name(repo_full: str, override: str | None = None) -> str:
    raw = override or repo_full.split("/")[-1]
    slug = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug or "liara-app")[:40]


async def clone_repository(
    run_id: str,
    repo_full: str,
    on_log: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.github_token:
        raise RuntimeError("CLONE_FAILED: GITHUB_TOKEN is not configured")

    owner, repo = repo_full.split("/", 1)
    dest = repo_clone_dir(run_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://x-access-token:{settings.github_token}@github.com/{owner}/{repo}.git"

    if on_log:
        await on_log(f"$ git clone --depth 1 {owner}/{repo} (sandbox)")

    result = await run_in_sandbox(
        run_id,
        ["git", "clone", "--depth", "1", clone_url, "repo"],
        workdir_in_container="/workspace",
        env={"GIT_TERMINAL_PROMPT": "0"},
        on_line=on_log,
    )
    if not result.ok:
        raise RuntimeError("CLONE_FAILED: " + result.output[-3000:])

    sha_res = await run_in_sandbox(
        run_id,
        ["git", "-C", "repo", "rev-parse", "HEAD"],
        workdir_in_container="/workspace",
        on_line=on_log,
    )
    sha = sha_res.output.strip().splitlines()[-1] if sha_res.output else ""
    if on_log:
        await on_log(f"clone done @ {sha[:12]}")
    return {"path": str(dest), "commit": sha}


def analyze_local_directory(root: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    stack = "unknown"
    pkg_path = root / "package.json"
    liara_path = root / "liara.json"
    suggested: dict[str, str] = {}

    if pkg_path.is_file():
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "next" in deps:
            stack = "next"
        elif "react" in deps:
            stack = "react"
        if "build" not in (pkg.get("scripts") or {}):
            findings.append(
                {"severity": "high", "code": "NO_BUILD_SCRIPT", "message": "Missing build script in package.json"}
            )
    else:
        findings.append({"severity": "high", "code": "NO_PACKAGE_JSON", "message": "package.json not found"})

    if not liara_path.is_file():
        findings.append({"severity": "medium", "code": "NO_LIARA_JSON", "message": "liara.json missing"})
        platform = "next" if stack == "next" else "node"
        suggested["liara.json"] = json.dumps(
            {"platform": platform, "app": root.name, "port": 3000},
            indent=2,
        )
    return {"stack": stack, "findings": findings, "suggested_files": suggested}


def write_liara_config(root: Path, app_name: str, analysis: dict[str, Any]) -> str:
    liara_path = root / "liara.json"
    if liara_path.is_file():
        return f"liara.json already exists at {liara_path}"

    suggested = analysis.get("suggested_files") or {}
    content = suggested.get("liara.json")
    if not content:
        platform = "next" if analysis.get("stack") == "next" else "node"
        content = json.dumps({"platform": platform, "app": app_name, "port": 3000}, indent=2)
    else:
        try:
            obj = json.loads(content)
            obj["app"] = app_name
            content = json.dumps(obj, indent=2)
        except json.JSONDecodeError:
            pass
    liara_path.write_text(content, encoding="utf-8")
    return f"Wrote {liara_path}"
