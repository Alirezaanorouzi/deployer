"""Deterministic setup / plan / domain guidance (no writes)."""

from __future__ import annotations

import re
from typing import Any

from app.agent.language import prefers_persian


def infer_stack_profile(text: str) -> dict[str, Any]:
    """Best-effort stack from user message or prepare_deploy_workspace analysis."""
    t = (text or "").lower()
    profile: dict[str, Any] = {
        "stack": "node",
        "platform": "node",
        "port": 3000,
        "traffic": "low",
        "needs_disk": False,
        "needs_database": False,
        "static_site": False,
    }
    if any(x in t for x in ("next.js", "nextjs", "next ", "نکست")):
        profile.update(stack="next", platform="next", port=3000)
    elif any(x in t for x in ("django", "fastapi", "flask", "python", "پایتون")):
        profile.update(stack="python", platform="python", port=8000)
    elif any(x in t for x in ("static", "html only", "فقط html")):
        profile.update(stack="static", platform="static", port=80, static_site=True)
    elif "docker" in t or "ایمیج" in t:
        profile.update(stack="docker", platform="docker", port=8080)
    elif "wordpress" in t or "وردپرس" in t:
        profile.update(stack="wordpress", platform="wordpress", port=80)
    elif "react" in t and "next" not in t:
        profile.update(stack="react", platform="react", port=3000)
    if any(x in t for x in ("زیاد", "high traffic", "پرترافیک", "scale", "هزاران")):
        profile["traffic"] = "high"
    elif any(x in t for x in ("متوسط", "medium", "میان")):
        profile["traffic"] = "medium"
    if any(x in t for x in ("disk", "دیسک", "upload", "فایل", "storage", "ذخیره")):
        profile["needs_disk"] = True
    if any(x in t for x in ("postgres", "mysql", "mongodb", "database", "دیتابیس", "db ")):
        profile["needs_database"] = True
    return profile


