"""Build executable Plan from agent decision (deterministic step expansion)."""

from app.agent.planner_agent import AgentDecision
from app.pipeline_planner import plan_pipeline_github_to_liara
from app.planner import plan_from_message
from app.repo.paths import normalize_subpath
from app.schemas import Plan


def plan_from_decision(decision: AgentDecision) -> Plan:
    if decision.is_pipeline:
        repo = decision.params.get("repo") or decision.params.get("github_repo")
        if not repo:
            raise ValueError("Agent chose pipeline but no repo in params")
        plan = plan_pipeline_github_to_liara(
            repo,
            normalize_subpath(decision.params.get("subpath")),
            decision.params.get("liara_app_name") or decision.params.get("app_name"),
        )
        plan.summary_en = decision.summary_en or plan.summary_en
        plan.summary_fa = decision.summary_fa or plan.summary_fa
        plan.recommendation = decision.recommendation
        return plan

    app = decision.params.get("app_name") or "my-app"
    env = decision.params.get("target_env", "staging")
    health = decision.params.get("health_url")
    # Reuse legacy step templates via synthetic message + overrides
    base = plan_from_message(
        _skill_hint(decision.skill_id),
        app=app,
        target_env=env,
        health_url=health,
    )
    base.skill_id = decision.skill_id
    base.summary_en = decision.summary_en
    base.summary_fa = decision.summary_fa
    base.recommendation = decision.recommendation
    return base


def _skill_hint(skill_id: str) -> str:
    hints = {
        "liara.balance": "account balance credit remaining",
        "liara.usage": "cpu memory resource usage metrics service",
        "liara.deploy": "deploy",
        "liara.bootstrap": "bootstrap setup",
        "liara.health": "health status",
        "liara.logs": "logs",
        "liara.events": "platform events activity",
        "liara.history": "deploy release history",
        "liara.domains": "custom domain ssl dns",
        "liara.disks": "disk volume storage",
        "liara.settings": "app settings configuration",
        "liara.resize": "plan scale resize replicas",
        "incident.diagnose": "site slow error 500",
    }
    return hints.get(skill_id, "health")
