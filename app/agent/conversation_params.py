"""Multi-turn chat → planner_params (all ReAct skills)."""

import re
from typing import Any

from app.agent.deploy_params import enrich_deploy_params

AFFIRM_PHRASES = (
    "yes,",
    "yes ",
    "confirm",
    "approved",
    "go ahead",
    "do it",
    "please do",
    "تایید",
    "تأیید",
    "بله",
    "انجام بده",
    "اوکی",
    "بساز",
    "بزن",
    "تایید می‌کنم",
    "تأیید می‌کنم",
    "ساخت",
    "بساز خب",
    "خب بساز",
    "اوکیه",
    "باشه",
    "حله",
)


def persian_digits_to_ascii(text: str) -> str:
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))


_ADVISORY_FOLLOWUP_MARKERS = (
    "چیکار کنم",
    "چکار کنم",
    "چه کار کنم",
    "چکار کن",
    "چیکار کن",
    "الان چ",
    "خب الان",
    "حالا چ",
    "what should i",
    "what now",
    "now what",
    "next step",
    "what do i do",
    "how do i fix",
    "علیکار",
    "راهنمایی",
    "کمک کن",
)


def is_advisory_followup(user_message: str, history: list[dict[str, str]] | None) -> bool:
    """Short 'what should I do now?' after a prior turn — not a repeat of the old deploy request."""
    if not history:
        return False
    m = (user_message or "").strip().lower()
    if len(m) > 96:
        return False
    return any(p in m or p in (user_message or "") for p in _ADVISORY_FOLLOWUP_MARKERS)


def conversation_text(user_message: str, history: list[dict[str, str]] | None) -> str:
    """Legacy: full transcript string. Prefer text_for_param_inference for slot filling."""
    return text_for_param_inference(user_message, history)


def text_for_param_inference(user_message: str, history: list[dict[str, str]] | None) -> str:
    """Regex/slot inference: current message; plus only the last prior user line for short continuations."""
    msg = (user_message or "").strip()
    if not history:
        return msg
    if looks_like_independent_turn(msg):
        return msg
    if message_affirms_write(msg) or len(msg) <= 36:
        for item in reversed(history):
            if (item.get("role") or "").strip() == "user":
                prev = (item.get("content") or "").strip()
                if prev and prev != msg:
                    return prev + "\n" + msg
                break
    return msg


_INDEPENDENT_TURN_MARKERS = (
    "?",
    "؟",
    "ببین",
    "چک کن",
    "چک ",
    "check ",
    "show ",
    "list ",
    "آیا",
    "چند",
    "چنتا",
    "how many",
    "تنظیم",
    "ست شده",
    "نشده",
    "نبود",
    "وجود",
    "github",
    "گیت",
    "git ",
    "توکن",
    "token",
    "دیسک",
    "disk",
    "لاگ",
    "log",
    "deploy",
    "دیپلوی",
    "domain",
    "دامنه",
    "restart",
    "ریستارت",
    "balance",
    "موجودی",
    "متغیر",
    "env ",
    "environment",
)


def looks_like_independent_turn(user_message: str) -> bool:
    """New question or task in chat — do not merge with the previous user turn."""
    raw = (user_message or "").strip()
    if not raw:
        return False
    if message_affirms_write(raw) and len(raw) <= 56:
        return False
    low = raw.lower()
    if "?" in raw or "؟" in raw:
        return True
    return any(m in low or m in raw for m in _INDEPENDENT_TURN_MARKERS)


def compose_user_goal(user_message: str, history: list[dict[str, str]] | None) -> str:
    """ReAct user_goal is always the current message; history is passed separately to the LLM."""
    from app.agent.context_builder import current_user_goal

    return current_user_goal(user_message)


def message_affirms_write(text: str) -> bool:
    m = (text or "").lower().strip()
    deploy_intent = any(x in m for x in ("deploy", "publish", "release", "دیپلوی", "منتشر"))
    if deploy_intent:
        return False
    explicit = (
        "تأیید می‌کنم",
        "تایید می‌کنم",
        "confirm",
        "approved",
        "go ahead",
        "do it",
        "yes,",
        "yes ",
    )
    if any(e in m for e in explicit):
        return True
    # Short approval follow-ups only — not "یک دیسک بساز" in the same sentence.
    if len(m) > 48:
        return False
    return any(a in m for a in AFFIRM_PHRASES)


def user_approved_write(user_message: str, history: list[dict[str, str]] | None) -> bool:
    for item in (history or []) + [{"role": "user", "content": user_message}]:
        if (item.get("role") or "").strip() != "user":
            continue
        if message_affirms_write(item.get("content") or ""):
            return True
    return False


def apply_confirm_from_conversation(
    user_message: str,
    params: dict[str, Any],
    *,
    conversation_history: list[dict[str, str]] | None = None,
) -> None:
    if message_affirms_write(user_message):
        params["confirm_write"] = True
        return
    if user_approved_write(user_message, conversation_history):
        params["confirm_write"] = True


