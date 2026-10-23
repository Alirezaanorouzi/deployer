"""Deterministic tool chains per skill (multi-turn safe)."""

from typing import Any

from app.agent.conversation_params import (
    apply_inferred_env,
    conversation_text,
    env_write_intent,
    enrich_planner_params,
    infer_env_key_name,
    is_settings_read,
    settings_env_upsert_intent,
)
from app.agent.disk_params import enrich_disk_params
from app.agent.setup_guide import is_domain_guide_only

Obs = list[dict[str, Any]]


def last_obs_for_tool(observations: Obs, tool_id: str) -> dict[str, Any] | None:
    for obs in reversed(observations):
        if obs.get("tool") == tool_id:
            return obs
    return None


def app_name_from(planner_params: dict[str, Any]) -> str | None:
    return (
        planner_params.get("app_name")
        or planner_params.get("liara_app_name")
        or planner_params.get("app")
    )


def base_params(planner_params: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    app = app_name_from(planner_params)
    if app:
        out["app_name"] = app
    if planner_params.get("project_id"):
        out["project_id"] = planner_params["project_id"]
    if confirm and planner_params.get("confirm_write"):
        out["confirm_write"] = True
    return out


def copy_keys(planner_params: dict[str, Any], *keys: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        if planner_params.get(key) is not None:
            out[key] = planner_params[key]
    return out


READ_PRIMARY_TOOL: dict[str, str] = {
    "liara.balance": "liara.get_account_balance",
    "liara.usage": "liara.get_service_resource_usage",
    "liara.logs": "liara.analyze_service_logs",
    "liara.events": "liara.list_app_events",
    "liara.history": "liara.list_deploy_releases",
}

READ_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "liara.usage": ("since_hours",),
    "liara.logs": ("since_hours", "since_ts", "until_ts", "since_iso", "until_iso", "lines", "focus"),
    "liara.events": ("since_hours", "start_ts", "end_ts", "search"),
    "liara.history": ("page", "count"),
}


def _call(tool_id: str, tool_params: dict[str, Any]) -> dict[str, Any]:
    return {"action": "call_tool", "tool_id": tool_id, "tool_params": tool_params, "message": ""}


def _clarify(message: str) -> dict[str, Any]:
    return {"action": "clarify", "message": message}


def _forced_read_action(skill_id: str, observations: Obs, planner_params: dict[str, Any]) -> dict[str, Any] | None:
    tool_id = READ_PRIMARY_TOOL.get(skill_id)
    if not tool_id:
        return None
    last = last_obs_for_tool(observations, tool_id)
    if last and last.get("ok"):
        return None
    if tool_id != "liara.get_account_balance" and not app_name_from(planner_params):
        return _clarify("نام اپ لیارا (app_name) را بفرستید، مثلاً `new1`.")
    if last is not None and not last.get("ok"):
        return None
    params = base_params(planner_params)
    params.update(copy_keys(planner_params, *READ_PARAM_KEYS.get(skill_id, ())))
    return _call(tool_id, params)


# --- disks (create) ---


def disk_create_done(observations: Obs) -> bool:
    create = last_obs_for_tool(observations, "liara.create_app_disk")
    return create is not None and bool(create.get("ok"))


def disk_create_intent(planner_params: dict[str, Any], user_goal: str) -> bool:
    if planner_params.get("disk_name") and (
        planner_params.get("size_gb") is not None or planner_params.get("confirm_write")
    ):
        return True
    g = (user_goal or "").lower()
    create = any(x in g for x in ("بساز", "create", "ساخت", "اضافه"))
    disk = any(x in g for x in ("دیسک", "disk", "volume"))
    return create and disk


def forced_disk_action(
    observations: Obs,
    planner_params: dict[str, Any],
    *,
    user_goal: str = "",
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    enrich_disk_params(
        user_goal or "",
        planner_params,
        conversation_history=conversation_history,
    )
    if not disk_create_intent(planner_params, user_goal):
        return _maybe_list_disks(observations, planner_params)
    if disk_create_done(observations):
        return None

    name = planner_params.get("disk_name")
    size = planner_params.get("size_gb") or planner_params.get("size")
    app = app_name_from(planner_params)
    confirm = planner_params.get("confirm_write")

    create_obs = last_obs_for_tool(observations, "liara.create_app_disk")
    if create_obs and not create_obs.get("ok"):
        code = create_obs.get("code") or ""
        if code not in ("CONFIRMATION_REQUIRED",):
            return None

    if name and size and app and confirm:
        p = base_params(planner_params, confirm=True)
        p.update(copy_keys(planner_params, "disk_name", "size_gb", "size", "project_id"))
        return _call("liara.create_app_disk", p)

    if not name or not size:
        missing = []
        if not name:
            missing.append("نام دیسک")
        if not size:
            missing.append("حجم (گیگابایت)")
        return _clarify("لطفاً " + " و ".join(missing) + " را بفرستید.")

    if not confirm:
        return _clarify(
            f"ایجاد دیسک `{name}` ({size} GB) برای `{app}` هزینه دارد. بنویسید: تأیید می‌کنم یا بساز."
        )
    return _maybe_list_disks(observations, planner_params)


def _maybe_list_disks(observations: Obs, planner_params: dict[str, Any]) -> dict[str, Any] | None:
    if not app_name_from(planner_params):
        return None
    if last_obs_for_tool(observations, "liara.list_app_disks"):
        return None
    return _call("liara.list_app_disks", base_params(planner_params))


# --- domains ---


def domain_write_intent(planner_params: dict[str, Any], combined: str) -> str | None:
    t = combined.lower()
    if planner_params.get("redirect_to") or "redirect" in t or "ریدایرکت" in t:
        return "liara.set_domain_redirect"
    if planner_params.get("domain_name") and any(
        x in t for x in ("add domain", "attach", "custom domain", "دامنه", "اضافه", "بساز", "connect")
    ):
        return "liara.create_app_domain"
    if planner_params.get("domain_name") and planner_params.get("confirm_write"):
        return "liara.create_app_domain"
    return None


def domain_write_done(observations: Obs, tool_id: str) -> bool:
    last = last_obs_for_tool(observations, tool_id)
    return last is not None and bool(last.get("ok"))


def _obs_data(observations: Obs, tool_id: str) -> dict[str, Any] | None:
    last = last_obs_for_tool(observations, tool_id)
    if not last or not last.get("ok"):
        return None
    data = last.get("data")
    return data if isinstance(data, dict) else None


def _guide_domain_params(
    planner_params: dict[str, Any],
    observations: Obs,
    *,
    user_goal: str,
    defer_attach: bool = False,
) -> dict[str, Any]:
    app = app_name_from(planner_params)
    dom_data = _obs_data(observations, "liara.list_app_domains")
    domains = (dom_data or {}).get("domains") if dom_data else None
    return {
        "user_goal": user_goal,
        "app_name": app,
        "domain_name": planner_params.get("domain_name"),
        "default_url": planner_params.get("url") or (f"https://{app}.liara.run" if app else None),
        "domains": domains if isinstance(domains, list) else [],
        "defer_attach": defer_attach,
    }


def forced_domains_action(
    observations: Obs,
    planner_params: dict[str, Any],
    *,
    user_goal: str,
    conversation_history: list[dict[str, str]] | None,
    liara_projects: list[str] | None = None,
) -> dict[str, Any] | None:
    enrich_planner_params(
        "liara.domains",
        user_goal,
        planner_params,
        conversation_history=conversation_history,
        liara_projects=liara_projects,
    )
    combined = conversation_text(user_goal, conversation_history)
    if is_domain_guide_only(user_goal, conversation_history):
        _default_app_if_single(planner_params, liara_projects)
        if app_name_from(planner_params) and not last_obs_for_tool(observations, "liara.list_app_domains"):
            return _call("liara.list_app_domains", base_params(planner_params))
        if not last_obs_for_tool(observations, "guide.domain_connect_playbook"):
            return _call(
                "guide.domain_connect_playbook",
                _guide_domain_params(
                    planner_params,
                    observations,
                    user_goal=user_goal,
                    defer_attach=True,
                ),
            )
        return None
    write_tool = domain_write_intent(planner_params, combined)
    if write_tool:
        if domain_write_done(observations, write_tool):
            return None
        app = app_name_from(planner_params)
        if not app:
            return _clarify("برای دامنه، نام اپ لیارا را بگویید.")
        if write_tool == "liara.create_app_domain":
            dom = planner_params.get("domain_name")
            if not dom:
                return _clarify("نام دامنه (مثلاً shop.example.com) را بفرستید.")
            if not planner_params.get("confirm_write"):
                return _clarify(f"افزودن دامنه `{dom}` به `{app}` را تأیید کنید (بله / تأیید می‌کنم).")
            p = base_params(planner_params, confirm=True)
            p["domain_name"] = dom
            return _call(write_tool, p)
        if write_tool == "liara.set_domain_redirect":
            if not planner_params.get("confirm_write"):
                return _clarify("تنظیم ریدایرکت دامنه را تأیید کنید.")
            p = base_params(planner_params, confirm=True)
            p.update(copy_keys(planner_params, "domain_name", "domain_id", "redirect_to", "redirect_status"))
            if not p.get("redirect_to"):
                return _clarify("آدرس مقصد ریدایرکت (redirect_to) را بفرستید.")
            return _call(write_tool, p)
    if last_obs_for_tool(observations, "liara.list_app_domains"):
        return None
    if not app_name_from(planner_params):
        return _clarify("نام اپ برای لیست دامنه‌ها را بفرستید.")
    return _call("liara.list_app_domains", base_params(planner_params))


# --- settings ---


def settings_write_tool(planner_params: dict[str, Any], combined: str) -> str | None:
    if env_write_intent(planner_params, combined):
        return "liara.update_env_variables"
    t = combined.lower()
    if any(x in t for x in ("restart", "ریستارت", "ری‌استارت", "راه‌اندازی مجدد")):
        return "liara.restart_service"
    if planner_params.get("enabled") is not None or any(
        x in t for x in ("zero downtime", "zero-downtime", "بدون قطعی")
    ):
        return "liara.set_zero_downtime"
    return None


def _default_app_if_single(
    planner_params: dict[str, Any], liara_projects: list[str] | None
) -> None:
    if app_name_from(planner_params):
        return
    projects = [p for p in (liara_projects or []) if p]
    if len(projects) == 1:
        planner_params["app_name"] = projects[0]


def _env_keys_from_settings_obs(obs: dict[str, Any] | None) -> list[str]:
    if not obs or not obs.get("ok"):
        return []
    data = obs.get("data") or {}
    settings = data.get("settings") if isinstance(data, dict) else None
    if not isinstance(settings, dict):
        return []
    keys = settings.get("env_var_keys") or []
    return [k for k in keys if isinstance(k, str)]


def forced_settings_action(
    observations: Obs,
    planner_params: dict[str, Any],
    *,
    user_goal: str,
    conversation_history: list[dict[str, str]] | None,
    liara_projects: list[str] | None = None,
) -> dict[str, Any] | None:
    enrich_planner_params(
        "liara.settings",
        user_goal,
        planner_params,
        conversation_history=conversation_history,
        liara_projects=liara_projects,
    )
    combined = conversation_text(user_goal, conversation_history)
    apply_inferred_env(planner_params, combined)
    if not planner_params.get("env_key"):
        key = infer_env_key_name(combined)
        if key:
            planner_params["env_key"] = key
    read_only = is_settings_read(combined)
    if read_only:
        planner_params.pop("env_key", None)
        planner_params.pop("env_value", None)
        planner_params.pop("variables", None)
        write_tool = None
    else:
        write_tool = settings_write_tool(planner_params, combined)
    _default_app_if_single(planner_params, liara_projects)
    get_obs = last_obs_for_tool(observations, "liara.get_app_settings")
    env_key = planner_params.get("env_key")
    if env_key and get_obs and get_obs.get("ok"):
        if env_key in _env_keys_from_settings_obs(get_obs):
            return None
        if settings_env_upsert_intent(combined) or env_write_intent(planner_params, combined):
            write_tool = "liara.update_env_variables"
    elif write_tool == "liara.update_env_variables" and settings_env_upsert_intent(combined):
        if not get_obs or not get_obs.get("ok"):
            if not app_name_from(planner_params):
                return _clarify("نام اپ را بفرستید (مثلاً `new1`).")
            return _call("liara.get_app_settings", base_params(planner_params))
    if write_tool:
        last = last_obs_for_tool(observations, write_tool)
        if last and last.get("ok"):
            return None
        app = app_name_from(planner_params)
        p = base_params(planner_params, confirm=True)
        if write_tool == "liara.update_env_variables":
            p.update(copy_keys(planner_params, "env_key", "env_value", "variables"))
            if not p.get("env_key") and not p.get("variables"):
                return _clarify(
                    "نام و مقدار متغیر را بفرستید، مثلاً `SAMPLE_NAME=sample_value` "
                    "یا name SAMPLE_NAME value sample_value."
                )
            if not app:
                return _clarify("نام اپ لیارا را بفرستید (مثلاً `new1`).")
            if not planner_params.get("confirm_write"):
                key = p.get("env_key") or "?"
                return _clarify(
                    f"ست کردن متغیر `{key}` روی اپ `{app}` (با redeploy) را تأیید کنید؛ "
                    "مقدار در لاگ نمایش داده نمی‌شود. بنویسید: تأیید می‌کنم."
                )
            return _call(write_tool, p)
        if not app:
            return _clarify("نام اپ را برای این تغییر تنظیمات بفرستید.")
        if write_tool == "liara.restart_service" and not planner_params.get("confirm_write"):
            return _clarify(
                f"ریستارت اپ `{app}` باعث قطع کوتاه سرویس می‌شود. برای ادامه بنویسید: تأیید می‌کنم."
            )
        if not planner_params.get("confirm_write"):
            return _clarify("این تغییر روی اپ اثر می‌گذارد. برای ادامه بنویسید: تأیید می‌کنم.")
        if write_tool == "liara.set_zero_downtime":
            if planner_params.get("enabled") is None:
                return _clarify("zero-downtime را فعال کنم یا غیرفعال؟")
            p["enabled"] = planner_params["enabled"]
        return _call(write_tool, p)
    if get_obs and get_obs.get("ok"):
        return None
    if not app_name_from(planner_params):
        return _clarify("نام اپ را بفرستید.")
    return _call("liara.get_app_settings", base_params(planner_params))


# --- resize ---


def resize_write_tool(planner_params: dict[str, Any], combined: str) -> str | None:
    t = combined.lower()
    if planner_params.get("plan_id") or any(x in t for x in ("upgrade plan", "change plan", "پلن", "plan_id")):
        if any(x in t for x in ("change", "upgrade", "downgrade", "تغییر", "ارتقا", "کاهش")) or planner_params.get(
            "plan_id"
        ):
            return "liara.change_service_plan"
    if planner_params.get("scale") is not None or any(
        x in t for x in ("scale", "replica", "ریپلیکا", "instance", "متوقف", "stop")
    ):
        return "liara.scale_service"
    return None


def forced_resize_action(
    observations: Obs,
    planner_params: dict[str, Any],
    *,
    user_goal: str,
    conversation_history: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    enrich_planner_params(
        "liara.resize",
        user_goal,
        planner_params,
        conversation_history=conversation_history,
    )
    combined = conversation_text(user_goal, conversation_history)
    write_tool = resize_write_tool(planner_params, combined)
    if write_tool:
        last = last_obs_for_tool(observations, write_tool)
        if last and last.get("ok"):
            return None
        app = app_name_from(planner_params)
        if not app:
            return _clarify("نام اپ را بفرستید.")
        if not planner_params.get("confirm_write"):
            return _clarify("تغییر پلن یا scale هزینه دارد. برای ادامه: تأیید می‌کنم.")
        p = base_params(planner_params, confirm=True)
        if write_tool == "liara.change_service_plan":
            if not planner_params.get("plan_id"):
                if not last_obs_for_tool(observations, "liara.list_available_plans"):
                    return _call("liara.list_available_plans", {})
                return _clarify("plan_id مقصد را بفرستید (مثلاً small-g2).")
            p["plan_id"] = planner_params["plan_id"]
        if write_tool == "liara.scale_service":
            if planner_params.get("scale") is None:
                return _clarify("تعداد replica (مثلاً 0 برای توقف یا 2) را بفرستید.")
            p["scale"] = planner_params["scale"]
        return _call(write_tool, p)
    if last_obs_for_tool(observations, "liara.get_service_plan_info"):
        return None
    if not app_name_from(planner_params):
        return _clarify("نام اپ را بفرستید.")
    return _call("liara.get_service_plan_info", base_params(planner_params))


def _bootstrap_guide_params(
    planner_params: dict[str, Any],
    observations: Obs,
    *,
    user_goal: str,
) -> dict[str, Any]:
    plans_data = _obs_data(observations, "liara.list_available_plans")
    plans = (plans_data or {}).get("plans") if plans_data else []
    prep = _obs_data(observations, "repo.prepare_deploy_workspace")
    analysis = None
    if prep:
        analysis = {
            "stack": prep.get("stack"),
            "deploy_port": prep.get("port"),
            "findings": prep.get("findings"),
        }
        if prep.get("platform"):
            planner_params.setdefault("platform", prep.get("platform"))
        if prep.get("port"):
            planner_params.setdefault("port", prep.get("port"))
    return {
        "user_goal": user_goal,
        "repo": planner_params.get("repo"),
        "platform": planner_params.get("platform"),
        "stack": planner_params.get("stack") or (analysis or {}).get("stack"),
        "port": planner_params.get("port") or (analysis or {}).get("deploy_port"),
        "plans": plans if isinstance(plans, list) else [],
        "analysis": analysis,
    }


def forced_bootstrap_action(
    observations: Obs,
    planner_params: dict[str, Any],
    *,
    user_goal: str,
    conversation_history: list[dict[str, str]] | None,
    liara_projects: list[str] | None = None,
    github_repos: list[str] | None = None,
) -> dict[str, Any] | None:
    enrich_planner_params(
        "liara.bootstrap",
        user_goal,
        planner_params,
        conversation_history=conversation_history,
        liara_projects=liara_projects,
        github_repos=github_repos,
    )
    _default_app_if_single(planner_params, liara_projects)
    repo = planner_params.get("repo")
    app = app_name_from(planner_params)
    combined = conversation_text(user_goal, conversation_history)
    user_wants_repo_review = bool(repo) and (
        repo.lower() in user_goal.lower()
        or "github" in user_goal.lower()
        or "owner/" in user_goal.lower()
    )

    if user_wants_repo_review:
        clone = last_obs_for_tool(observations, "repo.clone_github")
        if clone is None:
            p: dict[str, Any] = {"repo": repo}
            if planner_params.get("subpath"):
                p["subpath"] = planner_params["subpath"]
            if github_repos:
                p["github_repos"] = github_repos
            return _call("repo.clone_github", p)
        if clone.get("ok") and not last_obs_for_tool(observations, "repo.prepare_deploy_workspace"):
            pp: dict[str, Any] = {"repo": repo}
            if app:
                pp["app_name"] = app
            if planner_params.get("subpath"):
                pp["subpath"] = planner_params["subpath"]
            return _call("repo.prepare_deploy_workspace", pp)

    if not last_obs_for_tool(observations, "liara.list_available_plans"):
        return _call("liara.list_available_plans", {})

    if app and not last_obs_for_tool(observations, "liara.get_service_plan_info"):
        return _call("liara.get_service_plan_info", base_params(planner_params))

    domain_topic = any(
        x in combined.lower()
        for x in ("دامنه", "domain", "dns", "irnic", "ssl", "certificate")
    )
    if domain_topic:
        if app and not last_obs_for_tool(observations, "liara.list_app_domains"):
            return _call("liara.list_app_domains", base_params(planner_params))
        if not last_obs_for_tool(observations, "guide.domain_connect_playbook"):
            return _call(
                "guide.domain_connect_playbook",
                _guide_domain_params(
                    planner_params,
                    observations,
                    user_goal=user_goal,
                    defer_attach=is_domain_guide_only(user_goal, conversation_history),
                ),
            )

    if not last_obs_for_tool(observations, "guide.recommend_setup"):
        return _call(
            "guide.recommend_setup",
            _bootstrap_guide_params(planner_params, observations, user_goal=user_goal),
        )
    return None


def read_skill_complete(skill_id: str, observations: Obs) -> bool:
    """Primary read tool succeeded — skip extra judge/replan LLM round."""
    tool_id = READ_PRIMARY_TOOL.get(skill_id)
    if not tool_id:
        return False
    last = last_obs_for_tool(observations, tool_id)
    return last is not None and bool(last.get("ok"))


def guide_skill_complete(skill_id: str, observations: Obs, user_goal: str, conversation_history: list[dict[str, str]] | None) -> bool:
    if skill_id == "liara.bootstrap":
        return bool(
            last_obs_for_tool(observations, "guide.recommend_setup")
            and last_obs_for_tool(observations, "guide.recommend_setup").get("ok")
        )
    if skill_id == "liara.domains" and is_domain_guide_only(user_goal, conversation_history):
        return bool(
            last_obs_for_tool(observations, "guide.domain_connect_playbook")
            and last_obs_for_tool(observations, "guide.domain_connect_playbook").get("ok")
        )
    return False


PIPELINE_SKILLS = frozenset(
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
        "liara.bootstrap",
    }
)


def write_pending(
    skill_id: str,
    planner_params: dict[str, Any],
    user_goal: str,
    observations: Obs,
    *,
    conversation_history: list[dict[str, str]] | None = None,
) -> bool:
    combined = conversation_text(user_goal, conversation_history)
    apply_inferred_env(planner_params, combined)
    if skill_id == "liara.disks" and disk_create_intent(planner_params, user_goal):
        return not disk_create_done(observations) and bool(planner_params.get("confirm_write"))
    if skill_id == "liara.domains":
        wt = domain_write_intent(planner_params, combined)
        return bool(wt and planner_params.get("confirm_write") and not domain_write_done(observations, wt))
    if skill_id == "liara.settings":
        combined = conversation_text(user_goal, conversation_history)
        apply_inferred_env(planner_params, combined)
        get_obs = last_obs_for_tool(observations, "liara.get_app_settings")
        env_key = planner_params.get("env_key") or infer_env_key_name(combined)
        if env_key and settings_env_upsert_intent(combined):
            if not (get_obs and get_obs.get("ok")):
                return True
            if env_key not in _env_keys_from_settings_obs(get_obs):
                return True
            return False
        if is_settings_read(combined):
            return not (get_obs and get_obs.get("ok"))
        wt = settings_write_tool(planner_params, combined)
        last = last_obs_for_tool(observations, wt) if wt else None
        if last and last.get("ok"):
            return False
        if wt == "liara.update_env_variables" and (
            planner_params.get("env_key") or planner_params.get("variables")
        ):
            return True
        return bool(wt and planner_params.get("confirm_write"))
    if skill_id == "liara.resize":
        wt = resize_write_tool(planner_params, combined)
        last = last_obs_for_tool(observations, wt) if wt else None
        return bool(wt and planner_params.get("confirm_write") and not (last and last.get("ok")))
    return False


def forced_skill_action(
    skill_id: str,
    observations: Obs,
    planner_params: dict[str, Any],
    *,
    user_goal: str = "",
    conversation_history: list[dict[str, str]] | None = None,
    liara_projects: list[str] | None = None,
    github_repos: list[str] | None = None,
) -> dict[str, Any] | None:
    if skill_id not in PIPELINE_SKILLS:
        return None
    enrich_planner_params(
        skill_id,
        user_goal,
        planner_params,
        conversation_history=conversation_history,
        liara_projects=liara_projects,
        github_repos=github_repos,
    )
    if skill_id == "liara.disks":
        return forced_disk_action(
            observations,
            planner_params,
            user_goal=user_goal,
            conversation_history=conversation_history,
        )
    if skill_id == "liara.domains":
        return forced_domains_action(
            observations,
            planner_params,
            user_goal=user_goal,
            conversation_history=conversation_history,
            liara_projects=liara_projects,
        )
    if skill_id == "liara.settings":
        return forced_settings_action(
            observations,
            planner_params,
            user_goal=user_goal,
            conversation_history=conversation_history,
            liara_projects=liara_projects,
        )
    if skill_id == "liara.resize":
        return forced_resize_action(
            observations,
            planner_params,
            user_goal=user_goal,
            conversation_history=conversation_history,
        )
    if skill_id == "liara.bootstrap":
        return forced_bootstrap_action(
            observations,
            planner_params,
            user_goal=user_goal,
            conversation_history=conversation_history,
            liara_projects=liara_projects,
            github_repos=github_repos,
        )
    return _forced_read_action(skill_id, observations, planner_params)