def merge_analysis_profile(profile: dict[str, Any], analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not analysis:
        return profile
    out = dict(profile)
    stack = str(analysis.get("stack") or "").lower()
    if stack and stack != "unknown":
        out["stack"] = stack
        if stack == "python":
            out["platform"] = "python"
            out["port"] = int(analysis.get("deploy_port") or 8000)
        elif stack == "next":
            out["platform"] = "next"
            out["port"] = int(analysis.get("deploy_port") or 3000)
        else:
            out["platform"] = stack if stack in ("node", "react", "static", "docker") else "node"
            out["port"] = int(analysis.get("deploy_port") or 3000)
    findings = analysis.get("findings") or []
    if isinstance(findings, list):
        codes = {f.get("code") for f in findings if isinstance(f, dict)}
        if "NO_LIARA_JSON" in codes:
            out["missing_liara_json"] = True
    return out


def pick_plan_id(plans: list[dict[str, Any]], profile: dict[str, Any]) -> str | None:
    if not plans:
        return None
    traffic = profile.get("traffic") or "low"
    ram_target = 0.5
    if profile.get("stack") == "python" and profile.get("needs_database"):
        ram_target = 2.0
    elif profile.get("stack") == "next":
        ram_target = 1.0 if traffic == "low" else 2.0
    elif traffic == "high":
        ram_target = 4.0
    elif traffic == "medium":
        ram_target = 2.0

    best_id: str | None = None
    best_ram = -1.0
    for p in plans:
        pid = p.get("plan_id")
        if not pid:
            continue
        ram_raw = p.get("ram_gb")
        try:
            ram = float(ram_raw) if ram_raw is not None else 0.5
        except (TypeError, ValueError):
            ram = 0.5
        if ram >= ram_target and (best_id is None or ram < best_ram or best_ram < ram_target):
            best_id = str(pid)
            best_ram = ram
    if best_id:
        return best_id
    first = plans[0].get("plan_id")
    return str(first) if first else None


def deployment_method_lines(profile: dict[str, Any], *, persian: bool) -> list[str]:
    platform = profile.get("platform") or "node"
    repo = profile.get("repo")
    if persian:
        lines = ["### روش استقرار پیشنهادی"]
        if repo:
            lines.append(
                f"- **GitHub → Liara (این دستیار):** کلون `{repo}`، تحلیل پروژه، نوشتن `liara.json`، "
                "ساخت اپ (در صورت نیاز)، سپس **Deploy / استقرار** از CLI/API."
            )
        else:
            lines.append(
                "- **GitHub → Liara (این دستیار):** اگر ریپوی `owner/name` بدهید، همان مسیر خودکار "
                "(clone → prepare → create_app → deploy_service) را پیشنهاد می‌کنیم."
            )
        lines.append(
            "- **Liara CLI محلی:** `liara deploy` از پوشه پروژه بعد از `liara.json` و `liara login`."
        )
        lines.append(
            "- **اتصال Git در پنل لیارا:** برای استقرار خودکار روی push (خارج از این agent مگر یکپارچه‌سازی Git API)."
        )
        if platform == "next":
            lines.append(
                "- برای **Next.js** پلتفرم `next` و زمان build را در پلن در نظر بگیرید؛ خروجی standalone یا `npm run build` + `start`."
            )
        elif platform == "python":
            lines.append(
                "- برای **Python** پلتفرم `python`، پورت از `PORT` لیارا (معمولاً 8000) و دستور `python main.py` یا uvicorn."
            )
        return lines

    lines = ["### Recommended deployment path"]
    if repo:
        lines.append(
            f"- **GitHub via this agent:** clone `{repo}`, analyze, write `liara.json`, "
            "create app if needed, then **deploy_service** (Liara Deploy tab)."
        )
    else:
        lines.append(
            "- **GitHub via this agent:** send `owner/repo` for clone → prepare → create → deploy."
        )
    lines.append("- **Local Liara CLI:** `liara deploy` from project root with `liara.json`.")
    lines.append("- **Liara Git integration:** auto deploy on push (console / Git API).")
    return lines


def plan_recommendation_lines(
    profile: dict[str, Any],
    plans: list[dict[str, Any]],
    suggested_plan: str | None,
    *,
    persian: bool,
) -> list[str]:
    traffic = profile.get("traffic") or "low"
    if persian:
        lines = ["### پیشنهاد پلن و منابع"]
        if suggested_plan:
            lines.append(
                f"- پلن پیشنهادی برای شروع: **`{suggested_plan}`** "
                f"(بر اساس پلتفرم **{profile.get('platform')}** و ترافیک **{traffic}**)."
            )
        else:
            lines.append(
                "- پلن: از `liara.list_available_plans` یک `plan_id` کوچک برای شروع انتخاب کنید؛ "
                "بعد از دیپلوی با `get_service_plan_info` وضعیت فعلی را ببینید."
            )
        if profile.get("needs_disk"):
            lines.append("- **دیسک پایدار:** برای آپلود/لاگ/فایل، دیسک جدا (`create_app_disk`) و mount در `liara.json`.")
        if profile.get("needs_database"):
            lines.append(
                "- **دیتابیس:** سرویس **DBaaS** لیارا (API جدا) یا دیتابیس خارجی؛ "
                "اتصال با env در تنظیمات اپ."
            )
        if traffic == "high":
            lines.append("- ترافیک بالا: scale/replica یا ارتقای پلن بعد از مانیتورینگ CPU/RAM.")
        return lines

    lines = ["### Plan & resources"]
    if suggested_plan:
        lines.append(f"- Suggested starter plan: **`{suggested_plan}`** ({profile.get('platform')}, traffic {traffic}).")
    else:
        lines.append("- Pick a small `plan_id` from list_available_plans; confirm with get_service_plan_info after deploy.")
    if profile.get("needs_disk"):
        lines.append("- Add a persistent disk for uploads/logs if the app writes local files.")
    if profile.get("needs_database"):
        lines.append("- Use Liara DBaaS or an external DB; configure via env vars.")
    return lines


def setup_checklist_lines(profile: dict[str, Any], *, persian: bool) -> list[str]:
    if persian:
        return [
            "### مراحل بعدی (حداقل کار دستی)",
            "1. توکن API لیارا در `.env` سرور agent (`LIARA_API_TOKEN`) و در صورت تیم `LIARA_TEAM_ID`.",
            "2. نام اپ (slug) و در صورت GitHub، `owner/repo`.",
            "3. تأیید صریح قبل از **ساخت اپ**، **دیپلوی**، **دامنه**، **پلن** یا **ریستارت**.",
            "4. بعد از دیپلوی: `verify_app_health` و در صورت 502 → `diagnose_deploy_health`.",
            "5. envهای production (مثلاً `DATABASE_URL`, `LLM_*`) در تنظیمات اپ.",
        ]
    return [
        "### Next steps (minimal manual work)",
        "1. Configure LIARA_API_TOKEN (and LIARA_TEAM_ID if needed).",
        "2. Choose app slug and optional GitHub `owner/repo`.",
        "3. Explicit approval before create_app, deploy, domain, plan change, or restart.",
        "4. After deploy: verify_app_health; on 502 use diagnose_deploy_health.",
        "5. Set production env vars in app settings.",
    ]


def build_setup_recommendation(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    user_goal = str(params.get("user_goal") or "")
    persian = prefers_persian(user_goal)
    profile = infer_stack_profile(user_goal)
    profile = merge_analysis_profile(profile, params.get("analysis") if isinstance(params.get("analysis"), dict) else None)
    if params.get("platform"):
        profile["platform"] = params["platform"]
    if params.get("stack"):
        profile["stack"] = params["stack"]
    if params.get("port"):
        profile["port"] = params["port"]
    if params.get("repo"):
        profile["repo"] = params["repo"]

    plans = params.get("plans") if isinstance(params.get("plans"), list) else []
    suggested = pick_plan_id(plans, profile)

    parts: list[str] = []
    if persian:
        parts.append("## راه‌اندازی اولیه روی لیارا")
        parts.append(
            f"پروفایل تشخیص‌داده‌شده: پلتفرم **{profile.get('platform')}**، پورت پیشنهادی **{profile.get('port')}**."
        )
    else:
        parts.append("## Initial Liara setup")
        parts.append(
            f"Detected profile: platform **{profile.get('platform')}**, suggested port **{profile.get('port')}**."
        )
    parts.extend(deployment_method_lines(profile, persian=persian))
    parts.extend(plan_recommendation_lines(profile, plans, suggested, persian=persian))
    parts.extend(setup_checklist_lines(profile, persian=persian))

    data = {
        "profile": profile,
        "suggested_plan_id": suggested,
        "plans_considered": len(plans),
    }
    return "\n\n".join(parts), data


def is_domain_guide_only(user_message: str, conversation_history: list[dict[str, str]] | None = None) -> bool:
    """User wants IRNIC/DNS steps without attaching a domain now."""
    from app.agent.conversation_params import conversation_text

    combined = conversation_text(user_message, conversation_history).lower()
    guide_markers = (
        "irnic",
        "آی‌آر نیک",
        "ثبت‌کننده",
        "ثبت کننده",
        "dns",
        "name server",
        "نیم سرور",
        "nameserver",
        "چه کار",
        "چیکار",
        "راهنما",
        "guide",
        "how to connect",
        "چطور وصل",
        "اتصال دامنه",
    )
    write_markers = (
        "اضافه کن",
        "بساز",
        "add domain",
        "attach",
        "create domain",
        "ست کن",
        "تأیید می‌کنم",
        "تایید می‌کنم",
        "الان دامنه را اضافه",
    )
    if not any(m in combined for m in guide_markers):
        return False
    if any(m in combined for m in write_markers):
        return False
    if re.search(r"الان\s+دامنه", combined) and "اضافه" in combined:
        return False
    return True


def is_bootstrap_intent(user_message: str) -> bool:
    m = (user_message or "").lower()
    markers = (
        "bootstrap",
        "initial setup",
        "first deploy",
        "first time",
        "راه‌اندازی",
        "راه اندازی",
        "شروع",
        "اولین",
        "setup",
        "استقرار اول",
        "چطور deploy",
        "how to deploy",
        "روش استقرار",
        "پلن مناسب",
        "which plan",
        "what plan",
        "recommend plan",
        "پیشنهاد پلن",
        "next.js",
        "nextjs",
        "سایت next",
    )
    deploy_now = any(x in m for x in ("deploy now", "دیپلوی کن", "منتشر کن", "برو live", "go live"))
    if deploy_now:
        return False
    return any(x in m for x in markers)


def build_domain_connect_playbook(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    user_goal = str(params.get("user_goal") or "")
    persian = prefers_persian(user_goal)
    app = (params.get("app_name") or params.get("project_id") or "my-app").strip()
    domain = (params.get("domain_name") or "").strip().lower()
    default_url = params.get("default_url") or f"https://{app}.liara.run"
    existing = params.get("domains") if isinstance(params.get("domains"), list) else []
    tld_ir = domain.endswith(".ir") if domain else "ir" in user_goal.lower() or "irnic" in user_goal.lower()

    if persian:
        parts = [
            "## اتصال دامنه به لیارا",
            f"اپ: **{app}** — آدرس پیش‌فرض: {default_url}",
        ]
        if existing:
            names = [d.get("name") for d in existing if isinstance(d, dict) and d.get("name")]
            if names:
                parts.append("دامنه‌های فعلی روی اپ: " + ", ".join(f"`{n}`" for n in names))
            else:
                parts.append("فعلاً **دامنه سفارشی** روی این اپ ثبت نشده است.")
        else:
            parts.append("فعلاً دامنه سفارشی روی اپ دیده نشد (یا هنوز لیست نشده).")

        parts.append("### کارهایی که **شما** در ثبت‌کننده (مثلاً IRNIC) انجام می‌دهید")
        if tld_ir:
            parts.extend(
                [
                    "1. ورود به [IRNIC](https://www.nic.ir) و انتخاب دامنه `.ir`.",
                    "2. در بخش DNS / Name Server، طبق **راهنمای اتصال دامنه در پنل لیارا** برای اپ خود رکوردها را تنظیم کنید.",
                    "3. اگر لیارا **CNAME** یا **A** مشخص کرد، همان را در IRNIC ثبت کنید (مقدار را از پنل لیارا → دامنه‌ها کپی کنید، نه حدس).",
                    "4. انتشار DNS ممکن است چند دقیقه تا چند ساعت طول بکشد.",
                ]
            )
        else:
            parts.extend(
                [
                    "1. در پنل ثبت‌کننده دامنه (GoDaddy, Cloudflare, …) رکوردهای DNS را طبق راهنمای لیارا تنظیم کنید.",
                    "2. برای `.ir` معمولاً IRNIC؛ برای سایر TLDها همان پنل ثبت‌کننده.",
                ]
            )
        parts.append("### کارهایی که **این agent** می‌تواند انجام دهد")
        parts.extend(
            [
                "- `liara.list_app_domains` — وضعیت SSL و دامنه‌های متصل.",
                "- `liara.check_domain_status` — بررسی وضعیت تأیید DNS/SSL پس از ثبت دامنه در لیارا.",
                "- پس از **تأیید شما**: `liara.create_app_domain` برای افزودن دامنه.",
                "- `liara.set_domain_redirect` برای ریدایرکت 301/302.",
            ]
        )
        if params.get("defer_attach"):
            parts.append("\nطبق درخواست شما، **الان دامنه اضافه نمی‌شود** — فقط راهنما.")
    else:
        parts = [
            "## Connect a custom domain",
            f"App: **{app}** — default URL: {default_url}",
            "### Your steps (registrar / IRNIC)",
        ]
        if tld_ir:
            parts.append("Configure DNS at IRNIC per Liara's domain docs; copy CNAME/A from Liara console.")
        else:
            parts.append("Set DNS records at your registrar per Liara domain setup guide.")
        parts.append("### Agent can: list domains, check domain status, add domain after approval, set redirect.")

    return "\n\n".join(parts), {"app_name": app, "domain_name": domain or None, "tld_ir": tld_ir}