def infer_app_name(text: str, liara_projects: list[str] | None) -> str | None:
    m = re.search(
        r"\b(?:app|اپ(?:لیکیشن)?|service)\s+[`'\"]?([a-z0-9][a-z0-9-]*)[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1).lower()
    m = re.search(r"اپ\s+[`'\"]?([a-z0-9][a-z0-9-]*)[`'\"]?", text)
    if m:
        return m.group(1).lower()
    for proj in liara_projects or []:
        if proj and proj.lower() in text.lower():
            return proj
    return None


def infer_since_hours(text: str) -> float | None:
    t = persian_digits_to_ascii(text.lower())
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hour|hours|ساعت)", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:last|past|اخیر|گذشته)\s+(\d+)\s*(?:h|hour|ساعت)?", t)
    if m:
        return float(m.group(1))
    if "۲۴" in text or "24" in t:
        return 24.0
    if "۶" in text or re.search(r"\b6\s*ساعت", t):
        return 6.0
    return None


def infer_log_lines(text: str) -> int | None:
    t = persian_digits_to_ascii(text.lower())
    m = re.search(r"(\d+)\s*(?:line|lines|خط)", t)
    if m:
        n = int(m.group(1))
        if 10 <= n <= 400:
            return n
    return None


def infer_domain_name(text: str) -> str | None:
    for m in re.finditer(
        r"`([a-z0-9](?:[a-z0-9-]*\.)+[a-z]{2,})`",
        text,
        re.I,
    ):
        return m.group(1).lower()
    m = re.search(
        r"\b([a-z0-9](?:[a-z0-9-]*\.)+(?:com|ir|net|org|io|dev|app|run))\b",
        text,
        re.I,
    )
    if m:
        return m.group(1).lower()
    return None


