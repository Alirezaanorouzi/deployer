import json
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.llm.client import get_llm_client
from app.pipeline_planner import PIPELINE_SKILL
from app.repo.paths import normalize_github_repo


ALLOWED_SKILLS = [
    PIPELINE_SKILL,
    "liara.balance",
    "liara.usage",
    "liara.deploy",
    "liara.bootstrap",
    "liara.health",
    "liara.logs",
    "liara.events",
    "liara.history",
    "liara.domains",
    "liara.disks",
    "liara.settings",
    "liara.resize",
    "agent.summarize",
    "incident.diagnose",
]

# Skills executed via tool registry + ReAct loop (plan → tool → evaluate → replan).
REACT_SKILLS = frozenset(
    {
        "liara.balance",
        "liara.usage",
        "liara.logs",
        "liara.events",
        "liara.history",
        "liara.domains",
        "liara.disks",
        "liara.settings",
        "liara.resize",
        "liara.deploy",
        "agent.summarize",
    }
)


@dataclass
class AgentDecision:
    skill_id: str
    summary_en: str
    summary_fa: str
    params: dict[str, Any]
    assistant_message: str
    recommendation: str | None = None

    @property
    def is_pipeline(self) -> bool:
        return self.skill_id == PIPELINE_SKILL


def _apply_skill_overrides(
    user_message: str,
    skill_id: str,
    conversation_history: list[dict[str, str]] | None,
) -> str:
    """Deterministic fixes when the LLM picks the wrong skill."""
    m = user_message.lower()
    hist = conversation_history or []
    recap_words = (
        "conclusion",
        "summarize",
        "summary",
        "recap",
        "what we checked",
        "what you found",
        "overall",
        "in short",
        "wrap up",
    )
    if hist and any(w in m for w in recap_words):
        return "agent.summarize"
    persian = user_message  # keep Persian script for keyword checks
    slow_markers = ("کند", "کندی", "slow", "slowness", "latency")
    log_markers = ("لاگ", "log", "خطا", "error")
    if any(x in persian for x in log_markers) and any(x in persian for x in slow_markers):
        return "incident.diagnose"
    if any(x in m for x in ("slow", "slowness", "laggy")) and any(
        x in m for x in ("log", "logs", "error")
    ):
        return "incident.diagnose"
    return skill_id


def _apply_pipeline_repo_params(
    user_message: str,
    skill_id: str,
    params: dict[str, Any],
    github_repos: list[str] | None,
) -> None:
    if skill_id not in (PIPELINE_SKILL, "liara.deploy"):
        return
    if github_repos:
        params.setdefault("github_repos", github_repos[:25])
    repos = github_repos or []
    m = user_message.lower()
    if not params.get("repo"):
        if "deployer" in m or "دیپلوی" in user_message:
            for r in repos:
                if r.lower().endswith("/deployer"):
                    params["repo"] = r
                    break
        if not params.get("repo") and len(repos) == 1:
            params["repo"] = repos[0]
    if params.get("repo"):
        try:
            params["repo"] = normalize_github_repo(params["repo"], repos)
        except ValueError:
            pass
    if not params.get("liara_app_name") and params.get("repo"):
        params.setdefault("liara_app_name", params["repo"].split("/")[-1])


def _apply_confirm_write(user_message: str, params: dict[str, Any]) -> None:
    """When the user clearly approves a pending write, allow tools to proceed."""
    m = user_message.lower().strip()
    affirm = (
        "yes,",
        "yes ",
        "confirm",
        "approved",
        "go ahead",
        "do it",
        "please do",
        "تایید",
        "بله",
        "انجام بده",
        "اوکی",
    )
    deploy_intent = any(
        x in m for x in ("deploy", "publish", "release", "دیپلوی", "منتشر", "لیارا")
    )
    if any(a in m for a in affirm) and not deploy_intent:
        params["confirm_write"] = True


