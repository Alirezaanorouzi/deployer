import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import get_settings
from app.http_client import async_client
from app.repo.paths import deploy_root, normalize_github_repo, repo_clone_dir
from app.sandbox.docker_runner import run_in_sandbox


def stack_to_liara_platform(stack: str) -> str:
    """Map analyze_local_directory stack to Liara create/deploy platform id."""
    s = (stack or "").lower()
    if s == "python":
        return "python"
    if s == "next":
        return "next"
    if s == "react":
        return "react"
    return "node"


def infer_liara_platform(root: Path) -> tuple[str, str]:
    """Return (platform, stack) from a project directory."""
    analysis = analyze_local_directory(root)
    stack = str(analysis.get("stack") or "unknown")
    if stack == "unknown":
        req = root / "requirements.txt"
        main_py = root / "app" / "main.py"
        if req.is_file() and main_py.is_file():
            stack = "python"
        elif (root / "package.json").is_file():
            stack = "node"
    platform = stack_to_liara_platform(stack)
    return platform, stack


def slug_app_name(repo_full: str, override: str | None = None) -> str:
    raw = override or repo_full.split("/")[-1]
    slug = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug or "liara-app")[:40]


def _python_liara_json(app_name: str) -> str:
    return json.dumps(
        {
            "platform": "python",
            "app": app_name,
            "port": 8000,
            "args": ["python main.py"],
            "python": {"version": "3.12"},
        },
        indent=2,
    )


def _git_rev_parse(dest: Path) -> str:
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        return ""
    lines = (r.stdout or "").strip().splitlines()
    return lines[-1] if lines else ""


