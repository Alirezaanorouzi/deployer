import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.http_client import async_client
from app.liara.cli import LiaraCli, OnLine
from app.liara.projects import project_ids_from_list_payload, resolve_project_id
from app.liara.redact import redact, sanitize_liara_project
from app.liara.log_analysis import _tail_lines, analyze_service_logs
from app.liara.log_range import (
    entries_to_text,
    filter_log_entries,
    resolve_log_time_range,
)
from app.liara.platform import (
    disks_from_api_payload,
    events_window_from_params,
    format_disks_text,
    format_domains_text,
    format_events_text,
    format_plan_text,
    format_releases_text,
    format_settings_text,
    liara_error_message,
    normalize_disk_name,
    parse_disk_size_gb,
    plan_snapshot,
    sanitize_domain,
    sanitize_event,
    sanitize_release,
    settings_snapshot,
)
from app.liara.create_app import (
    normalize_liara_app_id,
    validate_app_id,
    validate_platform,
)
from app.liara.usage import build_usage_summary, usage_summary_text


@dataclass
class AdapterResult:
    ok: bool
    code: str
    message: str
    data: dict[str, Any] | None = None


class LiaraAdapter:
    def __init__(self, api_token: str | None = None, run_id: str | None = None) -> None:
        settings = get_settings()
        self._token = api_token or settings.liara_api_token
        self._base = settings.liara_api_base.rstrip("/")
        self._team_id = (settings.liara_team_id or "").strip() or None
        self._cli = LiaraCli(self._token, team_id=self._team_id)
        self._run_id = run_id

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _team_query(self, *, enabled: bool = True) -> dict[str, str]:
        if enabled and self._team_id:
            return {"teamID": self._team_id}
        return {}

    def _team_context(self) -> bool:
        return bool(self._team_id)

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        team_scope: bool = True,
    ) -> httpx.Response:
        merged = {**(params or {}), **self._team_query(enabled=team_scope)}
        async with async_client(timeout=45) as client:
            return await client.get(f"{self._base}{path}", headers=self._headers(), params=merged)

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        team_scope: bool = True,
    ) -> httpx.Response:
        headers = {**self._headers(), "Content-Type": "application/json"}
        payload = dict(body or {})
        params = self._team_query(enabled=team_scope)
        async with async_client(timeout=90) as client:
            return await client.post(
                f"{self._base}{path}",
                headers=headers,
                json=payload,
                params=params,
            )

    async def _delete(
        self,
        path: str,
        *,
        team_scope: bool = True,
    ) -> httpx.Response:
        params = self._team_query(enabled=team_scope)
        async with async_client(timeout=90) as client:
            return await client.delete(
                f"{self._base}{path}",
                headers=self._headers(),
                params=params,
            )

    async def _post_empty(
        self,
        path: str,
        *,
        team_scope: bool = True,
    ) -> httpx.Response:
        headers = {**self._headers(), "Content-Type": "application/json"}
        params = self._team_query(enabled=team_scope)
        async with async_client(timeout=90) as client:
            return await client.post(
                f"{self._base}{path}",
                headers=headers,
                json={},
                params=params,
            )

    @staticmethod
    def _toggle_path(enabled: bool) -> str:
        return "enable" if enabled else "disable"

    async def resolve_domain_id(
        self,
        *,
        domain_id: str | None = None,
        domain_name: str | None = None,
    ) -> tuple[str | None, str | None]:
        dom_id = (domain_id or "").strip()
        name = (domain_name or "").strip().lower()
        if dom_id:
            return dom_id, name or None
        if not name:
            return None, None
        listed = await self._get("/v1/domains")
        if listed.status_code >= 400:
            return None, name
        body = listed.json() if listed.content else {}
        for d in body.get("domains") or []:
            if isinstance(d, dict) and str(d.get("name") or "").lower() == name:
                return str(d.get("_id") or d.get("id") or ""), name
        return None, name

    def _pick_credit_toman(self, body: dict[str, Any]) -> int | float | None:
        user = body.get("user")
        if isinstance(user, dict):
            for key in ("balance", "credit", "remainingCredit"):
                val = user.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        pass
        for key in ("credit", "balance", "remainingCredit", "accountCredit", "creditAmount"):
            val = body.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        billing = body.get("billing")
        if isinstance(billing, dict):
            return self._pick_credit_toman(billing)
        return None

    async def get_billing(self) -> AdapterResult:
        if not self._token:
            return AdapterResult(False, "AUTH_MISSING", "LIARA_API_TOKEN is not configured")
        try:
            r = await self._get("/v1/billing")
            if r.status_code == 401:
                return AdapterResult(False, "AUTH_FAILED", "Invalid Liara API token")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Billing API returned {r.status_code}")
            body = r.json() if r.content else {}
            if not isinstance(body, dict):
                body = {}
            credit = self._pick_credit_toman(body)
            user = body.get("user") if isinstance(body.get("user"), dict) else {}
            safe = {
                "credit_toman": int(credit) if credit is not None and credit == int(credit) else credit,
                "account_type": user.get("accountType") or body.get("accountType") or body.get("account_type"),
                "min_credit_alert_toman": user.get("minCreditAmount") or body.get("minCreditAmount"),
                "low_credit_alert_enabled": user.get("isManualMinCredit") or body.get("isManualMinCredit"),
            }
            msg = (
                f"Account credit: {safe['credit_toman']} Toman"
                if safe["credit_toman"] is not None
                else "Billing info retrieved"
            )
            return AdapterResult(True, "OK", msg, safe)
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def get_free_credit(self) -> AdapterResult:
        try:
            r = await self._get("/v1/me/free-credit")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Free credit API returned {r.status_code}")
            body = r.json() if r.content else {}
            remaining = body.get("remainingFreeCredit") if isinstance(body, dict) else None
            return AdapterResult(
                True,
                "OK",
                "Free credit loaded",
                {"remaining_free_credit": remaining},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def get_account_balance(self) -> AdapterResult:
        billing = await self.get_billing()
        if not billing.ok:
            return billing
        data = dict(billing.data or {})
        free = await self.get_free_credit()
        if free.ok and free.data:
            data.update(free.data)
        return AdapterResult(True, "OK", billing.message, data)

    async def auth_check(self) -> AdapterResult:
        if not self._token:
            return AdapterResult(False, "AUTH_MISSING", "LIARA_API_TOKEN is not configured")
        try:
            r = await self._get("/v1/me")
            if r.status_code == 401:
                return AdapterResult(False, "AUTH_FAILED", "Invalid Liara API token")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Liara API returned {r.status_code}")
            body = r.json() if r.content else {}
            user = (body.get("user") or {}) if isinstance(body, dict) else {}
            safe = {"user_id": user.get("_id"), "email": user.get("email"), "isVerified": user.get("isVerified")}
            return AdapterResult(True, "OK", "Authenticated", {"me": safe})
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def list_projects(self) -> AdapterResult:
        try:
            r = await self._get("/v1/projects")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Liara API returned {r.status_code}")
            data = r.json()
            projects = data.get("projects", data) if isinstance(data, dict) else data
            return AdapterResult(True, "OK", "Projects listed", {"projects": projects})
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def project_exists(self, project_id: str) -> bool:
        r = await self.get_project(project_id)
        return r.ok

    async def get_project(self, project_id: str) -> AdapterResult:
        try:
            r = await self._get(f"/v1/projects/{project_id}")
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", f"Project {project_id} not found")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Liara API returned {r.status_code}")
            raw = r.json()
            project = raw.get("project", raw) if isinstance(raw, dict) else raw
            safe = sanitize_liara_project(project) if isinstance(project, dict) else project
            return AdapterResult(True, "OK", "Project loaded", {"project": safe})
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    def project_url(self, project_id: str) -> str:
        return f"https://{project_id}.liara.run"

    async def resolve_site_url(self, project_id: str) -> AdapterResult:
        url = self.project_url(project_id)
        proj = await self.get_project(project_id)
        if proj.ok:
            return AdapterResult(True, "OK", "Site URL resolved", {"url": url, "project": proj.data})
        return AdapterResult(True, "OK", "Site URL (default subdomain)", {"url": url})

    async def logs_api(
        self,
        project_id: str,
        *,
        since: int,
        until: int | None = None,
    ) -> AdapterResult:
        try:
            r = await self._get(
                f"/v1/projects/{project_id}/logs",
                params={"since": str(since)},
            )
            if r.status_code >= 400:
                return AdapterResult(False, "LOGS_FAILED", f"Logs API returned {r.status_code}")
            raw = r.json()
            entries = raw if isinstance(raw, list) else []
            filtered = filter_log_entries(entries, since, until)
            text = entries_to_text(filtered)
            return AdapterResult(
                True,
                "OK",
                "Logs from API",
                {
                    "logs": redact(text),
                    "source": "api",
                    "since": since,
                    "until": until,
                    "entry_count": len(filtered),
                },
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def list_networks(self) -> AdapterResult:
        """Private networks visible to the API token (not team-scoped query — team filter returns empty)."""
        try:
            r = await self._get("/v1/networks", team_scope=self._team_context())
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"networks API returned {r.status_code}")
            data = r.json()
            networks = data.get("networks", data) if isinstance(data, dict) else data
            if not isinstance(networks, list):
                networks = []
            scope = "team" if self._team_context() else "account"
            return AdapterResult(
                True,
                "OK",
                f"Found {len(networks)} private network(s) on this {scope}.",
                {"networks": networks, "team_id": self._team_id},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def create_network(self, network_name: str) -> AdapterResult:
        name = (network_name or "").strip() or "my-network"
        try:
            r = await self._post_json(
                "/v1/networks",
                {"networkName": name},
                team_scope=self._team_context(),
            )
            if r.status_code == 409:
                resolved = await self._resolve_network_id(preferred_name=name, allow_create=False)
                return AdapterResult(
                    True,
                    "OK",
                    f"Network **{name}** already exists.",
                    {"network_id": resolved, "network_name": name, "created": False},
                )
            if r.status_code >= 400:
                return AdapterResult(
                    False,
                    "NETWORK_CREATE_FAILED",
                    liara_error_message(r.text or "", r.status_code),
                )
            raw = r.json()
            net = raw.get("network", raw) if isinstance(raw, dict) else {}
            net_id = net.get("_id") if isinstance(net, dict) else None
            return AdapterResult(
                True,
                "OK",
                f"Created private network **{name}**.",
                {"network_id": net_id, "network_name": name, "created": True},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def _resolve_network_id(
        self,
        *,
        preferred_name: str | None = None,
        allow_create: bool = True,
    ) -> str:
        settings = get_settings()
        if settings.liara_network_id.strip():
            return settings.liara_network_id.strip()

        name = (preferred_name or settings.liara_network_name or "my-network").strip()

        team = self._team_context()
        r = await self._get("/v1/networks", team_scope=team)
        if r.status_code >= 400:
            raise RuntimeError(f"networks API returned {r.status_code}")
        data = r.json()
        networks = data.get("networks", data) if isinstance(data, dict) else data
        if not isinstance(networks, list):
            networks = []

        for net in networks:
            if isinstance(net, dict) and net.get("name") == name and net.get("_id"):
                return str(net["_id"])
        if networks:
            first = networks[0]
            if isinstance(first, dict) and first.get("_id"):
                return str(first["_id"])

        if not allow_create:
            raise RuntimeError(f"private network '{name}' not found")

        created = await self.create_network(name)
        if not created.ok:
            raise RuntimeError(created.message)
        net_id = (created.data or {}).get("network_id")
        if net_id:
            return str(net_id)
        r2 = await self._get("/v1/networks", team_scope=team)
        data2 = r2.json()
        nets2 = data2.get("networks", []) if isinstance(data2, dict) else []
        for net in nets2:
            if isinstance(net, dict) and net.get("name") == name and net.get("_id"):
                return str(net["_id"])
        raise RuntimeError("could not resolve network id after create")

    async def create_project(
        self,
        app: str,
        platform: str = "python",
        on_line: OnLine | None = None,
        run_id: str | None = None,
        *,
        network_name: str | None = None,
    ) -> AdapterResult:
        app_id = normalize_liara_app_id(app)
        plat = (platform or "").strip().lower()
        id_err = validate_app_id(app_id)
        if id_err:
            return AdapterResult(False, "INVALID_APP_ID", id_err, {"app": app_id})
        plat_err = validate_platform(plat)
        if plat_err:
            return AdapterResult(False, "INVALID_PLATFORM", plat_err, {"platform": plat})

        if await self.project_exists(app_id):
            return AdapterResult(
                True,
                "OK",
                f"App {app_id} already exists",
                {"app": app_id, "app_name": app_id, "platform": plat, "created": False},
            )
        if on_line:
            await on_line(f"Creating Liara app {app_id} (platform={plat}) via API…")
        try:
            net_pref = (network_name or get_settings().liara_network_name or "my-network").strip()
            network_id = await self._resolve_network_id(preferred_name=net_pref)
            body = {
                "name": app_id,
                "platform": plat,
                "planID": "small-g2",
                "bundlePlanID": "basic",
                "network": network_id,
            }
            r = await self._post_json("/v1/projects", body, team_scope=self._team_context())
            if r.status_code >= 400:
                text = redact(r.text)
                if "exist" in text.lower():
                    return AdapterResult(
                        True,
                        "OK",
                        f"App {app_id} already exists",
                        {"app": app_id, "platform": plat, "created": False},
                    )
                return AdapterResult(
                    False,
                    "CREATE_APP_FAILED",
                    liara_error_message(text, r.status_code),
                    {"response": text[:500], "team_id": self._team_id},
                )
            project_id = None
            try:
                data = r.json()
                project_id = data.get("projectID") or data.get("project_id")
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
            return AdapterResult(
                True,
                "OK",
                f"Created Liara app **{app_id}** with platform **{plat}**"
                + (f" in team `{self._team_id}`." if self._team_id else " on your account."),
                {
                    "app": app_id,
                    "app_name": app_id,
                    "platform": plat,
                    "created": True,
                    "project_id": project_id,
                    "team_id": self._team_id,
                    "account_scope": "team" if self._team_id else "personal",
                },
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))
        except RuntimeError as e:
            return AdapterResult(False, "CREATE_APP_FAILED", str(e))

    async def deploy(
        self,
        app: str,
        path: str = ".",
        port: int = 3000,
        on_line: OnLine | None = None,
        run_id: str | None = None,
    ) -> AdapterResult:
        deploy_cwd = path if path and path != "." else None
        res = await self._cli.run(
            "deploy",
            "--app",
            app,
            "--port",
            str(port),
            "--path",
            ".",
            on_line=on_line,
            sandbox_run_id=None,
            cwd=deploy_cwd,
            timeout=1800,
        )
        if res.exit_code == 124:
            return AdapterResult(False, "BUILD_TIMEOUT", res.stderr or res.stdout)
        if not res.ok:
            return AdapterResult(
                False,
                "DEPLOY_FAILED",
                redact(res.stderr or res.stdout),
                {"stdout": res.stdout, "stderr": res.stderr},
            )
        return AdapterResult(True, "OK", "Deploy finished", {"stdout": res.stdout})

    async def logs(
        self,
        app: str,
        lines: int = 80,
        *,
        since: int | None = None,
        until: int | None = None,
    ) -> AdapterResult:
        since_ts = since if since is not None else int(time.time()) - 3600
        api = await self.logs_api(app, since=since_ts, until=until)
        if api.ok and (api.data or {}).get("logs", "").strip():
            return api
        res = await self._cli.run("logs", "--app", app, "--lines", str(lines))
        if not res.ok:
            return AdapterResult(False, "LOGS_FAILED", res.stderr or res.stdout)
        return AdapterResult(
            True,
            "OK",
            "Logs retrieved",
            {
                "logs": res.stdout,
                "source": "cli",
                "since": since_ts,
                "until": until,
            },
        )

    async def health_http(self, url: str) -> AdapterResult:
        try:
            async with async_client(timeout=30, follow_redirects=True) as client:
                r = await client.get(url)
            ok = r.status_code < 500
            return AdapterResult(
                ok,
                "OK" if ok else "VERIFY_FAILED",
                f"HTTP {r.status_code}",
                {"status_code": r.status_code, "latency_ms": r.elapsed.total_seconds() * 1000},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "HEALTH_FAILED", str(e))

    async def list_project_ids(self) -> list[str]:
        listed = await self.list_projects()
        if not listed.ok:
            return []
        return project_ids_from_list_payload(listed.data)

    async def resolve_project(self, hint: str | None) -> tuple[str | None, str | None]:
        return resolve_project_id(hint, await self.list_project_ids())

    async def get_metric_reports(
        self,
        project_id: str,
        metric: str,
        *,
        since_hours: float = 1.0,
    ) -> AdapterResult:
        if metric not in ("cpu", "memory"):
            return AdapterResult(False, "INVALID_METRIC", f"Unknown metric: {metric}")
        if not self._token:
            return AdapterResult(False, "AUTH_MISSING", "LIARA_API_TOKEN is not configured")
        since_hours = max(0.25, min(float(since_hours), 168.0))
        since_ts = int(time.time() - since_hours * 3600)
        try:
            async with async_client(timeout=45) as client:
                r = await client.get(
                    f"{self._base}/v1/projects/{project_id}/metrics/{metric}",
                    headers=self._headers(),
                    params={"since": since_ts},
                )
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", f"App {project_id} not found or metrics unavailable")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Metrics API returned {r.status_code}")
            body = r.json() if r.content else {}
            return AdapterResult(True, "OK", f"{metric} metrics loaded", body if isinstance(body, dict) else {})
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def get_service_logs(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        lines: int = 120,
        time_range: dict[str, Any] | None = None,
    ) -> AdapterResult:
        hint = project_id or app_name
        resolved, err = await self.resolve_project(hint)
        if not resolved:
            return AdapterResult(False, "PROJECT_REQUIRED", err or "project_id required")

        range_params = dict(time_range or {})
        if project_id:
            range_params.setdefault("project_id", project_id)
        if app_name:
            range_params.setdefault("app_name", app_name)
        since, until, window_label = resolve_log_time_range(range_params)

        lines = max(10, min(int(lines), 400))
        fetched = await self.logs(resolved, lines=lines, since=since, until=until)
        if not fetched.ok:
            return fetched

        raw = (fetched.data or {}).get("logs", "") or ""
        text = _tail_lines(redact(raw), lines)
        source = (fetched.data or {}).get("source", "api")
        if not text.strip():
            return AdapterResult(
                False,
                "LOGS_EMPTY",
                f"No log lines for **{resolved}** in window ({window_label}). Try a wider range (e.g. since_hours: 24).",
            )

        return AdapterResult(
            True,
            "OK",
            f"Retrieved logs for {resolved} ({len(text.splitlines())} lines, {window_label}, {source})",
            {
                "project_id": resolved,
                "logs": text,
                "line_count": len(text.splitlines()),
                "source": source,
                "since": since,
                "until": until,
                "window_label": window_label,
            },
        )

    async def get_and_analyze_service_logs(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        lines: int = 120,
        focus: str | None = None,
        time_range: dict[str, Any] | None = None,
        user_goal: str | None = None,
    ) -> AdapterResult:
        got = await self.get_service_logs(
            project_id=project_id,
            app_name=app_name,
            lines=lines,
            time_range=time_range,
        )
        if not got.ok:
            return got

        data = got.data or {}
        pid = data.get("project_id", project_id or app_name or "app")
        window = data.get("window_label", "selected window")
        focus_text = focus or "general health and errors"
        analysis = await analyze_service_logs(
            project_id=str(pid),
            logs=data.get("logs", ""),
            user_focus=f"{focus_text} (time window: {window})",
            user_goal=user_goal,
        )
        preview_lines = min(8, data.get("line_count", 8))
        log_preview = "\n".join((data.get("logs") or "").splitlines()[-preview_lines:])
        message = (
            f"**{pid}** — logs ({data.get('line_count')} lines, **{window}**, {data.get('source')})\n\n"
            f"**Analysis**\n{analysis}\n\n"
            f"**Recent lines**\n```\n{log_preview}\n```"
        )
        return AdapterResult(
            True,
            "OK",
            message,
            {**data, "analysis": analysis},
        )

    async def get_service_resource_usage(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        since_hours: float = 1.0,
    ) -> AdapterResult:
        hint = project_id or app_name
        resolved, err = await self.resolve_project(hint)
        if not resolved:
            return AdapterResult(False, "PROJECT_REQUIRED", err or "project_id required")

        proj = await self.get_project(resolved)
        if not proj.ok:
            return AdapterResult(False, proj.code, proj.message)

        meta = (proj.data or {}).get("project") or {}
        cpu = await self.get_metric_reports(resolved, "cpu", since_hours=since_hours)
        if not cpu.ok:
            return cpu
        mem = await self.get_metric_reports(resolved, "memory", since_hours=since_hours)
        if not mem.ok:
            return mem

        summary = build_usage_summary(
            resolved,
            project_meta=meta,
            cpu=cpu.data or {},
            memory=mem.data or {},
            window_hours=since_hours,
        )
        return AdapterResult(
            True,
            "OK",
            usage_summary_text(summary),
            summary,
        )

    async def metrics(self, project_id: str) -> AdapterResult:
        proj = await self.get_project(project_id)
        if not proj.ok or not proj.data:
            return AdapterResult(
                False,
                "API_NOT_AVAILABLE",
                proj.message,
                {"fallback": "logs"},
            )
        p = proj.data.get("project") or {}
        return AdapterResult(
            True,
            "OK",
            "Project metadata",
            {
                "project_id": p.get("project_id"),
                "type": p.get("type"),
                "status": p.get("status"),
                "planID": p.get("planID"),
                "scale": p.get("scale"),
            },
        )

    async def _require_project(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> tuple[str | None, AdapterResult | None]:
        hint = project_id or app_name
        resolved, err = await self.resolve_project(hint)
        if not resolved:
            return None, AdapterResult(False, "PROJECT_REQUIRED", err or "project_id required")
        return resolved, None

    async def _project_internal_id(self, project_slug: str) -> str | None:
        proj = await self.get_project(project_slug)
        if not proj.ok:
            return None
        meta = (proj.data or {}).get("project") or {}
        if isinstance(meta, dict):
            internal = meta.get("_id")
            return str(internal) if internal else None
        return None

    async def list_app_events(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        since_hours: float | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
        search: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        params: dict[str, Any] = {}
        effective_since = since_hours
        if effective_since is None and start_ts is None:
            effective_since = 168.0
        range_params = {
            "since_hours": effective_since,
            "start_ts": start_ts,
            "since_ts": start_ts,
            "end_ts": end_ts,
            "until_ts": end_ts,
        }
        start, end = events_window_from_params(range_params)
        window = "last 7 days (default)"
        if start_ts is not None and start is not None:
            params["start"] = start
            params["end"] = end
            window = f"since {start} to {end}"
        elif start is not None and end is not None:
            params["start"] = start
            params["end"] = end
            window = f"last {effective_since}h" if effective_since is not None else window
        if search:
            params["search"] = search.strip()[:200]
        try:
            r = await self._get(f"/v1/projects/{resolved}/events", params=params or None)
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", f"App {resolved} not found")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Events API returned {r.status_code}")
            body = r.json() if r.content else {}
            raw = body.get("data") if isinstance(body, dict) else body
            if not isinstance(raw, list):
                raw = []
            events = [sanitize_event(e) for e in raw if isinstance(e, dict)]
            text = format_events_text(resolved, events, window)
            return AdapterResult(
                True,
                "OK",
                text,
                {"project_id": resolved, "events": events, "count": len(events), "window": window},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def list_deploy_releases(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        page: int = 1,
        count: int = 10,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        page = max(1, int(page))
        count = max(1, min(int(count), 20))
        try:
            r = await self._get(
                f"/v1/projects/{resolved}/releases",
                params={"page": page, "count": count},
            )
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", f"App {resolved} not found")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Releases API returned {r.status_code}")
            body = r.json() if r.content else {}
            if not isinstance(body, dict):
                body = {}
            raw_releases = body.get("releases") or []
            releases = [sanitize_release(x) for x in raw_releases if isinstance(x, dict)]
            total = int(body.get("total") or len(releases))
            text = format_releases_text(
                resolved,
                total=total,
                current_release=body.get("currentRelease"),
                ready_count=body.get("readyReleasesCount"),
                releases=releases,
                page=page,
            )
            return AdapterResult(
                True,
                "OK",
                text,
                {
                    "project_id": resolved,
                    "releases": releases,
                    "total": total,
                    "page": page,
                    "current_release": body.get("currentRelease"),
                    "ready_releases_count": body.get("readyReleasesCount"),
                },
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def list_app_domains(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        try:
            r = await self._get("/v1/domains", params={"project": resolved})
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Domains API returned {r.status_code}")
            body = r.json() if r.content else {}
            raw = body.get("domains") if isinstance(body, dict) else []
            if not isinstance(raw, list):
                raw = []
            domains = [sanitize_domain(d) for d in raw if isinstance(d, dict)]
            text = format_domains_text(resolved, domains)
            return AdapterResult(
                True,
                "OK",
                text,
                {"project_id": resolved, "domains": domains, "total": len(domains)},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def check_domain_status(
        self,
        *,
        domain_id: str | None = None,
        domain_name: str | None = None,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        """GET /v1/domains/{id}/check — DNS/SSL verification status."""
        dom_id = (domain_id or "").strip()
        name = (domain_name or "").strip().lower()
        if not dom_id and name:
            listed = await self.list_app_domains(project_id=project_id, app_name=app_name)
            if not listed.ok:
                return listed
            for d in (listed.data or {}).get("domains") or []:
                if isinstance(d, dict) and str(d.get("name") or "").lower() == name:
                    dom_id = str(d.get("id") or d.get("_id") or "")
                    break
            if not dom_id:
                all_dom = await self._get("/v1/domains")
                if all_dom.status_code == 200:
                    body = all_dom.json() if all_dom.content else {}
                    for d in body.get("domains") or []:
                        if isinstance(d, dict) and str(d.get("name") or "").lower() == name:
                            dom_id = str(d.get("_id") or d.get("id") or "")
                            break
        if not dom_id:
            return AdapterResult(
                False,
                "DOMAIN_REQUIRED",
                "Provide domain_id or domain_name (must already exist on Liara).",
            )
        try:
            r = await self._get(f"/v1/domains/{dom_id}/check")
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", "Domain not found on Liara.")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Domain check returned {r.status_code}")
            body = r.json() if r.content else {}
            dom = body.get("domain") if isinstance(body, dict) else body
            if not isinstance(dom, dict):
                dom = {}
            status = dom.get("status") or "unknown"
            cert = dom.get("certificatesStatus") or dom.get("certificates_status")
            cname = dom.get("CNameRecord")
            lines = [
                f"Domain **{dom.get('name') or name}**",
                f"- status: **{status}**",
                f"- certificates: **{cert or 'n/a'}**",
            ]
            if cname:
                lines.append(f"- CNAME hint: `{cname}`")
            return AdapterResult(
                True,
                "OK",
                "\n".join(lines),
                {"domain": dom, "domain_id": dom_id},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def list_app_disks(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        try:
            internal_id = await self._project_internal_id(resolved)
            path_ids = [internal_id, resolved] if internal_id and internal_id != resolved else [resolved]
            last_status = 0
            last_text = ""
            body: dict[str, Any] | list | None = None
            for pid in path_ids:
                if not pid:
                    continue
                r = await self._get(f"/v1/projects/{pid}/disks")
                last_status = r.status_code
                last_text = r.text or ""
                if r.status_code == 200:
                    body = r.json() if r.content else {}
                    break
            if body is None:
                msg = liara_error_message(last_text, last_status)
                return AdapterResult(
                    False,
                    "DISKS_UNAVAILABLE",
                    f"**{resolved}** — could not list disks. {msg}",
                    {"project_id": resolved, "disks": [], "api_status": last_status},
                )
            disks = disks_from_api_payload(body if isinstance(body, (dict, list)) else {})
            text = format_disks_text(resolved, disks)
            return AdapterResult(
                True,
                "OK",
                text,
                {"project_id": resolved, "disks": disks, "count": len(disks)},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def get_app_settings(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        proj = await self.get_project(resolved)
        if not proj.ok:
            return proj
        meta = (proj.data or {}).get("project") or {}
        if not isinstance(meta, dict):
            return AdapterResult(False, "API_ERROR", "Invalid project payload")
        snap = settings_snapshot(meta)
        return AdapterResult(True, "OK", format_settings_text(snap), {"project_id": resolved, "settings": snap})

    async def get_service_plan_info(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        proj = await self.get_project(resolved)
        if not proj.ok:
            return proj
        meta = (proj.data or {}).get("project") or {}
        if not isinstance(meta, dict):
            return AdapterResult(False, "API_ERROR", "Invalid project payload")
        snap = plan_snapshot(meta)
        return AdapterResult(True, "OK", format_plan_text(snap), {"project_id": resolved, "plan": snap})

    async def list_available_plans(self) -> AdapterResult:
        if not self._token:
            return AdapterResult(False, "AUTH_MISSING", "LIARA_API_TOKEN is not configured")
        res = await self._cli.run("plan", "list", "--output", "json")
        if not res.ok:
            return AdapterResult(False, "CLI_ERROR", redact(res.stderr or res.stdout))
        try:
            plans = json.loads(res.stdout)
        except json.JSONDecodeError:
            return AdapterResult(False, "PARSE_ERROR", "Could not parse plan list from Liara CLI")
        if not isinstance(plans, list):
            plans = []
        slim = []
        for p in plans[:40]:
            if not isinstance(p, dict):
                continue
            slim.append(
                {
                    "plan_id": p.get("Plan") or p.get("planID") or p.get("id"),
                    "ram_gb": p.get("RAM"),
                    "cpu": p.get("CPU"),
                    "price": p.get("Price") or p.get("hourlyPrice"),
                }
            )
        lines = ["**Available Liara plans** (use `plan_id` with change_service_plan):"]
        for p in slim[:15]:
            lines.append(f"- **{p.get('plan_id')}** — CPU {p.get('cpu')}, RAM {p.get('ram_gb')} GB")
        return AdapterResult(True, "OK", "\n".join(lines), {"plans": slim})

    async def restart_service(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        try:
            r = await self._post_json(f"/v1/projects/{resolved}/actions/restart")
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", f"App {resolved} not found")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Restart failed ({r.status_code})")
            return AdapterResult(
                True,
                "OK",
                f"Restart requested for **{resolved}**. It may take a minute to become healthy again.",
                {"project_id": resolved},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def scale_service(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        scale: int = 1,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        scale = max(0, min(int(scale), 32))
        try:
            r = await self._post_json(f"/v1/projects/{resolved}/actions/scale", {"scale": scale})
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Scale failed ({r.status_code})")
            action = "stopped" if scale == 0 else f"scaled to **{scale}** replica(s)"
            return AdapterResult(True, "OK", f"**{resolved}** {action}.", {"project_id": resolved, "scale": scale})
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def change_service_plan(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        plan_id: str,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        plan_id = (plan_id or "").strip()
        if not plan_id:
            return AdapterResult(False, "INVALID_INPUT", "plan_id is required")
        try:
            r = await self._post_json(f"/v1/projects/{resolved}/resize", {"planID": plan_id})
            if r.status_code >= 400:
                body = r.text[:300] if r.text else ""
                return AdapterResult(False, "API_ERROR", f"Plan change failed ({r.status_code}): {body}")
            return AdapterResult(
                True,
                "OK",
                f"Plan change to **{plan_id}** submitted for **{resolved}** (billing may change immediately).",
                {"project_id": resolved, "plan_id": plan_id},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def update_env_variables(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        variables: list[dict[str, str]],
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        if not variables:
            return AdapterResult(False, "INVALID_INPUT", "No variables to update")
        try:
            r = await self._post_json(
                "/v1/projects/update-envs",
                {"project": resolved, "variables": variables},
            )
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Env update failed ({r.status_code})")
            keys = ", ".join(v["key"] for v in variables)
            return AdapterResult(
                True,
                "OK",
                f"Updated env vars on **{resolved}**: {keys} (triggers redeploy).",
                {"project_id": resolved, "keys": [v["key"] for v in variables]},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def set_zero_downtime(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        enabled: bool,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        status = "enable" if enabled else "disable"
        try:
            r = await self._post_json(f"/v1/projects/{resolved}/zero-downtime/{status}")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Zero-downtime update failed ({r.status_code})")
            state = "enabled" if enabled else "disabled"
            return AdapterResult(
                True,
                "OK",
                f"Zero-downtime deploys **{state}** for **{resolved}**.",
                {"project_id": resolved, "zero_downtime": enabled},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def create_app_domain(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        domain_name: str,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        name = (domain_name or "").strip().lower()
        if not name:
            return AdapterResult(False, "INVALID_INPUT", "domain_name is required")
        try:
            r = await self._post_json(
                "/v1/domains",
                {"name": name, "project": resolved, "type": "PROJECT"},
            )
            if r.status_code == 409:
                return AdapterResult(False, "CONFLICT", f"Domain {name} already exists")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Create domain failed ({r.status_code})")
            body = r.json() if r.content else {}
            dom_id = (body.get("domain") or {}).get("_id") if isinstance(body, dict) else None
            return AdapterResult(
                True,
                "OK",
                f"Domain **{name}** created for **{resolved}**. Configure DNS (CNAME) in Liara console if needed.",
                {"project_id": resolved, "domain": name, "domain_id": dom_id},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def set_domain_redirect(
        self,
        *,
        domain_id: str | None = None,
        domain_name: str | None = None,
        redirect_to: str,
        redirect_status: int = 301,
    ) -> AdapterResult:
        dom_id = (domain_id or "").strip()
        if not dom_id and domain_name:
            listed = await self._get("/v1/domains")
            if listed.status_code >= 400:
                return AdapterResult(False, "API_ERROR", "Could not list domains")
            body = listed.json() if listed.content else {}
            for d in body.get("domains") or []:
                if isinstance(d, dict) and (d.get("name") or "").lower() == domain_name.strip().lower():
                    dom_id = d.get("_id") or ""
                    break
        if not dom_id:
            return AdapterResult(False, "NOT_FOUND", "domain_id or known domain_name required")
        status = 301 if int(redirect_status) == 301 else 302
        try:
            r = await self._post_json(
                f"/v1/domains/{dom_id}/set-redirect",
                {"redirectTo": redirect_to, "redirectStatus": status},
            )
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Redirect update failed ({r.status_code})")
            return AdapterResult(
                True,
                "OK",
                f"Redirect set to **{redirect_to}** ({status}).",
                {"domain_id": dom_id, "redirect_to": redirect_to, "redirect_status": status},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def create_app_disk(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        disk_name: str,
        size_gb: str | int,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        name, name_err = normalize_disk_name(disk_name)
        if name_err or not name:
            return AdapterResult(False, "INVALID_DISK_NAME", name_err or "invalid disk name")
        size, size_err = parse_disk_size_gb(size_gb)
        if size_err or size is None:
            return AdapterResult(False, "INVALID_INPUT", size_err or "invalid size")
        try:
            r = await self._post_json(
                f"/v1/projects/{resolved}/disks",
                {"name": name, "size": size},
            )
            if r.status_code >= 400:
                return AdapterResult(
                    False,
                    "DISK_CREATE_FAILED",
                    liara_error_message(r.text or "", r.status_code),
                    {"project_id": resolved, "disk_name": name, "size_gb": size},
                )
            return AdapterResult(
                True,
                "OK",
                f"Disk **{name}** ({size} GB) created for **{resolved}**. Mount it in app settings if needed.",
                {"project_id": resolved, "disk_name": name, "size_gb": size},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def deploy_service_from_path(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        path: str = ".",
        port: int = 3000,
        on_line: OnLine | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        return await self.deploy(resolved, path=path, port=port, on_line=on_line)

    async def delete_app(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        try:
            r = await self._delete(f"/v1/projects/{resolved}")
            if r.status_code in (200, 204):
                return AdapterResult(True, "OK", f"App **{resolved}** deleted.", {"project_id": resolved})
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", f"App {resolved} not found")
            return AdapterResult(False, "API_ERROR", f"Delete app failed ({r.status_code})")
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def deploy_release(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        source_id: str,
        port: int | None = None,
        platform_type: str | None = None,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        source_id = (source_id or "").strip()
        if not source_id:
            return AdapterResult(False, "INVALID_INPUT", "source_id (release/source ID) is required")
        body: dict[str, Any] = {"sourceID": source_id}
        if port is not None:
            body["port"] = int(port)
        if platform_type:
            body["type"] = platform_type.strip()
        try:
            r = await self._post_json(f"/v2/projects/{resolved}/releases", body)
            if r.status_code >= 400:
                return AdapterResult(
                    False,
                    "API_ERROR",
                    liara_error_message(r.text or "", r.status_code),
                )
            payload = r.json() if r.content else {}
            release_id = payload.get("releaseID") if isinstance(payload, dict) else None
            return AdapterResult(
                True,
                "OK",
                f"Release deploy submitted for **{resolved}**"
                + (f" (releaseID `{release_id}`)." if release_id else "."),
                {"project_id": resolved, "release_id": release_id, "source_id": source_id},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def delete_app_domain(
        self,
        *,
        domain_id: str | None = None,
        domain_name: str | None = None,
    ) -> AdapterResult:
        dom_id, name = await self.resolve_domain_id(domain_id=domain_id, domain_name=domain_name)
        if not dom_id:
            return AdapterResult(False, "NOT_FOUND", "domain_id or known domain_name required")
        try:
            r = await self._delete(f"/v1/domains/{dom_id}")
            if r.status_code in (200, 204):
                label = name or dom_id
                return AdapterResult(True, "OK", f"Domain **{label}** deleted.", {"domain_id": dom_id})
            if r.status_code == 404:
                return AdapterResult(False, "NOT_FOUND", "Domain not found")
            return AdapterResult(False, "API_ERROR", f"Delete domain failed ({r.status_code})")
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def enable_domain_ssl(self, *, domain_name: str) -> AdapterResult:
        name = (domain_name or "").strip().lower()
        if not name:
            return AdapterResult(False, "INVALID_INPUT", "domain_name is required")
        try:
            r = await self._post_json("/v1/domains/provision-ssl-certs", {"domain": name})
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Enable SSL failed ({r.status_code})")
            return AdapterResult(
                True,
                "OK",
                f"SSL provisioning requested for **{name}**. Check domain status after DNS is correct.",
                {"domain_name": name},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def disable_domain_ssl(
        self,
        *,
        domain_id: str | None = None,
        domain_name: str | None = None,
    ) -> AdapterResult:
        dom_id, name = await self.resolve_domain_id(domain_id=domain_id, domain_name=domain_name)
        if not dom_id:
            return AdapterResult(False, "NOT_FOUND", "domain_id or known domain_name required")
        try:
            r = await self._post_empty(f"/v1/domains/{dom_id}/ssl/disable")
            if r.status_code in (200, 204):
                return AdapterResult(
                    True,
                    "OK",
                    f"SSL disabled for **{name or dom_id}**.",
                    {"domain_id": dom_id},
                )
            return AdapterResult(False, "API_ERROR", f"Disable SSL failed ({r.status_code})")
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def delete_app_disk(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        disk_name: str,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        name, name_err = normalize_disk_name(disk_name)
        if name_err or not name:
            return AdapterResult(False, "INVALID_DISK_NAME", name_err or "invalid disk name")
        try:
            r = await self._delete(f"/v1/projects/{resolved}/disks/{name}")
            if r.status_code in (200, 204):
                return AdapterResult(
                    True,
                    "OK",
                    f"Disk **{name}** deleted from **{resolved}**.",
                    {"project_id": resolved, "disk_name": name},
                )
            return AdapterResult(
                False,
                "API_ERROR",
                liara_error_message(r.text or "", r.status_code),
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def resize_app_disk(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        disk_name: str,
        size_gb: str | int,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        name, name_err = normalize_disk_name(disk_name)
        if name_err or not name:
            return AdapterResult(False, "INVALID_DISK_NAME", name_err or "invalid disk name")
        size, size_err = parse_disk_size_gb(size_gb)
        if size_err or size is None:
            return AdapterResult(False, "INVALID_INPUT", size_err or "invalid size")
        try:
            r = await self._post_json(
                f"/v1/projects/{resolved}/disks/{name}/resize",
                {"size": str(size)},
            )
            if r.status_code >= 400:
                return AdapterResult(
                    False,
                    "API_ERROR",
                    liara_error_message(r.text or "", r.status_code),
                )
            return AdapterResult(
                True,
                "OK",
                f"Disk **{name}** resize to **{size} GB** submitted for **{resolved}**.",
                {"project_id": resolved, "disk_name": name, "size_gb": size},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def create_disk_backup(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        disk_name: str,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        name, name_err = normalize_disk_name(disk_name)
        if name_err or not name:
            return AdapterResult(False, "INVALID_DISK_NAME", name_err or "invalid disk name")
        try:
            r = await self._post_empty(f"/v1/projects/{resolved}/disks/{name}/backups")
            if r.status_code >= 400:
                return AdapterResult(
                    False,
                    "API_ERROR",
                    liara_error_message(r.text or "", r.status_code),
                )
            return AdapterResult(
                True,
                "OK",
                f"Backup job started for disk **{name}** on **{resolved}**.",
                {"project_id": resolved, "disk_name": name},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def create_disk_ftp(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        disk_name: str,
        ftp_username: str,
        read_only: bool = False,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        dname, name_err = normalize_disk_name(disk_name)
        if name_err or not dname:
            return AdapterResult(False, "INVALID_DISK_NAME", name_err or "invalid disk name")
        user = (ftp_username or "").strip()
        if not user:
            return AdapterResult(False, "INVALID_INPUT", "ftp_username is required")
        try:
            r = await self._post_json(
                f"/v1/projects/{resolved}/disks/{dname}/ftp",
                {"username": user, "readOnly": bool(read_only)},
            )
            if r.status_code >= 400:
                return AdapterResult(
                    False,
                    "API_ERROR",
                    liara_error_message(r.text or "", r.status_code),
                )
            body = r.json() if r.content else {}
            if not isinstance(body, dict):
                body = {}
            safe = {
                "username": body.get("username"),
                "host": body.get("host"),
                "password": "[redacted — see Liara console]" if body.get("password") else None,
            }
            return AdapterResult(
                True,
                "OK",
                f"FTP created for disk **{dname}** — user **{safe.get('username')}**, host **{safe.get('host')}**. "
                "Password was returned by API; retrieve from Liara console if needed.",
                {"project_id": resolved, "disk_name": dname, "ftp": safe},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def delete_disk_ftp(self, *, ftp_name: str) -> AdapterResult:
        fname = (ftp_name or "").strip()
        if not fname:
            return AdapterResult(False, "INVALID_INPUT", "ftp_name is required")
        try:
            r = await self._delete(f"/v1/ftp/{fname}")
            if r.status_code in (200, 204):
                return AdapterResult(True, "OK", f"FTP **{fname}** deleted.", {"ftp_name": fname})
            return AdapterResult(False, "API_ERROR", f"Delete FTP failed ({r.status_code})")
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def set_read_only_filesystem(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        enabled: bool,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        status = self._toggle_path(enabled)
        try:
            r = await self._post_empty(f"/v1/projects/{resolved}/read-only/{status}")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Read-only toggle failed ({r.status_code})")
            state = "enabled" if enabled else "disabled"
            return AdapterResult(
                True,
                "OK",
                f"Read-only filesystem **{state}** for **{resolved}**.",
                {"project_id": resolved, "read_only": enabled},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def set_default_subdomain(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        enabled: bool,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        status = self._toggle_path(enabled)
        try:
            r = await self._post_empty(f"/v1/projects/{resolved}/default-subdomain/{status}")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Default subdomain toggle failed ({r.status_code})")
            state = "enabled" if enabled else "disabled"
            return AdapterResult(
                True,
                "OK",
                f"Default `*.liara.run` subdomain **{state}** for **{resolved}**.",
                {"project_id": resolved, "default_subdomain": enabled},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))

    async def set_fixed_ip(
        self,
        *,
        project_id: str | None = None,
        app_name: str | None = None,
        enabled: bool,
    ) -> AdapterResult:
        resolved, fail = await self._require_project(project_id=project_id, app_name=app_name)
        if fail:
            return fail
        status = self._toggle_path(enabled)
        try:
            r = await self._post_empty(f"/v1/projects/{resolved}/fixed-ip/{status}")
            if r.status_code >= 400:
                return AdapterResult(False, "API_ERROR", f"Fixed IP toggle failed ({r.status_code})")
            body = r.json() if r.content else {}
            ip = body.get("IP") if isinstance(body, dict) else None
            state = "enabled" if enabled else "disabled"
            msg = f"Static/fixed IP **{state}** for **{resolved}**."
            if ip:
                msg += f" IP: `{ip}`."
            return AdapterResult(
                True,
                "OK",
                msg,
                {"project_id": resolved, "fixed_ip": enabled, "ip": ip},
            )
        except httpx.HTTPError as e:
            return AdapterResult(False, "NETWORK_ERROR", str(e))