async def plan_with_agent(
    user_message: str,
    *,
    app: str | None = None,
    target_env: str = "staging",
    health_url: str | None = None,
    github_repos: list[str] | None = None,
    liara_projects: list[str] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
) -> AgentDecision:
    """LLM planner: intent → skill_id + parameters (no secrets, no shell)."""
    client = get_llm_client()
    settings = get_settings()

    context = {
        "default_app": app,
        "target_env": target_env,
        "health_url": health_url,
        "github_repos_sample": (github_repos or [])[:25],
        "liara_projects": (liara_projects or [])[:25],
        "allowed_skills": ALLOWED_SKILLS,
    }

    system = """You are the planner for a Liara DevOps AI agent (agentic service).
Your job: understand the user goal and choose ONE skill from allowed_skills.
Return ONLY valid JSON (no markdown) with this shape:
{
  "skill_id": "<one of allowed_skills>",
  "summary_en": "short plan for the user",
  "summary_fa": "خلاصه فارسی",
  "assistant_message": "friendly explanation of what will happen next",
  "recommendation": "optional infra tip or null",
  "params": {
    "repo": "owner/name — REQUIRED for pipeline.github_to_liara",
    "subpath": "optional monorepo path",
    "liara_app_name": "optional Liara app id slug",
    "app_name": "Liara project id for non-pipeline skills",
    "target_env": "staging|prod",
    "health_url": "optional full URL",
    "lines": "optional log line count for liara.logs",
    "focus": "optional log analysis focus for liara.logs",
    "since_hours": "optional hours of logs for liara.logs",
    "since_ts": "optional unix start for liara.logs",
    "until_ts": "optional unix end for liara.logs",
    "since_iso": "optional ISO start for liara.logs",
    "until_iso": "optional ISO end for liara.logs",
    "page": "optional page for liara.history (releases)",
    "count": "optional page size for liara.history",
    "search": "optional events search for liara.events",
    "confirm_write": "set true only after user explicitly approved a write action in chat",
    "plan_id": "target plan for liara.resize / change_service_plan",
    "scale": "replica count for scale_service",
    "env_key": "env var name for settings update",
    "env_value": "env var value",
    "domain_name": "FQDN for create_app_domain",
    "redirect_to": "redirect target URL",
    "disk_name": "disk name for create_app_disk",
    "size_gb": "disk size",
    "deploy_path": "local path for deploy_service"
  }
}
Rules:
- Use liara.balance when the user asks about account credit, remaining balance, wallet, billing, or "how much money left" on Liara.
- Use liara.usage when the user asks about CPU, memory, RAM, resource usage, metrics, or load of a Liara app/service. Set params.app_name from liara_projects or default_app when they name an app.
- Use liara.logs when the user wants service logs, log analysis, errors in logs, tail logs, or debugging from log output. Set params.app_name; time window via params.since_hours (e.g. 6, 24) or params.since_ts/params.until_ts or ISO times; optional params.lines / params.focus.
- Use liara.events for platform Events tab: activity feed, recent changes; optional params.since_hours or params.search.
- Use liara.history for deploy/release History (not a new deploy). Optional params.page / params.count.
- Use liara.domains for custom domains, DNS, SSL certificate status on an app.
- Use liara.disks for persistent disks/volumes attached to an app.
- Use liara.settings for app Settings (env var names, zero-downtime, network) — secrets are never exposed.
- Use liara.resize when the user asks about plan, pricing, scale/replicas, or resizing. Agent can change plan or scale with write tools after user confirms (confirm_write).
- Use liara.deploy when the user wants to publish or deploy to Liara (GitHub repo or local path). Set params.repo (owner/name) for GitHub; set deploy_path for local redeploy. Order: clone → prepare → create_app → **liara.deploy_service** (console Deploy tab) → get_app_url → verify_app_health; on 502/503 or failed deploy use **liara.diagnose_deploy_health** (logs + hints). Set LIARA_TEAM_ID for team accounts.
- Use liara.settings for env vars, zero-downtime, restart — writes need confirm_write after user approval.
- Use liara.domains / liara.disks for list or create/configure — writes need confirm_write. Disk names: hyphens only (shop-cache); size in GB as integer.
- Write tools refuse without confirm_write; explain the change first, then set confirm_write when user agrees.
- Prefer liara.deploy in chat for "deploy / publish / go live on Liara". Set params.repo to owner/name (e.g. Alirezaanorouzi/deployer) and params.liara_app_name or app_name.
- Use pipeline.github_to_liara only when the user explicitly wants the left-panel pipeline with Approve & continue (legacy UI flow).
- For this deployer FastAPI repo on GitHub, use repo Alirezaanorouzi/deployer — not a bare name deployer.
- Use agent.summarize when the user asks for a conclusion, recap, or summary of what was already discussed in chat (no new API calls).
- Use incident.diagnose when the user reports slowness AND wants logs checked, or a broad outage (health + metrics + logs + analysis). Always set params.app_name to the Liara project id.
- Prefer liara.logs when the user only wants logs without performance investigation.
- Use liara.health / liara.deploy / liara.bootstrap appropriately.
- Pick repo from github_repos_sample when user says "my shop project" etc.; if unclear, use best match or ask in assistant_message.
- NEVER output API tokens, passwords, or shell commands."""

    user = f"User message:\n{user_message}\n\nContext:\n{json.dumps(context, ensure_ascii=False)}"

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in (conversation_history or [])[-12:]:
        role = (item.get("role") or "").strip()
        content = (item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": content[:2000]})

    messages.append({"role": "user", "content": user})

    kwargs: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    # Some providers (e.g. Box API) only support default temperature.
    if "boxapi" not in settings.llm_base_url.lower():
        kwargs["temperature"] = 0.1
    resp = await client.chat.completions.create(**kwargs)
    raw = (resp.choices[0].message.content or "{}").strip()
    data = json.loads(raw)

    skill_id = data.get("skill_id", "liara.health")
    if skill_id not in ALLOWED_SKILLS:
        skill_id = "liara.health"

    skill_id = _apply_skill_overrides(user_message, skill_id, conversation_history)

    params = data.get("params") or {}
    _apply_pipeline_repo_params(user_message, skill_id, params, github_repos)
    if app and not params.get("app_name"):
        params["app_name"] = app
    if target_env:
        params.setdefault("target_env", target_env)
    if health_url:
        params.setdefault("health_url", health_url)

    _apply_confirm_write(user_message, params)
    if skill_id == "liara.deploy":
        # User asked to deploy/publish — same run may create app and upload code.
        params["confirm_write"] = True

    return AgentDecision(
        skill_id=skill_id,
        summary_en=data.get("summary_en", "Planned operation"),
        summary_fa=data.get("summary_fa", ""),
        params=params,
        assistant_message=data.get("assistant_message", data.get("summary_en", "")),
        recommendation=data.get("recommendation"),
    )