def _run_host_git_clone(clone_url: str, dest: Path) -> tuple[int, str]:
    """Sync git clone (safe on Windows uvicorn event loops)."""
    import os
    import subprocess

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    r = subprocess.run(
        [
            "git",
            "-c",
            "credential.helper=",
            "clone",
            "--depth",
            "1",
            clone_url,
            str(dest),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


async def _safe_log(
    on_log: Callable[[str], Awaitable[None]] | None,
    line: str,
) -> None:
    if not on_log:
        return
    import asyncio

    try:
        result = on_log(line)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def _clone_via_host_git(
    clone_url: str,
    dest: Path,
    on_log: Callable[[str], Awaitable[None]] | None,
) -> None:
    import asyncio

    await _safe_log(on_log, "$ git clone --depth 1 (host)")

    code, output = await asyncio.to_thread(_run_host_git_clone, clone_url, dest)
    if output:
        for line in output.splitlines()[-40:]:
            await _safe_log(on_log, line)
    if code != 0:
        detail = output.strip() or f"git exit code {code}"
        raise RuntimeError(f"CLONE_FAILED: {detail[-3000:]}")


async def _clone_via_github_zipball(
    owner: str,
    repo: str,
    token: str,
    dest: Path,
    on_log: Callable[[str], Awaitable[None]] | None,
) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    branches = ("main", "master")
    last_err = ""
    async with async_client(follow_redirects=True, timeout=120) as client:
        for branch in branches:
            url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
            await _safe_log(on_log, f"$ curl -L github zipball ({branch})")
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                last_err = f"{branch}: HTTP {r.status_code} {r.text[:200]}"
                continue
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            names = zf.namelist()
            if not names:
                last_err = f"{branch}: empty archive"
                continue
            root_prefix = names[0].split("/")[0] + "/"
            dest.mkdir(parents=True, exist_ok=True)
            for name in names:
                if not name.startswith(root_prefix) or name.endswith("/"):
                    continue
                rel = name[len(root_prefix) :]
                if not rel:
                    continue
                target = dest / rel
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
            await _safe_log(on_log, f"zipball extracted ({branch})")
            return branch
    raise RuntimeError(f"CLONE_FAILED: GitHub zipball failed — {last_err or 'unknown'}")


async def clone_repository(
    run_id: str,
    repo_full: str,
    on_log: Callable[[str], Awaitable[None]] | None = None,
    *,
    github_repos: list[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.github_token:
        raise RuntimeError("CLONE_FAILED: GITHUB_TOKEN is not configured")

    try:
        repo_full = normalize_github_repo(repo_full, github_repos)
    except ValueError as e:
        raise RuntimeError(str(e)) from e

    owner, repo = repo_full.split("/", 1)
    dest = repo_clone_dir(run_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://x-access-token:{settings.github_token}@github.com/{owner}/{repo}.git"

    await _safe_log(on_log, f"$ git clone --depth 1 {owner}/{repo}")

    import shutil as _shutil

    errors: list[str] = []

    def _has_tree() -> bool:
        return dest.is_dir() and any(dest.iterdir())

    # 1) GitHub zipball — no local subprocess (reliable on Windows + uvicorn)
    try:
        await _clone_via_github_zipball(owner, repo, settings.github_token, dest, on_log)
    except RuntimeError as e:
        errors.append(str(e))
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)

    if not _has_tree() and _shutil.which("git"):
        try:
            await _clone_via_host_git(clone_url, dest, on_log)
        except (RuntimeError, OSError) as e:
            errors.append(str(e))
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)

    if not _has_tree():
        try:
            result = await run_in_sandbox(
                run_id,
                ["git", "clone", "--depth", "1", clone_url, "repo"],
                workdir_in_container="/workspace",
                env={"GIT_TERMINAL_PROMPT": "0", "GITHUB_TOKEN": settings.github_token},
                on_line=on_log,
            )
            if not result.ok:
                detail = (result.output or "").strip() or "no git output (Docker image, token, or repo name)"
                errors.append(detail[-1500:])
            elif not _has_tree():
                errors.append("sandbox clone produced no files")
        except NotImplementedError:
            errors.append("Docker/async subprocess not available on this server event loop")

    if not _has_tree():
        combined = "\n".join(e for e in errors if e).strip() or "all clone methods failed"
        raise RuntimeError(f"CLONE_FAILED: {combined[-3000:]}")

    sha = ""
    if _shutil.which("git"):
        import asyncio

        try:
            sha = await asyncio.to_thread(_git_rev_parse, dest)
        except OSError:
            sha = ""
    if not sha:
        sha = "zipball"

    await _safe_log(on_log, f"clone done @ {sha[:12]}")
    return {"path": str(dest), "commit": sha}


def analyze_local_directory(root: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    stack = "unknown"
    pkg_path = root / "package.json"
    liara_path = root / "liara.json"
    suggested: dict[str, str] = {}

    req_path = root / "requirements.txt"
    main_py = root / "app" / "main.py"
    if req_path.is_file() and main_py.is_file():
        stack = "python"
        suggested["liara.json"] = _python_liara_json(root.name)
    elif pkg_path.is_file():
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
        if not req_path.is_file():
            findings.append({"severity": "high", "code": "NO_PACKAGE_JSON", "message": "package.json not found"})

    if not liara_path.is_file():
        findings.append({"severity": "medium", "code": "NO_LIARA_JSON", "message": "liara.json missing"})
        if stack == "python":
            suggested.setdefault("liara.json", _python_liara_json(root.name))
        elif stack == "next":
            suggested.setdefault(
                "liara.json",
                json.dumps({"platform": "next", "app": root.name, "port": 3000}, indent=2),
            )
        else:
            suggested.setdefault(
                "liara.json",
                json.dumps({"platform": "node", "app": root.name, "port": 3000}, indent=2),
            )
    deploy_port = 8000 if stack == "python" else 3000
    if liara_path.is_file():
        try:
            deploy_port = int(json.loads(liara_path.read_text(encoding="utf-8")).get("port", deploy_port))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {
        "stack": stack,
        "deploy_port": deploy_port,
        "findings": findings,
        "suggested_files": suggested,
    }


def write_liara_config(root: Path, app_name: str, analysis: dict[str, Any]) -> str:
    liara_path = root / "liara.json"
    if liara_path.is_file():
        try:
            obj = json.loads(liara_path.read_text(encoding="utf-8"))
            obj["app"] = app_name
            if analysis.get("stack") == "python":
                if not obj.get("args"):
                    obj.setdefault("platform", "python")
                    obj.setdefault("port", 8000)
                    obj["args"] = ["uvicorn app.main:app --host 0.0.0.0 --port 8000"]
                # Writable storage uses DATA_DIR / paths.data_root() (/tmp fallback).
                # liara.json disk mounts require create_app_disk before deploy.
                obj.pop("disks", None)
            liara_path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            return f"Updated {liara_path} (app={app_name})"
        except json.JSONDecodeError:
            return f"liara.json already exists at {liara_path}"

    suggested = analysis.get("suggested_files") or {}
    content = suggested.get("liara.json")
    if not content:
        stack = analysis.get("stack")
        if stack == "python":
            content = _python_liara_json(app_name)
        elif stack == "next":
            content = json.dumps({"platform": "next", "app": app_name, "port": 3000}, indent=2)
        else:
            content = json.dumps({"platform": "node", "app": app_name, "port": 3000}, indent=2)
    else:
        try:
            obj = json.loads(content)
            obj["app"] = app_name
            content = json.dumps(obj, indent=2)
        except json.JSONDecodeError:
            pass
    liara_path.write_text(content, encoding="utf-8")
    return f"Wrote {liara_path}"
