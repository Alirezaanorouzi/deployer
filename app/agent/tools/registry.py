"""Tool registry — register tools once, select & execute from the ReAct agent."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.liara.adapter import LiaraAdapter
from app.liara.writes import CONFIRMATION_MESSAGE, parse_env_updates, user_confirmed_write

ToolHandler = Callable[..., Awaitable["ToolResult"]]


@dataclass
class ToolSpec:
    id: str
    name: str
    description: str
    parameters_schema: dict[str, Any]
    risk: str = "low"


@dataclass
class ToolResult:
    ok: bool
    code: str
    text_for_agent: str
    data: dict[str, Any] | None = None


_CONFIRM_WRITE_PROP = {
    "confirm_write": {
        "type": "boolean",
        "description": "Must be true after the user explicitly approved this change in chat",
    },
}


def _write_params(**extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = dict(_CONFIRM_WRITE_PROP)
    props.update(
        {
            "project_id": {
                "type": "string",
                "description": "Liara app id (project_id slug), e.g. my-shop",
            },
            "app_name": {
                "type": "string",
                "description": "Alias for project_id when user names their service",
            },
        }
    )
    props.update(extra)
    return {"type": "object", "properties": props, "additionalProperties": False}


def _require_confirm(params: dict[str, Any]) -> ToolResult | None:
    if not user_confirmed_write(params):
        return ToolResult(False, "CONFIRMATION_REQUIRED", CONFIRMATION_MESSAGE, None)
    return None


def _project_params(**extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = {
        "project_id": {
            "type": "string",
            "description": "Liara app id (project_id slug), e.g. my-shop",
        },
        "app_name": {
            "type": "string",
            "description": "Alias for project_id when user names their service",
        },
    }
    props.update(extra)
    return {"type": "object", "properties": props, "additionalProperties": False}


_USAGE_PARAM_SCHEMA = _project_params(
    since_hours={
        "type": "number",
        "description": "Metrics window in hours (0.25–168, default 1)",
    },
)

_LOGS_PARAM_SCHEMA = _project_params(
    lines={
        "type": "integer",
        "description": "Number of log lines to fetch (10–400, default 120)",
    },
    focus={
        "type": "string",
        "description": "What to look for: errors, 500, deploy failure, slow requests, etc.",
    },
    since_hours={
        "type": "number",
        "description": "Relative window: logs since N hours ago (0.25–168, default 1)",
    },
    since_ts={
        "type": "integer",
        "description": "Unix timestamp (seconds) — start of log window",
    },
    until_ts={
        "type": "integer",
        "description": "Unix timestamp (seconds) — end of log window (optional, default now)",
    },
    since_iso={
        "type": "string",
        "description": "ISO-8601 start time, e.g. 2026-10-21T10:00:00Z",
    },
    until_iso={
        "type": "string",
        "description": "ISO-8601 end time (optional)",
    },
)

_LOG_TIME_KEYS = ("since_hours", "since_ts", "until_ts", "since_iso", "until_iso")

_EVENTS_PARAM_SCHEMA = _project_params(
    since_hours={
        "type": "number",
        "description": "Events in the last N hours (0.25–168); optional",
    },
    start_ts={
        "type": "integer",
        "description": "Unix start time (seconds) for events filter",
    },
    end_ts={
        "type": "integer",
        "description": "Unix end time (seconds) for events filter",
    },
    search={
        "type": "string",
        "description": "Optional search string for events API",
    },
)

_RELEASES_PARAM_SCHEMA = _project_params(
    page={"type": "integer", "description": "Page number (default 1)"},
    count={
        "type": "integer",
        "description": "Releases per page (1–20, default 10)",
    },
)


def time_range_from_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: params[k] for k in _LOG_TIME_KEYS if params.get(k) is not None}

TOOLS: dict[str, ToolSpec] = {
    "liara.get_account_balance": ToolSpec(
        id="liara.get_account_balance",
        name="Get Liara account balance",
        description=(
            "Fetch the Liara account remaining credit (balance) in Toman via the billing API. "
            "Use when the user asks about balance, credit, wallet, or remaining money on Liara."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk="low",
    ),
    "liara.get_service_resource_usage": ToolSpec(
        id="liara.get_service_resource_usage",
        name="Get service resource usage",
        description=(
            "Fetch CPU and memory usage for a Liara PaaS app (any project on the user's account). "
            "Uses Liara metrics APIs. Requires project_id or app_name unless the account has only one app."
        ),
        parameters_schema=_USAGE_PARAM_SCHEMA,
        risk="low",
    ),
    "liara.analyze_service_logs": ToolSpec(
        id="liara.analyze_service_logs",
        name="Retrieve and analyze service logs",
        description=(
            "Fetch logs for any Liara PaaS app (API requires since timestamp; supports since_hours, "
            "since_ts/until_ts, or since_iso/until_iso). Analyze errors and next steps. "
            "On empty results, replan with a wider since_hours."
        ),
        parameters_schema=_LOGS_PARAM_SCHEMA,
        risk="low",
    ),
    "liara.list_app_events": ToolSpec(
        id="liara.list_app_events",
        name="List platform events",
        description=(
            "Read Liara platform events for a PaaS app (deploy activity, config changes, etc.). "
            "Optional since_hours or start_ts/end_ts (unix seconds) and search."
        ),
        parameters_schema=_EVENTS_PARAM_SCHEMA,
        risk="low",
    ),
    "liara.list_deploy_releases": ToolSpec(
        id="liara.list_deploy_releases",
        name="List deploy / release history",
        description=(
            "Deployment and release history for a Liara app (current release, past builds, git commits). "
            "Use before deploy_service or rollback decisions."
        ),
        parameters_schema=_RELEASES_PARAM_SCHEMA,
        risk="low",
    ),
    "liara.list_app_domains": ToolSpec(
        id="liara.list_app_domains",
        name="List custom domains",
        description=(
            "List custom domains linked to a Liara PaaS app, certificate status, and redirects."
        ),
        parameters_schema=_project_params(),
        risk="low",
    ),
    "liara.list_app_disks": ToolSpec(
        id="liara.list_app_disks",
        name="List attached disks",
        description=(
            "List persistent disks attached to a Liara PaaS app. Handles API errors gracefully."
        ),
        parameters_schema=_project_params(),
        risk="low",
    ),
    "liara.get_app_settings": ToolSpec(
        id="liara.get_app_settings",
        name="Get app settings snapshot",
        description=(
            "Read app settings from Liara: scale flags, network, env var names (values redacted), "
            "zero-downtime, read-only FS, etc."
        ),
        parameters_schema=_project_params(),
        risk="low",
    ),
    "liara.get_service_plan_info": ToolSpec(
        id="liara.get_service_plan_info",
        name="Get plan and scale info",
        description="Read current plan ID, hourly price, bundle plan, and replica scale.",
        parameters_schema=_project_params(),
        risk="low",
    ),
    "liara.list_available_plans": ToolSpec(
        id="liara.list_available_plans",
        name="List available service plans",
        description="List Liara PaaS plans (plan_id values for change_service_plan).",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk="low",
    ),
    "liara.restart_service": ToolSpec(
        id="liara.restart_service",
        name="Restart app",
        description="Restart a Liara PaaS app (rolling restart). Requires confirm_write after user approval.",
        parameters_schema=_write_params(),
        risk="high",
    ),
    "liara.scale_service": ToolSpec(
        id="liara.scale_service",
        name="Scale or stop app",
        description="Set replica count (scale 0 stops the app). Requires confirm_write.",
        parameters_schema=_write_params(
            scale={"type": "integer", "description": "Replica count (0 to stop, 1+ to run)"},
        ),
        risk="high",
    ),
    "liara.change_service_plan": ToolSpec(
        id="liara.change_service_plan",
        name="Change service plan",
        description="Resize/upgrade/downgrade plan via plan_id. Use list_available_plans first. Requires confirm_write.",
        parameters_schema=_write_params(
            plan_id={"type": "string", "description": "Target plan ID, e.g. small-g2"},
        ),
        risk="high",
    ),
    "liara.update_env_variables": ToolSpec(
        id="liara.update_env_variables",
        name="Update environment variables",
        description=(
            "Set env vars (triggers redeploy). Pass env_key/env_value or variables array. Requires confirm_write."
        ),
        parameters_schema=_write_params(
            env_key={"type": "string", "description": "Single env var name"},
            env_value={"type": "string", "description": "Value for env_key"},
            variables={
                "type": "array",
                "description": "List of {key, value} objects for multiple updates",
            },
        ),
        risk="medium",
    ),
    "liara.set_zero_downtime": ToolSpec(
        id="liara.set_zero_downtime",
        name="Enable or disable zero-downtime deploys",
        description="Toggle zero-downtime setting. Requires confirm_write.",
        parameters_schema=_write_params(
            enabled={"type": "boolean", "description": "true to enable, false to disable"},
        ),
        risk="medium",
    ),
    "liara.create_app_domain": ToolSpec(
        id="liara.create_app_domain",
        name="Add custom domain",
        description="Attach a custom domain to the app. Requires confirm_write.",
        parameters_schema=_write_params(
            domain_name={"type": "string", "description": "FQDN, e.g. shop.example.com"},
        ),
        risk="medium",
    ),
    "liara.set_domain_redirect": ToolSpec(
        id="liara.set_domain_redirect",
        name="Set domain redirect",
        description="HTTP redirect for a domain (301/302). Requires confirm_write.",
        parameters_schema=_write_params(
            domain_id={"type": "string", "description": "Liara domain _id"},
            domain_name={"type": "string", "description": "Or resolve by domain name"},
            redirect_to={"type": "string", "description": "Target URL"},
            redirect_status={"type": "integer", "description": "301 or 302"},
        ),
        risk="medium",
    ),
    "liara.create_app_disk": ToolSpec(
        id="liara.create_app_disk",
        name="Create persistent disk",
        description=(
            "Create a disk on the app (Liara name: lowercase letters, digits, hyphens only — "
            "use shop-cache not shop_cache). Size is integer GB. Requires confirm_write."
        ),
        parameters_schema=_write_params(
            disk_name={"type": "string", "description": "Disk name (hyphens, e.g. shop-cache)"},
            size_gb={
                "type": "integer",
                "description": "Size in GB (e.g. 5). Aliases: size",
            },
            size={"type": "integer", "description": "Alias for size_gb"},
        ),
        risk="high",
    ),
    "liara.deploy_service": ToolSpec(
        id="liara.deploy_service",
        name="Deploy app from directory",
        description=(
            "Deploy local project path with Liara CLI (same as console New Deployment). "
            "Requires confirm_write and deploy_path on server."
        ),
        parameters_schema=_write_params(
            deploy_path={
                "type": "string",
                "description": "Absolute or workspace path containing liara.json / app source",
            },
            port={"type": "integer", "description": "App port (default 3000)"},
        ),
        risk="high",
    ),
}


async def _run_get_account_balance(liara: LiaraAdapter, _params: dict[str, Any]) -> ToolResult:
    r = await liara.get_account_balance()
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    data = r.data or {}
    credit = data.get("credit_toman")
    if credit is not None:
        try:
            lines = [f"Remaining account credit: **{float(credit):,.0f} Toman**"]
        except (TypeError, ValueError):
            lines = [f"Remaining account credit: **{credit} Toman**"]
    else:
        lines = [r.message]
    if data.get("account_type"):
        lines.append(f"Account type: {data['account_type']}")
    if data.get("min_credit_alert_toman") is not None:
        lines.append(f"Low-balance alert threshold: {data['min_credit_alert_toman']:,} Toman")
    if data.get("remaining_free_credit") is not None:
        lines.append(f"Remaining free credit: {data['remaining_free_credit']:,} Toman")
    return ToolResult(True, "OK", "\n".join(lines), data)


async def _run_get_service_resource_usage(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    since = params.get("since_hours")
    try:
        since_hours = float(since) if since is not None else 1.0
    except (TypeError, ValueError):
        since_hours = 1.0
    r = await liara.get_service_resource_usage(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        since_hours=since_hours,
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_list_app_events(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    since = params.get("since_hours")
    try:
        since_hours = float(since) if since is not None else None
    except (TypeError, ValueError):
        since_hours = None
    r = await liara.list_app_events(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        since_hours=since_hours,
        start_ts=params.get("start_ts"),
        end_ts=params.get("end_ts"),
        search=params.get("search"),
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_list_deploy_releases(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    page = params.get("page", 1)
    count = params.get("count", 10)
    try:
        page_i = int(page)
    except (TypeError, ValueError):
        page_i = 1
    try:
        count_i = int(count)
    except (TypeError, ValueError):
        count_i = 10
    r = await liara.list_deploy_releases(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        page=page_i,
        count=count_i,
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_list_app_domains(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    r = await liara.list_app_domains(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_list_app_disks(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    r = await liara.list_app_disks(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    ok = r.ok or r.code == "DISKS_UNAVAILABLE"
    return ToolResult(ok, r.code, r.message, r.data)


async def _run_get_app_settings(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    r = await liara.get_app_settings(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_get_service_plan_info(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    r = await liara.get_service_plan_info(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_list_available_plans(liara: LiaraAdapter, _params: dict[str, Any]) -> ToolResult:
    r = await liara.list_available_plans()
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_restart_service(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    r = await liara.restart_service(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_scale_service(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    scale = params.get("scale", 1)
    try:
        scale_i = int(scale)
    except (TypeError, ValueError):
        scale_i = 1
    r = await liara.scale_service(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        scale=scale_i,
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_change_service_plan(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    r = await liara.change_service_plan(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        plan_id=params.get("plan_id") or "",
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_update_env_variables(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    variables = parse_env_updates(params)
    if not variables:
        return ToolResult(False, "INVALID_INPUT", "Provide env_key/env_value or variables list", None)
    r = await liara.update_env_variables(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        variables=variables,
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_set_zero_downtime(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    enabled = params.get("enabled")
    if enabled is None:
        return ToolResult(False, "INVALID_INPUT", "enabled (boolean) is required", None)
    r = await liara.set_zero_downtime(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        enabled=bool(enabled),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_create_app_domain(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    r = await liara.create_app_domain(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        domain_name=params.get("domain_name") or "",
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_set_domain_redirect(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    r = await liara.set_domain_redirect(
        domain_id=params.get("domain_id"),
        domain_name=params.get("domain_name"),
        redirect_to=params.get("redirect_to") or "",
        redirect_status=int(params.get("redirect_status") or 301),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_create_app_disk(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    disk_name = params.get("disk_name") or params.get("name") or ""
    size = params.get("size_gb")
    if size is None:
        size = params.get("size")
    if size is None:
        size = 1
    r = await liara.create_app_disk(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        disk_name=disk_name,
        size_gb=size,
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_deploy_service(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    path = params.get("deploy_path") or params.get("path") or "."
    port = params.get("port", 3000)
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = 3000
    r = await liara.deploy_service_from_path(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        path=path,
        port=port_i,
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_analyze_service_logs(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    lines = params.get("lines", 120)
    try:
        line_count = int(lines)
    except (TypeError, ValueError):
        line_count = 120
    r = await liara.get_and_analyze_service_logs(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        lines=line_count,
        focus=params.get("focus"),
        time_range=time_range_from_params(params),
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


_HANDLERS: dict[str, ToolHandler] = {
    "liara.get_account_balance": _run_get_account_balance,
    "liara.get_service_resource_usage": _run_get_service_resource_usage,
    "liara.analyze_service_logs": _run_analyze_service_logs,
    "liara.list_app_events": _run_list_app_events,
    "liara.list_deploy_releases": _run_list_deploy_releases,
    "liara.list_app_domains": _run_list_app_domains,
    "liara.list_app_disks": _run_list_app_disks,
    "liara.get_app_settings": _run_get_app_settings,
    "liara.get_service_plan_info": _run_get_service_plan_info,
    "liara.list_available_plans": _run_list_available_plans,
    "liara.restart_service": _run_restart_service,
    "liara.scale_service": _run_scale_service,
    "liara.change_service_plan": _run_change_service_plan,
    "liara.update_env_variables": _run_update_env_variables,
    "liara.set_zero_downtime": _run_set_zero_downtime,
    "liara.create_app_domain": _run_create_app_domain,
    "liara.set_domain_redirect": _run_set_domain_redirect,
    "liara.create_app_disk": _run_create_app_disk,
    "liara.deploy_service": _run_deploy_service,
}


def list_tool_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "parameters_schema": t.parameters_schema,
            "risk": t.risk,
        }
        for t in TOOLS.values()
    ]


def tools_for_skill(skill_id: str) -> list[str]:
    return _SKILL_TOOL_HINTS.get(skill_id, list(TOOLS.keys()))


_SKILL_TOOL_HINTS: dict[str, list[str]] = {
    "liara.balance": ["liara.get_account_balance"],
    "liara.usage": ["liara.get_service_resource_usage"],
    "liara.logs": ["liara.analyze_service_logs"],
    "liara.events": ["liara.list_app_events"],
    "liara.history": ["liara.list_deploy_releases", "liara.deploy_service"],
    "liara.domains": [
        "liara.list_app_domains",
        "liara.create_app_domain",
        "liara.set_domain_redirect",
    ],
    "liara.disks": ["liara.list_app_disks", "liara.create_app_disk"],
    "liara.settings": [
        "liara.get_app_settings",
        "liara.update_env_variables",
        "liara.set_zero_downtime",
        "liara.restart_service",
    ],
    "liara.resize": [
        "liara.get_service_plan_info",
        "liara.list_available_plans",
        "liara.change_service_plan",
        "liara.scale_service",
    ],
    "liara.deploy": [
        "liara.list_deploy_releases",
        "liara.deploy_service",
    ],
    "agent.summarize": [],
}


async def execute_tool(
    tool_id: str,
    *,
    liara: LiaraAdapter,
    params: dict[str, Any] | None = None,
) -> ToolResult:
    if tool_id not in TOOLS:
        return ToolResult(False, "UNKNOWN_TOOL", f"Unknown tool: {tool_id}", None)
    handler = _HANDLERS.get(tool_id)
    if not handler:
        return ToolResult(False, "NO_HANDLER", f"No handler for {tool_id}", None)
    return await handler(liara, params or {})
