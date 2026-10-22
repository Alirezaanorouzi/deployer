from app.risk import OperationType, classify_step, max_risk
from app.schemas import Plan, PlanStep


def _detect_skill(message: str) -> str:
    m = message.lower()
    if any(w in m for w in ("500", "خطا", "error", "مشکل", "کند", "slow", "down")):
        return "incident.diagnose"
    if any(w in m for w in ("لاگ", "log")):
        return "liara.logs"
    if any(w in m for w in ("health", "سلامت", "وضعیت")):
        return "liara.health"
    if any(w in m for w in ("اول", "bootstrap", "setup", "راه‌اندازی", "شروع")):
        return "liara.bootstrap"
    if any(w in m for w in ("deploy", "دیپلوی", "انتشار")):
        return "liara.deploy"
    return "liara.health"


def plan_from_message(
    message: str,
    *,
    app: str | None,
    target_env: str,
    health_url: str | None,
) -> Plan:
    skill = _detect_skill(message)
    app_name = app or "my-app"
    url = health_url or f"https://{app_name}.liara.run"

    if skill == "liara.bootstrap":
        steps = [
            PlanStep(
                name="auth_check",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={},
            ),
            PlanStep(
                name="recommend_plan",
                operation=OperationType.guide.value,
                risk=classify_step(OperationType.guide),
                params={"stack_hint": _stack_hint(message)},
            ),
            PlanStep(
                name="first_deploy",
                operation=OperationType.deploy.value,
                risk=classify_step(OperationType.deploy, target_env=target_env),
                params={"app": app_name, "port": 3000},
            ),
            PlanStep(
                name="verify_health",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={"url": url},
            ),
        ]
        playbook = [
            "در پنل IRNIC یا ثبت‌کننده دامنه، رکوردهای DNS را طبق راهنمای لیارا تنظیم کنید.",
            "در پنل لیارا → دامنه‌ها، دامنه را اضافه و وضعیت SSL را بررسی کنید.",
            "تا propagate شدن DNS ممکن است چند ساعت زمان ببرد.",
        ]
        return Plan(
            skill_id=skill,
            summary_fa="راه‌اندازی اولیه: احراز هویت، پیشنهاد پلن، اولین دیپلوی و بررسی سلامت.",
            steps=steps,
            recommendation=_recommend_infra(message),
            domain_playbook=playbook,
        )

    if skill == "incident.diagnose":
        steps = [
            PlanStep(
                name="auth_check",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={},
            ),
            PlanStep(
                name="health_check",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={"url": url},
            ),
            PlanStep(
                name="fetch_metrics",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={"app": app_name},
            ),
            PlanStep(
                name="tail_logs",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={"app": app_name, "lines": 100},
            ),
            PlanStep(
                name="diagnose_llm",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={},
            ),
        ]
        return Plan(
            skill_id=skill,
            summary_fa="جمع‌آوری شواهد (سلامت، متریک، لاگ) و تحلیل علت محتمل.",
            steps=steps,
        )

    if skill == "liara.logs":
        steps = [
            PlanStep(
                name="tail_logs",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={"app": app_name, "lines": 120},
            ),
        ]
        return Plan(skill_id=skill, summary_fa="دریافت لاگ‌های اخیر سرویس.", steps=steps)

    if skill == "liara.deploy":
        steps = [
            PlanStep(
                name="auth_check",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={},
            ),
            PlanStep(
                name="deploy",
                operation=OperationType.deploy.value,
                risk=classify_step(OperationType.deploy, target_env=target_env),
                params={"app": app_name, "port": 3000, "path": "."},
            ),
            PlanStep(
                name="verify_health",
                operation=OperationType.read.value,
                risk=classify_step(OperationType.read),
                params={"url": url},
            ),
        ]
        return Plan(
            skill_id=skill,
            summary_fa=f"دیپلوی نسخه جدید به محیط {target_env} و بررسی سلامت.",
            steps=steps,
        )

    steps = [
        PlanStep(
            name="health_check",
            operation=OperationType.read.value,
            risk=classify_step(OperationType.read),
            params={"url": url},
        ),
    ]
    return Plan(skill_id="liara.health", summary_fa="بررسی سلامت سرویس.", steps=steps)


def _stack_hint(message: str) -> str:
    m = message.lower()
    if "next" in m or "react" in m:
        return "node"
    if "static" in m or "html" in m:
        return "static"
    if "docker" in m:
        return "docker"
    return "node"


def _recommend_infra(message: str) -> str:
    hint = _stack_hint(message)
    if hint == "static":
        return "پیشنهاد: پلن پایه Static/Node کوچک؛ در صورت ترافیک بالا پلن را ارتقا دهید."
    if hint == "docker":
        return "پیشنهاد: پلن با RAM بیشتر برای ایمیج Docker؛ دیسک جدا برای داده پایدار."
    return "پیشنهاد: پلن Node استاندارد؛ پورت 3000؛ برای Next.js SSG زمان build را در نظر بگیرید."


def plan_max_risk(plan: Plan):
    return max_risk([s.risk for s in plan.steps])