def infer_env_update(text: str) -> tuple[str, str] | None:
    m = re.search(
        r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]{0,64})\s*=\s*([^\s`,;]+)",
        text,
    )
    if m:
        return m.group(1), m.group(2)
    m = re.search(
        r"(?:env|متغیر)\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s+(?:to|به|برابر|رو|را)\s+[`'\"]?([^`\"'\n]+)[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1), m.group(2).strip()
    m = re.search(
        r"(?:name|نام)(?:\s+of)?\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s+(?:and\s+)?(?:value|مقدار)(?:\s+is|\s+برابر)?\s+[`'\"]?([^`\"'\n]+)[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1), m.group(2).strip()
    m = re.search(
        r"\bset\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s+to\s+[`'\"]?([^`\"'\n]+)[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1), m.group(2).strip()
    m = re.search(
        r"(?:with\s+)?(?:sample\s+)?name\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s+(?:and\s+)?(?:sample\s+)?value\s+[`'\"]?([^`\"'\n]+)[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1), m.group(2).strip()
    m = re.search(
        r"(?:نام|name)\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?\s+و\s+مقدار\s+[`'\"]?([^`\"'\n]+)[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1), m.group(2).strip()
    m = re.search(
        r"به\s+نام\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?",
        text,
        re.I,
    )
    if m:
        key = m.group(1)
        m2 = re.search(
            r"مقدار(?:ش)?\s+(?:هم\s+)?(?:اینو\s+)?(?:بزار|بذار|بگذار|برابر|ست\s+کن?)\s+[`'\"]?([^\s`'\"،]+)[`'\"]?",
            text,
            re.I,
        )
        if m2:
            return key, m2.group(1).strip()
        m2 = re.search(
            r"مقدار(?:ش)?\s+[`'\"]?([^\s`'\"،]+)[`'\"]?",
            text,
            re.I,
        )
        if m2:
            return key, m2.group(1).strip()
    return None


def infer_env_key_name(text: str) -> str | None:
    """Env var name mentioned without a parseable value (e.g. LIARA_TEAM_ID)."""
    pair = infer_env_update(text)
    if pair:
        return pair[0]
    m = re.search(r"\b([A-Z][A-Z0-9_]{2,})\b", text or "")
    if m:
        return m.group(1)
    m = re.search(
        r"به\s+نام\s+[`'\"]?([A-Za-z_][A-Za-z0-9_]*)[`'\"]?",
        text or "",
        re.I,
    )
    if m:
        return m.group(1)
    return None


def settings_env_upsert_intent(text: str) -> bool:
    """User wants to check an env var and create/set it if missing."""
    raw = text or ""
    t = raw.lower()
    if not infer_env_key_name(raw):
        return False
    upsert_markers = (
        "اگر نه",
        "اگر نبود",
        "اگر نداشت",
        "if not",
        "if missing",
        "not set",
        "ست نشده",
        "تنظیم نشده",
        "وجود نداشت",
        "ندارد",
        "بساز",
        "ایجاد",
        "create",
        "ساخته",
        "مقدارش",
        "مقدار",
    )
    return any(x in raw or x in t for x in upsert_markers)


def is_settings_read(text: str) -> bool:
    """User wants to inspect settings, not change them."""
    raw = text or ""
    t = raw.lower()
    if settings_env_upsert_intent(raw):
        return False
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*=", raw):
        return False
    if any(x in t for x in ("ست کن", "تنظیم کن", "قرار بده", "update env", "set env", "set ")):
        return False
    if any(x in raw or x in t for x in ("بساز", "ایجاد", "create", "مقدارش")) and infer_env_key_name(raw):
        return False
    return any(
        x in t
        for x in (
            "ببین",
            "چک",
            "نشون",
            "نشان",
            "نمایش",
            "لیست",
            "list",
            "show",
            "check",
            "ست نشده",
            "نشده",
            "وضعیت",
            "بگو",
        )
    )


def env_write_intent(planner_params: dict[str, Any], combined: str) -> bool:
    if planner_params.get("env_key") or planner_params.get("variables"):
        return True
    if infer_env_update(combined):
        return True
    if settings_env_upsert_intent(combined):
        return True
    if is_settings_read(combined):
        return False
    t = combined.lower()
    if any(
        x in t
        for x in (
            "env var",
            "env ",
            "environment variable",
            "environment",
            "متغیر محیط",
            "متغیر",
            "set env",
            "تنظیم env",
            "update env",
            "مقداردهی",
        )
    ):
        return True
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*\s*=", combined):
        return True
    if re.search(r"\b(?:name|نام)\b.*\b(?:value|مقدار)\b", combined, re.I):
        return True
    return False


def apply_inferred_env(planner_params: dict[str, Any], combined: str) -> None:
    if planner_params.get("env_key"):
        return
    pair = infer_env_update(combined)
    if pair:
        planner_params["env_key"], planner_params["env_value"] = pair


def infer_scale(text: str) -> int | None:
    t = persian_digits_to_ascii(text.lower())
    m = re.search(r"(?:scale|replica|ریپلیکا|مقیاس)\s*[:=]?\s*(\d+)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\s*(?:instance|instances|نمونه)\b", t)
    if m:
        return int(m.group(1))
    if re.search(r"\b(stop|متوقف|خاموش)\b", t):
        return 0
    return None


def infer_plan_id(text: str) -> str | None:
    m = re.search(r"\b([a-z]+-g\d+|small|medium|large|c\d+-g\d+)\b", text.lower())
    if m:
        return m.group(1)
    m = re.search(r"plan[_\s-]?id\s*[:=]?\s*[`'\"]?([a-z0-9-]+)[`'\"]?", text, re.I)
    if m:
        return m.group(1).lower()
    return None


def infer_zero_downtime_enabled(text: str) -> bool | None:
    t = text.lower()
    if any(x in t for x in ("disable zero", "بدون zero", "غیرفعال", "خاموش کن zero")):
        return False
    if any(x in t for x in ("zero downtime", "zero-downtime", "بدون قطعی", "زیرو داون")):
        return True
    return None


def enrich_common_params(
    user_message: str,
    params: dict[str, Any],
    *,
    conversation_history: list[dict[str, str]] | None = None,
    liara_projects: list[str] | None = None,
    default_app: str | None = None,
) -> None:
    combined = text_for_param_inference(user_message, conversation_history)
    if not params.get("app_name"):
        app = (
            infer_app_name(combined, liara_projects)
            or params.get("liara_app_name")
            or default_app
        )
        if not app and liara_projects and len(liara_projects) == 1:
            mine = ("پروژه ام", "پروژه‌ام", "پروژه‌ من", "my project", "my app")
            if any(x in combined for x in mine):
                app = liara_projects[0]
        if app:
            params["app_name"] = app
    if params.get("since_hours") is None:
        hours = infer_since_hours(combined) or infer_since_hours(user_message)
        if hours is not None:
            params["since_hours"] = hours
    if params.get("lines") is None:
        lines = infer_log_lines(combined)
        if lines is not None:
            params["lines"] = lines
    if not params.get("domain_name"):
        dom = infer_domain_name(combined)
        if dom:
            params["domain_name"] = dom
    apply_inferred_env(params, combined)
    if params.get("scale") is None:
        scale = infer_scale(combined)
        if scale is not None:
            params["scale"] = scale
    if not params.get("plan_id"):
        plan = infer_plan_id(combined)
        if plan:
            params["plan_id"] = plan
    if params.get("enabled") is None:
        zdt = infer_zero_downtime_enabled(combined)
        if zdt is not None:
            params["enabled"] = zdt


def enrich_planner_params(
    skill_id: str,
    user_message: str,
    params: dict[str, Any],
    *,
    conversation_history: list[dict[str, str]] | None = None,
    liara_projects: list[str] | None = None,
    github_repos: list[str] | None = None,
    default_app: str | None = None,
) -> None:
    enrich_common_params(
        user_message,
        params,
        conversation_history=conversation_history,
        liara_projects=liara_projects,
        default_app=default_app,
    )
    if skill_id in ("liara.deploy", "pipeline.github_to_liara", "liara.bootstrap"):
        enrich_deploy_params(user_message, params, github_repos)
        enrich_deploy_params(
            text_for_param_inference(user_message, conversation_history),
            params,
            github_repos,
        )
    if skill_id == "liara.disks":
        from app.agent.disk_params import enrich_disk_params

        enrich_disk_params(
            user_message,
            params,
            conversation_history=conversation_history,
            liara_projects=liara_projects,
        )
    apply_confirm_from_conversation(
        user_message,
        params,
        conversation_history=conversation_history,
    )
