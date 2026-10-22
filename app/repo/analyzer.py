import json
from dataclasses import dataclass, field

from app.github.client import GitHubClient


@dataclass
class RepoFinding:
    severity: str
    code: str
    message_fa: str
    file: str | None = None


@dataclass
class RepoAnalysis:
    owner: str
    repo: str
    ref: str
    stack: str
    findings: list[RepoFinding] = field(default_factory=list)
    suggested_files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "repo": self.repo,
            "ref": self.ref,
            "stack": self.stack,
            "findings": [f.__dict__ for f in self.findings],
            "suggested_files": self.suggested_files,
        }


def _parse_repo(full: str) -> tuple[str, str, str]:
    """owner/repo or owner/repo@branch or owner/repo:path ignored."""
    ref = "main"
    part = full.strip()
    if "@" in part:
        part, ref = part.split("@", 1)
    owner, repo = part.split("/", 1)
    return owner, repo, ref


async def analyze_repository(
    repo_full: str,
    github: GitHubClient | None = None,
    subpath: str = "",
) -> RepoAnalysis:
    gh = github or GitHubClient()
    owner, repo, ref = _parse_repo(repo_full)
    try:
        ref = await gh.get_default_branch(owner, repo)
    except Exception:
        pass

    prefix = subpath.strip("/")
    def p(name: str) -> str:
        return f"{prefix}/{name}" if prefix else name

    analysis = RepoAnalysis(owner=owner, repo=repo, ref=ref, stack="unknown")
    paths_to_try = [p(x) for x in ["package.json", "liara.json", "next.config.js", "next.config.mjs", "Dockerfile"]]

    contents: dict[str, str] = {}
    for path in paths_to_try:
        try:
            contents[path] = await gh.get_file(owner, repo, path, ref=ref)
        except Exception:
            continue

    pkg_key = next((k for k in contents if k.endswith("package.json")), None)
    if pkg_key:
        pkg = json.loads(contents[pkg_key])
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "next" in deps:
            analysis.stack = "next"
        elif "react" in deps:
            analysis.stack = "react"
        scripts = pkg.get("scripts") or {}
        if "build" not in scripts:
            analysis.findings.append(
                RepoFinding("high", "NO_BUILD_SCRIPT", "اسکریپت build در package.json نیست.", "package.json")
            )
        if "start" not in scripts and analysis.stack == "next":
            analysis.findings.append(
                RepoFinding(
                    "medium",
                    "NO_START",
                    "برای Liara Node معمولاً start یا output standalone لازم است.",
                    "package.json",
                )
            )
    else:
        analysis.findings.append(
            RepoFinding("high", "NO_PACKAGE_JSON", "package.json پیدا نشد؛ ریشه repo یا مسیر را بررسی کنید.", None)
        )

    liara_key = next((k for k in contents if k.endswith("liara.json")), None)
    if not liara_key:
        analysis.findings.append(
            RepoFinding(
                "medium",
                "NO_LIARA_JSON",
                "فایل liara.json نیست؛ برای دیسک و پلتفرم پیشنهاد می‌شود.",
                "liara.json",
            )
        )
        if analysis.stack == "next":
            liara_path = p("liara.json")
            analysis.suggested_files[liara_path] = json.dumps(
                {
                    "platform": "next",
                    "app": repo,
                    "port": 3000,
                    "build": {"command": "npm run build"},
                },
                indent=2,
            )
    else:
        try:
            lj = json.loads(contents[liara_key])
            if not lj.get("port"):
                analysis.findings.append(
                    RepoFinding("low", "LIARA_NO_PORT", "port در liara.json تنظیم نشده.", "liara.json")
                )
        except json.JSONDecodeError:
            analysis.findings.append(
                RepoFinding("high", "LIARA_JSON_INVALID", "liara.json نامعتبر است.", "liara.json")
            )

    return analysis


async def open_deploy_pr(
    repo_full: str,
    project_id: str,
    github: GitHubClient | None = None,
    subpath: str = "",
) -> dict:
    gh = github or GitHubClient()
    analysis = await analyze_repository(repo_full, gh, subpath=subpath)
    if not analysis.suggested_files:
        return {"ok": False, "message": "No file changes suggested", "analysis": analysis.to_dict()}

    owner, repo, _ = _parse_repo(repo_full)
    base = analysis.ref
    branch = f"liara-agent/setup-{project_id}"
    base_sha = await gh.get_branch_sha(owner, repo, base)
    try:
        await gh.create_branch(owner, repo, branch, base_sha)
    except Exception:
        pass

    for path, content in analysis.suggested_files.items():
        sha = await gh.file_sha(owner, repo, path, branch)
        await gh.upsert_file(
            owner,
            repo,
            path,
            content,
            f"chore(liara): add {path} for deploy",
            branch,
            sha=sha,
        )

    pr = await gh.create_pull_request(
        owner,
        repo,
        f"Liara deploy setup ({project_id})",
        branch,
        base,
        "پیشنهاد عامل DevOps لیارا: فایل‌های پایه deploy.\n\nلطفاً قبل از merge بررسی کنید.",
    )
    return {"ok": True, "pull_request": pr, "analysis": analysis.to_dict()}
