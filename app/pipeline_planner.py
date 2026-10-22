from app.risk import OperationType, classify_step
from app.repo.paths import normalize_subpath
from app.repo.workspace import slug_app_name
from app.schemas import Plan, PlanStep

PIPELINE_SKILL = "pipeline.github_to_liara"
APPROVAL_BEFORE_STEP = "liara_create_app"


def plan_pipeline_github_to_liara(
    repo: str,
    subpath: str = "",
    liara_app_name: str | None = None,
) -> Plan:
    subpath = normalize_subpath(subpath)
    app_name = slug_app_name(repo, liara_app_name)
    steps = [
        PlanStep(
            name="github_clone",
            operation=OperationType.read.value,
            risk=classify_step(OperationType.read),
            params={"repo": repo},
        ),
        PlanStep(
            name="repo_analyze",
            operation=OperationType.read.value,
            risk=classify_step(OperationType.read),
            params={"subpath": subpath},
        ),
        PlanStep(
            name="write_liara_config",
            operation=OperationType.env_set.value,
            risk=classify_step(OperationType.env_set),
            params={"app_name": app_name, "subpath": subpath},
        ),
        PlanStep(
            name="liara_auth_check",
            operation=OperationType.read.value,
            risk=classify_step(OperationType.read),
            params={},
        ),
        PlanStep(
            name="liara_create_app",
            operation=OperationType.create_app.value,
            risk=classify_step(OperationType.create_app),
            params={"app_name": app_name},
        ),
        PlanStep(
            name="liara_deploy",
            operation=OperationType.deploy.value,
            risk=classify_step(OperationType.deploy, target_env="prod"),
            params={"app_name": app_name, "port": 3000, "subpath": subpath},
        ),
        PlanStep(
            name="resolve_site_url",
            operation=OperationType.read.value,
            risk=classify_step(OperationType.read),
            params={"app_name": app_name},
        ),
        PlanStep(
            name="verify_health",
            operation=OperationType.read.value,
            risk=classify_step(OperationType.read),
            params={"app_name": app_name},
        ),
    ]
    return Plan(
        skill_id=PIPELINE_SKILL,
        summary_en=f"Deploy {repo} to new Liara app «{app_name}» (clone → configure → create → deploy → verify).",
        summary_fa=f"دیپلوی {repo} به اپ لیارا «{app_name}».",
        steps=steps,
    )


def approval_step_index(plan: Plan) -> int:
    for i, step in enumerate(plan.steps):
        if step.name == APPROVAL_BEFORE_STEP:
            return i
    return len(plan.steps)
