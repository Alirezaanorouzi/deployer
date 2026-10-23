"""Tool registry — register tools once, select & execute from the ReAct agent."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.liara.adapter import LiaraAdapter
from app.liara.deploy_diagnostics import (
    _LIARA_PYTHON_RUNTIME_CHECKLIST,
    format_diagnosis,
    hints_from_logs,
)
from app.liara.create_app import LIARA_CREATE_PLATFORMS, normalize_liara_app_id
from app.liara.cli import OnLine
from app.liara.writes import CONFIRMATION_MESSAGE, parse_env_updates, user_confirmed_write
from app.repo.paths import deploy_root, normalize_github_repo, normalize_subpath
from app.repo.workspace import (
    analyze_local_directory,
    clone_repository,
    infer_liara_platform,
    slug_app_name,
    write_liara_config,
)

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


def _workspace_root_for_tool(params: dict[str, Any]) -> Path | None:
    deploy_path = params.get("deploy_path")
    if deploy_path:
        p = Path(str(deploy_path)).resolve()
        return p if p.is_dir() else None
    run_id = params.get("run_id")
    if not run_id:
        return None
    root = deploy_root(run_id, normalize_subpath(params.get("subpath")))
    return root if root.is_dir() else None


def _resolve_platform_for_create(params: dict[str, Any]) -> tuple[str | None, str | None]:
    explicit = (params.get("platform") or "").strip().lower()
    if explicit:
        return explicit, None
    root = _workspace_root_for_tool(params)
    if not root:
        return None, None
    platform, stack = infer_liara_platform(root)
    return platform, stack


def _clone_error_result(exc: BaseException) -> ToolResult:
    msg = str(exc).replace("CLONE_FAILED: ", "").strip()
    if not msg:
        msg = (
            f"{type(exc).__name__}: GitHub fetch failed. "
            "Check GITHUB_TOKEN, repo access (owner/name), and server terminal logs."
        )
    return ToolResult(False, "CLONE_FAILED", msg, {"error_type": type(exc).__name__})


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
        name="Deploy / release (platform Deploy tab)",
        description=(
            "Upload source, build, and start the app — same as the Liara platform service **Deploy / استقرار** "
            "sub-tab (turns app from ready آماده به کار to running روشن). "
            "Requires confirm_write, app_name, and deploy_path from prepare_deploy_workspace."
        ),
        parameters_schema=_write_params(
            deploy_path={
                "type": "string",
                "description": "Absolute or workspace path containing liara.json / app source",
            },
            port={"type": "integer", "description": "App listen port (default from prepare step or 3000)"},
            platform={
                "type": "string",
                "description": "Liara platform: python, node, next, … (optional)",
            },
        ),
        risk="high",
    ),
    "liara.list_networks": ToolSpec(
        id="liara.list_networks",
        name="List private networks",
        description="List Liara private networks available for new apps (API).",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk="low",
    ),
    "liara.ensure_private_network": ToolSpec(
        id="liara.ensure_private_network",
        name="Create private network if missing",
        description=(
            "Ensure a private network exists (default name from LIARA_NETWORK_NAME or my-network). "
            "Runs automatically before create_app; call explicitly to create or pick a name."
        ),
        parameters_schema=_write_params(
            network_name={
                "type": "string",
                "description": "Network name (e.g. my-network). Default from server env.",
            },
        ),
        risk="medium",
    ),
    "liara.list_apps": ToolSpec(
        id="liara.list_apps",
        name="List Liara apps",
        description="List PaaS apps (project_id slugs) on the current Liara account (API).",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk="low",
    ),
    "liara.bind_app": ToolSpec(
        id="liara.bind_app",
        name="Select active app for this session",
        description=(
            "Bind a Liara app id as the active context so logs, settings, deploy, and other app tools "
            "target that service. Call after create_app or when user names an existing app."
        ),
        parameters_schema=_project_params(),
        risk="low",
    ),
    "liara.create_app": ToolSpec(
        id="liara.create_app",
        name="Create Liara PaaS app",
        description=(
            "Create a new Liara app via platform API (plan, network). Skips if the app id already exists. "
            "Binds the app as active context on success. Requires confirm_write after user approves."
        ),
        parameters_schema=_write_params(
            platform={
                "type": "string",
                "description": f"Liara platform (required after prepare): {', '.join(sorted(LIARA_CREATE_PLATFORMS))}",
            },
            network_name={
                "type": "string",
                "description": "Optional private network name (default LIARA_NETWORK_NAME / my-network)",
            },
        ),
        risk="high",
    ),
    "liara.get_app_url": ToolSpec(
        id="liara.get_app_url",
        name="Resolve default app URL",
        description="Return the default Liara subdomain (https://<app-id>.liara.run).",
        parameters_schema=_project_params(),
        risk="low",
    ),
    "liara.verify_app_health": ToolSpec(
        id="liara.verify_app_health",
        name="HTTP health check",
        description="GET the app URL (or custom url) and report status code.",
        parameters_schema=_project_params(
            url={"type": "string", "description": "Optional full URL; default is app subdomain"},
        ),
        risk="low",
    ),
    "liara.diagnose_deploy_health": ToolSpec(
        id="liara.diagnose_deploy_health",
        name="Diagnose 502 / unhealthy deploy",
        description=(
            "HTTP check plus recent Liara logs when status is 5xx or connection fails. "
            "Use after deploy_service when the app is ON but the URL fails."
        ),
        parameters_schema=_project_params(
            url={"type": "string", "description": "Optional URL; default app subdomain"},
            log_lines={"type": "integer", "description": "Log lines to fetch (default 40)"},
        ),
        risk="low",
    ),
    "repo.clone_github": ToolSpec(
        id="repo.clone_github",
        name="Clone GitHub repository",
        description=(
            "Clone owner/name into this run workspace (host git, sandbox, or GitHub zipball). "
            "Requires params.repo and run_id (injected by agent)."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub owner/name, e.g. Alirezaanorouzi/deployer",
                },
                "run_id": {"type": "string", "description": "Workspace run id (auto)"},
                "github_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Known repos for resolving bare names",
                },
            },
            "additionalProperties": False,
        },
        risk="low",
    ),
    "repo.prepare_deploy_workspace": ToolSpec(
        id="repo.prepare_deploy_workspace",
        name="Analyze repo and write liara.json",
        description=(
            "After clone: detect stack (python/next/node), write or update liara.json, return deploy_path and port."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "app_name": {"type": "string", "description": "Liara app id slug"},
                "subpath": {"type": "string", "description": "Optional monorepo subfolder"},
                "repo": {"type": "string", "description": "For default app slug from repo name"},
            },
            "additionalProperties": False,
        },
        risk="medium",
    ),
    "guide.recommend_setup": ToolSpec(
        id="guide.recommend_setup",
        name="Recommend deployment method and Liara plan",
        description=(
            "Read-only setup guide: deployment path (GitHub agent vs CLI), suggested platform/port, "
            "starter plan_id from list_available_plans, disk/DB hints, and manual checklist. "
            "Use after list_available_plans and optional repo.prepare_deploy_workspace."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "user_goal": {"type": "string"},
                "repo": {"type": "string"},
                "platform": {"type": "string"},
                "stack": {"type": "string"},
                "port": {"type": "integer"},
                "plans": {"type": "array", "items": {"type": "object"}},
                "analysis": {"type": "object"},
            },
            "additionalProperties": True,
        },
        risk="low",
    ),
    "guide.domain_connect_playbook": ToolSpec(
        id="guide.domain_connect_playbook",
        name="Domain + IRNIC / registrar playbook",
        description=(
            "Read-only steps for connecting a custom domain: user tasks at IRNIC/registrar, "
            "agent capabilities (list domains, check status, add after approval). No API writes."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "user_goal": {"type": "string"},
                "app_name": {"type": "string"},
                "domain_name": {"type": "string"},
                "default_url": {"type": "string"},
                "domains": {"type": "array", "items": {"type": "object"}},
                "defer_attach": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        risk="low",
    ),
    "liara.check_domain_status": ToolSpec(
        id="liara.check_domain_status",
        name="Check domain DNS/SSL status",
        description=(
            "Liara API GET /v1/domains/{id}/check — verification and certificate status. "
            "Pass domain_id or domain_name (must exist on the account)."
        ),
        parameters_schema=_project_params(
            domain_id={"type": "string"},
            domain_name={"type": "string"},
        ),
        risk="low",
    ),
    "liara.delete_app": ToolSpec(
        id="liara.delete_app",
        name="Delete Liara app",
        description="DELETE /v1/projects/{name} — permanently removes the app. Requires confirm_write.",
        parameters_schema=_write_params(),
        risk="high",
    ),
    "liara.deploy_release": ToolSpec(
        id="liara.deploy_release",
        name="Deploy an existing release/source",
        description=(
            "POST /v2/projects/{name}/releases — redeploy a previous sourceID from release history. "
            "Use list_deploy_releases first. Requires confirm_write."
        ),
        parameters_schema=_write_params(
            source_id={"type": "string", "description": "Release source ID from history"},
            port={"type": "integer", "description": "Listen port if required"},
            platform_type={"type": "string", "description": "Platform type if required"},
        ),
        risk="high",
    ),
    "liara.delete_app_domain": ToolSpec(
        id="liara.delete_app_domain",
        name="Delete custom domain",
        description="DELETE /v1/domains/{id}. Requires confirm_write.",
        parameters_schema=_write_params(
            domain_id={"type": "string"},
            domain_name={"type": "string"},
        ),
        risk="high",
    ),
    "liara.enable_domain_ssl": ToolSpec(
        id="liara.enable_domain_ssl",
        name="Provision SSL certificate",
        description="POST /v1/domains/provision-ssl-certs for domain FQDN. Requires confirm_write.",
        parameters_schema=_write_params(
            domain_name={"type": "string", "description": "FQDN, e.g. shop.example.com"},
        ),
        risk="medium",
    ),
    "liara.disable_domain_ssl": ToolSpec(
        id="liara.disable_domain_ssl",
        name="Disable domain SSL",
        description="POST /v1/domains/{id}/ssl/disable. Requires confirm_write.",
        parameters_schema=_write_params(
            domain_id={"type": "string"},
            domain_name={"type": "string"},
        ),
        risk="medium",
    ),
    "liara.delete_app_disk": ToolSpec(
        id="liara.delete_app_disk",
        name="Delete persistent disk",
        description="DELETE /v1/projects/{id}/disks/{name}. Requires confirm_write.",
        parameters_schema=_write_params(
            disk_name={"type": "string"},
        ),
        risk="high",
    ),
    "liara.resize_app_disk": ToolSpec(
        id="liara.resize_app_disk",
        name="Resize persistent disk",
        description="POST .../disks/{name}/resize. Requires confirm_write.",
        parameters_schema=_write_params(
            disk_name={"type": "string"},
            size_gb={"type": "integer"},
            size={"type": "integer", "description": "Alias for size_gb"},
        ),
        risk="high",
    ),
    "liara.create_disk_backup": ToolSpec(
        id="liara.create_disk_backup",
        name="Create disk backup",
        description="POST .../disks/{name}/backups. Requires confirm_write.",
        parameters_schema=_write_params(
            disk_name={"type": "string"},
        ),
        risk="medium",
    ),
    "liara.create_disk_ftp": ToolSpec(
        id="liara.create_disk_ftp",
        name="Create disk FTP access",
        description="POST .../disks/{dname}/ftp. Requires confirm_write.",
        parameters_schema=_write_params(
            disk_name={"type": "string"},
            ftp_username={"type": "string"},
            read_only={"type": "boolean", "description": "FTP read-only flag"},
        ),
        risk="medium",
    ),
    "liara.delete_disk_ftp": ToolSpec(
        id="liara.delete_disk_ftp",
        name="Delete disk FTP account",
        description="DELETE /v1/ftp/{fname}. Requires confirm_write.",
        parameters_schema=_write_params(
            ftp_name={"type": "string", "description": "FTP account name to delete"},
        ),
        risk="medium",
    ),
    "liara.set_read_only_filesystem": ToolSpec(
        id="liara.set_read_only_filesystem",
        name="Toggle read-only app filesystem",
        description="POST /v1/projects/{id}/read-only/enable|disable. Requires confirm_write.",
        parameters_schema=_write_params(
            enabled={"type": "boolean", "description": "true=enable read-only root"},
        ),
        risk="high",
    ),
    "liara.set_default_subdomain": ToolSpec(
        id="liara.set_default_subdomain",
        name="Toggle default liara.run subdomain",
        description="POST /v1/projects/{id}/default-subdomain/enable|disable. Requires confirm_write.",
        parameters_schema=_write_params(
            enabled={"type": "boolean"},
        ),
        risk="medium",
    ),
    "liara.set_fixed_ip": ToolSpec(
        id="liara.set_fixed_ip",
        name="Toggle static/fixed IP",
        description="POST /v1/projects/{id}/fixed-ip/enable|disable. Requires confirm_write.",
        parameters_schema=_write_params(
            enabled={"type": "boolean"},
        ),
        risk="high",
    ),
}

PAAS_WRITE_TOOL_IDS: frozenset[str] = frozenset(
    {
        "liara.restart_service",
        "liara.scale_service",
        "liara.change_service_plan",
        "liara.update_env_variables",
        "liara.set_zero_downtime",
        "liara.create_app_domain",
        "liara.set_domain_redirect",
        "liara.create_app_disk",
        "liara.deploy_service",
        "liara.ensure_private_network",
        "liara.create_app",
        "liara.delete_app",
        "liara.deploy_release",
        "liara.delete_app_domain",
        "liara.enable_domain_ssl",
        "liara.disable_domain_ssl",
        "liara.delete_app_disk",
        "liara.resize_app_disk",
        "liara.create_disk_backup",
        "liara.create_disk_ftp",
        "liara.delete_disk_ftp",
        "liara.set_read_only_filesystem",
        "liara.set_default_subdomain",
        "liara.set_fixed_ip",
    }
)


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


async def _run_guide_recommend_setup(_liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    from app.agent.setup_guide import build_setup_recommendation

    text, data = build_setup_recommendation(params)
    return ToolResult(True, "OK", text, data)


async def _run_guide_domain_connect_playbook(_liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    from app.agent.setup_guide import build_domain_connect_playbook

    text, data = build_domain_connect_playbook(params)
    return ToolResult(True, "OK", text, data)


async def _run_check_domain_status(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    r = await liara.check_domain_status(
        domain_id=params.get("domain_id"),
        domain_name=params.get("domain_name"),
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    return ToolResult(True, "OK", r.message, r.data)


async def _run_delete_app(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.delete_app(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_deploy_release(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.deploy_release(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        source_id=params.get("source_id") or "",
        port=params.get("port"),
        platform_type=params.get("platform_type"),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_delete_app_domain(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.delete_app_domain(
        domain_id=params.get("domain_id"),
        domain_name=params.get("domain_name"),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_enable_domain_ssl(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.enable_domain_ssl(domain_name=params.get("domain_name") or "")
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_disable_domain_ssl(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.disable_domain_ssl(
        domain_id=params.get("domain_id"),
        domain_name=params.get("domain_name"),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_delete_app_disk(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.delete_app_disk(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        disk_name=params.get("disk_name") or "",
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_resize_app_disk(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    size = params.get("size_gb") or params.get("size")
    r = await liara.resize_app_disk(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        disk_name=params.get("disk_name") or "",
        size_gb=size if size is not None else "",
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_create_disk_backup(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.create_disk_backup(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        disk_name=params.get("disk_name") or "",
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_create_disk_ftp(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.create_disk_ftp(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        disk_name=params.get("disk_name") or "",
        ftp_username=params.get("ftp_username") or "",
        read_only=bool(params.get("read_only")),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_delete_disk_ftp(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    r = await liara.delete_disk_ftp(ftp_name=params.get("ftp_name") or "")
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_set_read_only_filesystem(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    if params.get("enabled") is None:
        return ToolResult(False, "INVALID_INPUT", "enabled (true/false) is required", None)
    r = await liara.set_read_only_filesystem(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        enabled=bool(params["enabled"]),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_set_default_subdomain(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    if params.get("enabled") is None:
        return ToolResult(False, "INVALID_INPUT", "enabled (true/false) is required", None)
    r = await liara.set_default_subdomain(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        enabled=bool(params["enabled"]),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_set_fixed_ip(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(False, blocked.code, blocked.text_for_agent, None)
    if params.get("enabled") is None:
        return ToolResult(False, "INVALID_INPUT", "enabled (true/false) is required", None)
    r = await liara.set_fixed_ip(
        project_id=params.get("project_id"),
        app_name=params.get("app_name") or params.get("app"),
        enabled=bool(params["enabled"]),
    )
    return ToolResult(r.ok, r.code, r.message, r.data)


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


async def _run_deploy_service(
    liara: LiaraAdapter,
    params: dict[str, Any],
    *,
    on_log: OnLine | None = None,
) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    path = params.get("deploy_path") or params.get("path") or "."
    port = params.get("port", 3000)
    app = params.get("app_name") or params.get("app")
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = 3000
    r = await liara.deploy_service_from_path(
        project_id=params.get("project_id"),
        app_name=app,
        path=path,
        port=port_i,
        on_line=on_log,
    )
    if r.ok:
        return ToolResult(r.ok, r.code, r.message, r.data)
    log_tail = ""
    if app:
        log_r = await liara.logs(str(app), lines=40)
        if log_r.ok and log_r.data:
            log_tail = (log_r.data.get("logs") or "")[-2000:]
    hints = hints_from_logs(log_tail)
    if "modulenotfounderror" in log_tail.lower() or "no module named" in log_tail.lower():
        hints.insert(
            0,
            "Container crashed on import — on Liara use `python main.py` at repo root and ensure the `app` package is deployed.",
        )
    msg = (r.message or "Deploy failed")[:800]
    if log_tail:
        msg = format_diagnosis(
            url=liara.project_url(str(app)) if app else "(app)",
            status_code=None,
            log_tail=log_tail,
            extra_hints=hints,
            log_max_chars=1000,
        )
    return ToolResult(
        False,
        r.code or "DEPLOY_FAILED",
        msg,
        {**(r.data or {}), "logs": log_tail, "hints": hints, "app_name": app},
    )


async def _run_list_networks(liara: LiaraAdapter, _params: dict[str, Any]) -> ToolResult:
    r = await liara.list_networks()
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    networks = (r.data or {}).get("networks") or []
    lines = [
        f"- **{n.get('name')}** (`{n.get('_id')}`)"
        for n in networks
        if isinstance(n, dict) and n.get("_id")
    ]
    msg = "Private networks:\n" + ("\n".join(lines) if lines else "(none — use ensure_private_network)")
    return ToolResult(True, "OK", msg, r.data)


async def _run_ensure_private_network(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    from app.config import get_settings

    name = (params.get("network_name") or get_settings().liara_network_name or "my-network").strip()
    blocked = _require_confirm(params)
    if blocked:
        return ToolResult(
            False,
            blocked.code,
            f"Will create or reuse private network **{name}**. {blocked.text_for_agent}",
            None,
        )
    r = await liara.create_network(name)
    return ToolResult(r.ok, r.code, r.message, r.data)


async def _run_list_apps(liara: LiaraAdapter, _params: dict[str, Any]) -> ToolResult:
    r = await liara.list_projects()
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    projects = (r.data or {}).get("projects") or []
    lines: list[str] = []
    ids: list[str] = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        pid = p.get("project_id") or p.get("name")
        if not pid:
            continue
        ids.append(str(pid))
        ptype = p.get("type") or p.get("platform") or "?"
        lines.append(f"- **{pid}** ({ptype})")
    scope = "team" if getattr(liara, "_team_id", None) else "personal account"
    team_note = f"\n\n(Scope: **{scope}**" + (
        f", team id `{liara._team_id}`)" if liara._team_id else ".)"
    )
    if not lines:
        return ToolResult(
            True,
            "OK",
            "No apps on this Liara account/team yet." + team_note,
            {"app_ids": []},
        )
    return ToolResult(
        True,
        "OK",
        "Apps on this account:\n" + "\n".join(lines) + team_note,
        {"app_ids": ids, "projects": projects},
    )


async def _run_bind_app(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    app = params.get("app_name") or params.get("app") or params.get("project_id")
    if not app:
        return ToolResult(False, "INVALID_INPUT", "app_name is required", None)
    if not await liara.project_exists(app):
        return ToolResult(
            False,
            "NOT_FOUND",
            f"App **{app}** not found on this Liara account. Use liara.list_apps or liara.create_app.",
            None,
        )
    return ToolResult(
        True,
        "OK",
        f"Active app set to **{app}**. App-scoped tools (logs, settings, deploy, …) will use this id.",
        {"active_app": app, "app_name": app},
    )


async def _run_create_app(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    blocked = _require_confirm(params)
    if blocked:
        return blocked
    app = params.get("app_name") or params.get("app") or params.get("project_id")
    if not app:
        return ToolResult(False, "INVALID_INPUT", "app_name is required", None)
    platform, stack = _resolve_platform_for_create(params)
    if not platform:
        return ToolResult(
            False,
            "PLATFORM_UNKNOWN",
            "Cannot detect stack yet. Run **repo.clone_github** then **repo.prepare_deploy_workspace** "
            "before create_app, or pass `platform` (e.g. python).",
            None,
        )
    app = normalize_liara_app_id(app)
    net_name = (params.get("network_name") or "").strip() or None
    try:
        r = await liara.create_project(app, platform=platform, network_name=net_name)
    except RuntimeError as e:
        return ToolResult(False, "CREATE_APP_FAILED", str(e), None)
    data = dict(r.data or {})
    data["active_app"] = app
    data["app_name"] = app
    data["platform"] = platform
    if stack:
        data["stack"] = stack
    note = f" (platform **{platform}**"
    if stack:
        note += f", stack {stack}"
    note += ")"
    msg = r.message + note if r.ok else r.message
    return ToolResult(r.ok, r.code, msg, data)


async def _run_get_app_url(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    app = params.get("app_name") or params.get("app") or params.get("project_id")
    if not app:
        return ToolResult(False, "INVALID_INPUT", "app_name is required", None)
    r = await liara.resolve_site_url(app)
    if not r.ok:
        return ToolResult(False, r.code, r.message, r.data)
    url = (r.data or {}).get("url", liara.project_url(app))
    return ToolResult(
        True,
        "OK",
        f"Default URL: **{url}**",
        {"url": url, "app_name": app},
    )


async def _run_verify_app_health(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    url = params.get("url") or params.get("health_url")
    app = params.get("app_name") or params.get("app")
    if not url and app:
        url = liara.project_url(app)
    if not url:
        return ToolResult(False, "INVALID_INPUT", "url or app_name required", None)
    base = url.rstrip("/")
    candidates = [base, f"{base}/health"]
    best_status = 0
    best_url = base
    last_data: dict[str, Any] = {}
    for candidate in candidates:
        r = await liara.health_http(candidate)
        if not r.ok:
            continue
        status = int((r.data or {}).get("status_code") or 0)
        last_data = r.data or {}
        if status > best_status or (200 <= status < 400):
            best_status = status
            best_url = candidate
        if 200 <= status < 400:
            break
    ok = 200 <= best_status < 400
    msg = f"Health check **{best_url}** → HTTP {best_status}"
    if not ok:
        msg += (
            "\n\nURL not healthy (502/503 often means crash or wrong port). "
            "Next: **liara.diagnose_deploy_health** for logs and fixes."
        )
    return ToolResult(
        ok,
        "OK" if ok else "UNHEALTHY",
        msg,
        {"url": best_url, "status_code": best_status, "app_name": app, **last_data},
    )


async def _run_diagnose_deploy_health(liara: LiaraAdapter, params: dict[str, Any]) -> ToolResult:
    app = params.get("app_name") or params.get("app")
    url = params.get("url") or params.get("health_url") or (liara.project_url(app) if app else None)
    if not url or not app:
        return ToolResult(False, "INVALID_INPUT", "app_name required", None)
    lines = int(params.get("log_lines") or 60)
    health = await liara.health_http(url)
    status = int((health.data or {}).get("status_code") or 0)
    if status >= 400 or status == 0:
        alt = await liara.health_http(url.rstrip("/") + "/health")
        if alt.ok:
            status = int((alt.data or {}).get("status_code") or status)
    log_r = await liara.logs(str(app), lines=lines)
    log_tail = ""
    if log_r.ok and log_r.data:
        log_tail = (log_r.data.get("logs") or "")[-5000:]
    healthy = 200 <= status < 400
    hints = hints_from_logs(log_tail)
    body = format_diagnosis(url=url, status_code=status or None, log_tail=log_tail, extra_hints=hints)
    return ToolResult(
        healthy,
        "OK" if healthy else "UNHEALTHY",
        body,
        {"url": url, "status_code": status, "logs": log_tail, "hints": hints, "app_name": app},
    )


async def _run_clone_github(
    _liara: LiaraAdapter,
    params: dict[str, Any],
    *,
    on_log: OnLine | None = None,
) -> ToolResult:
    run_id = params.get("run_id")
    repo = params.get("repo")
    if not run_id:
        return ToolResult(False, "INVALID_INPUT", "run_id is required for clone", None)
    if not repo:
        return ToolResult(False, "INVALID_INPUT", "repo (owner/name) is required", None)
    candidates = params.get("github_repos")
    if isinstance(candidates, list):
        repos = [str(x) for x in candidates]
    else:
        repos = None
    try:
        repo = normalize_github_repo(str(repo), repos)
    except ValueError as e:
        return _clone_error_result(e)
    try:
        info = await clone_repository(run_id, repo, on_log=on_log, github_repos=repos)
    except Exception as e:
        return _clone_error_result(e)
    return ToolResult(
        True,
        "OK",
        f"Cloned **{repo}** @ `{info.get('commit', '')[:12]}`",
        {"repo": repo, **info},
    )


async def _run_prepare_deploy_workspace(
    _liara: LiaraAdapter,
    params: dict[str, Any],
) -> ToolResult:
    run_id = params.get("run_id")
    if not run_id:
        return ToolResult(False, "INVALID_INPUT", "run_id is required", None)
    subpath = normalize_subpath(params.get("subpath"))
    repo = params.get("repo") or ""
    app_name = params.get("app_name") or slug_app_name(repo or "app")
    deploy_path = params.get("deploy_path")
    if deploy_path:
        root = Path(str(deploy_path)).resolve()
    else:
        root = deploy_root(run_id, subpath)
    if not root.is_dir():
        return ToolResult(
            False,
            "NOT_FOUND",
            f"Workspace path missing: {root} (clone first?)",
            None,
        )
    analysis = analyze_local_directory(root)
    msg = write_liara_config(root, app_name, analysis)
    stack = analysis.get("stack", "node")
    port = int(analysis.get("deploy_port") or (8000 if stack == "python" else 3000))
    platform = "python" if stack == "python" else ("next" if stack == "next" else "node")
    return ToolResult(
        True,
        "OK",
        f"{msg}. Stack **{stack}**, port **{port}**, deploy_path `{root}`."
        + (f"\n\n{_LIARA_PYTHON_RUNTIME_CHECKLIST}" if stack == "python" else ""),
        {
            "deploy_path": str(root),
            "port": port,
            "stack": stack,
            "platform": platform,
            "app_name": app_name,
            "analysis": analysis,
        },
    )


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
        user_goal=params.get("user_goal"),
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
    "liara.list_networks": _run_list_networks,
    "liara.ensure_private_network": _run_ensure_private_network,
    "liara.list_apps": _run_list_apps,
    "liara.bind_app": _run_bind_app,
    "liara.create_app": _run_create_app,
    "liara.get_app_url": _run_get_app_url,
    "liara.verify_app_health": _run_verify_app_health,
    "liara.diagnose_deploy_health": _run_diagnose_deploy_health,
    "repo.clone_github": _run_clone_github,
    "repo.prepare_deploy_workspace": _run_prepare_deploy_workspace,
    "guide.recommend_setup": _run_guide_recommend_setup,
    "guide.domain_connect_playbook": _run_guide_domain_connect_playbook,
    "liara.check_domain_status": _run_check_domain_status,
    "liara.delete_app": _run_delete_app,
    "liara.deploy_release": _run_deploy_release,
    "liara.delete_app_domain": _run_delete_app_domain,
    "liara.enable_domain_ssl": _run_enable_domain_ssl,
    "liara.disable_domain_ssl": _run_disable_domain_ssl,
    "liara.delete_app_disk": _run_delete_app_disk,
    "liara.resize_app_disk": _run_resize_app_disk,
    "liara.create_disk_backup": _run_create_disk_backup,
    "liara.create_disk_ftp": _run_create_disk_ftp,
    "liara.delete_disk_ftp": _run_delete_disk_ftp,
    "liara.set_read_only_filesystem": _run_set_read_only_filesystem,
    "liara.set_default_subdomain": _run_set_default_subdomain,
    "liara.set_fixed_ip": _run_set_fixed_ip,
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


def tools_for_skill(skill_id: str, active_app: str | None = None) -> list[str]:
    base = list(_SKILL_TOOL_HINTS.get(skill_id, list(TOOLS.keys())))
    if skill_id == "liara.deploy" and active_app:
        seen = set(base)
        for tid in APP_SUBSERVICE_TOOL_IDS:
            if tid not in seen:
                base.append(tid)
                seen.add(tid)
    return base


_SKILL_TOOL_HINTS: dict[str, list[str]] = {
    "liara.balance": ["liara.get_account_balance"],
    "liara.usage": ["liara.get_service_resource_usage"],
    "liara.logs": ["liara.analyze_service_logs"],
    "liara.events": ["liara.list_app_events"],
    "liara.history": ["liara.list_deploy_releases", "liara.deploy_service", "liara.deploy_release"],
    "liara.domains": [
        "liara.list_app_domains",
        "liara.check_domain_status",
        "guide.domain_connect_playbook",
        "liara.create_app_domain",
        "liara.set_domain_redirect",
        "liara.enable_domain_ssl",
        "liara.disable_domain_ssl",
        "liara.delete_app_domain",
    ],
    "liara.bootstrap": [
        "liara.list_apps",
        "repo.clone_github",
        "repo.prepare_deploy_workspace",
        "liara.list_available_plans",
        "liara.get_service_plan_info",
        "guide.recommend_setup",
        "guide.domain_connect_playbook",
        "liara.list_app_domains",
        "liara.check_domain_status",
    ],
    "liara.disks": [
        "liara.list_app_disks",
        "liara.create_app_disk",
        "liara.delete_app_disk",
        "liara.resize_app_disk",
        "liara.create_disk_backup",
        "liara.create_disk_ftp",
        "liara.delete_disk_ftp",
    ],
    "liara.settings": [
        "liara.get_app_settings",
        "liara.update_env_variables",
        "liara.set_zero_downtime",
        "liara.restart_service",
        "liara.set_read_only_filesystem",
        "liara.set_default_subdomain",
        "liara.set_fixed_ip",
    ],
    "liara.resize": [
        "liara.get_service_plan_info",
        "liara.list_available_plans",
        "liara.change_service_plan",
        "liara.scale_service",
    ],
    "liara.health": [
        "liara.bind_app",
        "liara.get_app_url",
        "liara.verify_app_health",
        "liara.diagnose_deploy_health",
        "liara.get_service_resource_usage",
    ],
    "incident.diagnose": [
        "liara.bind_app",
        "liara.get_app_url",
        "liara.verify_app_health",
        "liara.diagnose_deploy_health",
        "liara.analyze_service_logs",
        "liara.get_service_resource_usage",
    ],
    "liara.deploy": [
        "liara.list_networks",
        "liara.ensure_private_network",
        "liara.list_apps",
        "liara.delete_app",
        "liara.bind_app",
        "repo.clone_github",
        "repo.prepare_deploy_workspace",
        "liara.create_app",
        "liara.deploy_service",
        "liara.get_app_url",
        "liara.verify_app_health",
        "liara.diagnose_deploy_health",
        "liara.list_deploy_releases",
    ],
    "agent.summarize": [],
}

# Unlocked for the active app after bind_app / create_app (logs, settings, …).
APP_SUBSERVICE_TOOL_IDS: tuple[str, ...] = (
    "liara.analyze_service_logs",
    "liara.list_app_events",
    "liara.list_deploy_releases",
    "liara.list_app_domains",
    "liara.list_app_disks",
    "liara.get_app_settings",
    "liara.get_service_resource_usage",
    "liara.restart_service",
    "liara.update_env_variables",
)


async def execute_tool(
    tool_id: str,
    *,
    liara: LiaraAdapter,
    params: dict[str, Any] | None = None,
    run_id: str | None = None,
    on_log: OnLine | None = None,
) -> ToolResult:
    if tool_id not in TOOLS:
        return ToolResult(False, "UNKNOWN_TOOL", f"Unknown tool: {tool_id}", None)
    handler = _HANDLERS.get(tool_id)
    if not handler:
        return ToolResult(False, "NO_HANDLER", f"No handler for {tool_id}", None)
    p = dict(params or {})
    if run_id and not p.get("run_id"):
        p["run_id"] = run_id
    if tool_id in ("repo.clone_github", "liara.deploy_service"):
        return await handler(liara, p, on_log=on_log)
    return await handler(liara, p)
