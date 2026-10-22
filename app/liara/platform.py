"""Liara PaaS platform console APIs (events, releases, domains, disks, settings)."""

from __future__ import annotations

import json
import re
import time
from typing import Any

_DISK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]+[a-z0-9]$")

from app.liara.redact import sanitize_liara_project


def _first_line(text: str | None, max_len: int = 100) -> str:
    if not text:
        return ""
    line = str(text).split("\n", 1)[0].strip()
    return line[:max_len] + ("…" if len(line) > max_len else "")


def sanitize_release(release: dict[str, Any]) -> dict[str, Any]:
    """Drop build args / secrets from a release record."""
    git = release.get("gitInfo") if isinstance(release.get("gitInfo"), dict) else {}
    commit = git.get("commit") or ""
    return {
        "id": release.get("_id"),
        "type": release.get("type"),
        "state": release.get("state"),
        "port": release.get("port"),
        "platform": release.get("projectType"),
        "build_location": release.get("buildLocation"),
        "created_at": release.get("createdAt") or release.get("created_at"),
        "finished_at": release.get("finishedAt") or release.get("finished_at"),
        "git": {
            "branch": git.get("branch"),
            "commit": commit[:12] if commit else None,
            "message": _first_line(git.get("message"), 140),
        },
    }


def sanitize_domain(domain: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": domain.get("_id"),
        "name": domain.get("name"),
        "status": domain.get("status"),
        "certificates_status": domain.get("certificatesStatus"),
        "redirect_to": domain.get("redirectTo") or None,
        "redirect_status": domain.get("redirectStatus"),
        "cname_record": domain.get("CNameRecord"),
        "acme_record": domain.get("acmeRecord"),
        "behind_cdn": domain.get("behindCdn"),
        "is_wildcard": domain.get("isWildcard"),
        "created_at": domain.get("created_at"),
    }


def normalize_disk_name(raw: str) -> tuple[str | None, str | None]:
    """Liara disk names: lowercase letters, digits, hyphens (no underscores)."""
    name = (raw or "").strip().lower().replace("_", "-")
    if not name:
        return None, "disk name is required"
    if len(name) < 3:
        return None, "disk name must be at least 3 characters"
    if not _DISK_NAME_RE.match(name):
        return (
            None,
            "disk name must match `[a-z0-9-]+` (hyphens only, e.g. shop-cache not shop_cache)",
        )
    return name, None


def parse_disk_size_gb(raw: str | int | float | None) -> tuple[int | None, str | None]:
    if raw is None:
        return None, "size_gb is required"
    if isinstance(raw, (int, float)):
        size = int(raw)
    else:
        text = str(raw).strip().lower().replace("گیگ", "").replace("gb", "").strip()
        try:
            size = int(float(text))
        except (TypeError, ValueError):
            return None, f"invalid disk size: {raw}"
    if size < 1 or size > 512:
        return None, "disk size must be between 1 and 512 GB"
    return size, None


def disks_from_api_payload(body: dict[str, Any] | list | None) -> list[dict[str, Any]]:
    if isinstance(body, list):
        raw_disks = body
        mounts: list[dict[str, Any]] = []
    elif isinstance(body, dict):
        raw_disks = body.get("disks") or []
        mounts = body.get("mounts") or []
    else:
        return []
    mount_map: dict[str, str] = {}
    for m in mounts:
        if isinstance(m, dict) and m.get("name"):
            mount_map[str(m["name"])] = str(m.get("mountedTo") or m.get("mount_to") or "")
    out: list[dict[str, Any]] = []
    for d in raw_disks:
        if not isinstance(d, dict):
            continue
        item = sanitize_disk(d)
        if not item.get("mount_to") and item.get("name") in mount_map:
            item["mount_to"] = mount_map[item["name"]]
        out.append(item)
    return out


def sanitize_disk(disk: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": disk.get("_id"),
        "name": disk.get("name"),
        "mount_to": disk.get("mountTo") or disk.get("mount_to"),
        "size_gb": disk.get("size") or disk.get("sizeGB"),
        "status": disk.get("status"),
        "filebrowser_url": disk.get("filebrowserUrl"),
    }


def liara_error_message(response_text: str, status_code: int) -> str:
    try:
        body = json.loads(response_text) if response_text else {}
    except json.JSONDecodeError:
        body = {}
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error")
        if msg == "max_disks_reached":
            return (
                f"Liara rejected disk create ({status_code}): this app already has the maximum "
                "number of disks allowed on your plan. Remove a disk in the console or upgrade."
            )
        if msg:
            return f"Liara API error ({status_code}): {msg}"
    return f"Liara API error ({status_code})"


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    ts = event.get("time") or event.get("createdAt") or event.get("created_at")
    action = event.get("action") or event.get("type")
    status = event.get("status")
    parts = [p for p in (action, status) if p]
    message = event.get("message") or event.get("title") or " — ".join(parts)
    if event.get("tag"):
        message = f"{message} ({event.get('tag')})"
    if event.get("release"):
        rel = str(event.get("release"))[:12]
        message = f"{message} [release {rel}]"
    return {
        "id": event.get("id") or event.get("_id"),
        "action": action,
        "status": status,
        "message": message,
        "time": ts,
        "tag": event.get("tag"),
        "release": event.get("release"),
    }


def settings_snapshot(project: dict[str, Any]) -> dict[str, Any]:
    safe = sanitize_liara_project(project)
    env_keys = [e.get("key") for e in (safe.get("envs") or []) if isinstance(e, dict) and e.get("key")]
    return {
        "project_id": safe.get("project_id"),
        "type": safe.get("type"),
        "status": safe.get("status"),
        "scale": safe.get("scale"),
        "is_deployed": safe.get("isDeployed"),
        "zero_downtime": safe.get("zeroDowntime"),
        "read_only_root_fs": safe.get("readOnlyRootFilesystem"),
        "default_subdomain": safe.get("defaultSubdomain"),
        "keep_default_subdomain": safe.get("keepDefaultSubdomain"),
        "network": safe.get("network"),
        "dns": safe.get("dns"),
        "outbound_ip": safe.get("outboundIp"),
        "fixed_ip_status": safe.get("fixedIPStatus"),
        "reserved_disk_space": safe.get("reservedDiskSpace"),
        "env_var_count": len(env_keys),
        "env_var_keys": env_keys[:40],
    }


def plan_snapshot(project: dict[str, Any]) -> dict[str, Any]:
    safe = sanitize_liara_project(project)
    return {
        "project_id": safe.get("project_id"),
        "plan_id": safe.get("planID"),
        "bundle_plan_id": safe.get("bundlePlanID"),
        "hourly_price_toman": safe.get("hourlyPrice"),
        "bundle_hourly_price_toman": safe.get("bundleHourlyPrice"),
        "scale": safe.get("scale"),
        "type": safe.get("type"),
        "status": safe.get("status"),
        "resize_note": "Use change_service_plan with a planID from list_available_plans after user confirmation.",
    }


def events_window_from_params(params: dict[str, Any]) -> tuple[int | None, int | None]:
    now = int(time.time())
    since_hours = params.get("since_hours")
    if since_hours is not None:
        try:
            hours = float(since_hours)
            hours = max(0.25, min(hours, 168.0))
            return int(now - hours * 3600), now
        except (TypeError, ValueError):
            pass
    start = params.get("start_ts") or params.get("since_ts")
    end = params.get("end_ts") or params.get("until_ts")
    try:
        s = int(start) if start is not None else None
    except (TypeError, ValueError):
        s = None
    try:
        e = int(end) if end is not None else now
    except (TypeError, ValueError):
        e = now
    return s, e if start is not None else (None, None)


def format_events_text(project_id: str, events: list[dict[str, Any]], window: str) -> str:
    lines = [f"**{project_id}** — platform events ({window})"]
    if not events:
        lines.append("_No events in this window._")
        return "\n".join(lines)
    for ev in events[:25]:
        ts = ev.get("time") or ev.get("created_at") or "?"
        typ = ev.get("action") or ev.get("type") or "event"
        msg = ev.get("message") or ""
        lines.append(f"- `{ts}` **{typ}** — {msg}")
    if len(events) > 25:
        lines.append(f"_… and {len(events) - 25} more_")
    return "\n".join(lines)


def format_releases_text(
    project_id: str,
    *,
    total: int,
    current_release: str | None,
    ready_count: int | None,
    releases: list[dict[str, Any]],
    page: int,
) -> str:
    lines = [
        f"**{project_id}** — deploy / release history (page {page}, showing {len(releases)} of {total})",
    ]
    if current_release:
        lines.append(f"Current release: `{current_release[:12]}…`")
    if ready_count is not None:
        lines.append(f"Ready releases: {ready_count}")
    if not releases:
        lines.append("_No releases on this page._")
        return "\n".join(lines)
    for rel in releases:
        git = rel.get("git") or {}
        commit = git.get("commit") or "—"
        branch = git.get("branch") or "—"
        state = rel.get("state") or "?"
        typ = rel.get("type") or "RELEASE"
        ts = rel.get("created_at") or "?"
        lines.append(
            f"- `{ts}` **{typ}** [{state}] `{commit}` on **{branch}** — {git.get('message') or ''}"
        )
    lines.append(
        "\n_To deploy new code use deploy_service or the GitHub pipeline; use history to pick release IDs._"
    )
    return "\n".join(lines)


def format_domains_text(project_id: str, domains: list[dict[str, Any]]) -> str:
    lines = [f"**{project_id}** — custom domains ({len(domains)})"]
    if not domains:
        lines.append("_No custom domains linked to this app._")
        lines.append(f"Default URL: https://{project_id}.liara.run")
        return "\n".join(lines)
    for d in domains:
        name = d.get("name")
        st = d.get("status")
        cert = d.get("certificates_status")
        redir = d.get("redirect_to")
        extra = f" -> {redir}" if redir else ""
        lines.append(f"- **{name}** — {st}, TLS {cert}{extra}")
    return "\n".join(lines)


def format_disks_text(project_id: str, disks: list[dict[str, Any]]) -> str:
    lines = [f"**{project_id}** — attached disks (**{len(disks)}** total)"]
    if not disks:
        lines.append("_No disks on this app._")
        return "\n".join(lines)
    for disk in disks:
        mount = disk.get("mount_to") or "—"
        lines.append(f"- **{disk.get('name')}** — {disk.get('size_gb')} GB, mount `{mount}`")
    return "\n".join(lines)


def format_settings_text(snapshot: dict[str, Any]) -> str:
    pid = snapshot.get("project_id", "app")
    lines = [
        f"**{pid}** — app settings (secrets redacted)",
        f"- Platform: **{snapshot.get('type')}**, status **{snapshot.get('status')}**, scale **{snapshot.get('scale')}**",
        f"- Deployed: {snapshot.get('is_deployed')}, zero-downtime: {snapshot.get('zero_downtime')}",
        f"- Read-only root FS: {snapshot.get('read_only_root_fs')}",
        f"- Network: {snapshot.get('network')}, DNS: {snapshot.get('dns') or 'default'}",
    ]
    if snapshot.get("outbound_ip"):
        lines.append(f"- Outbound IP: {snapshot.get('outbound_ip')} ({snapshot.get('fixed_ip_status')})")
    keys = snapshot.get("env_var_keys") or []
    if keys:
        preview = ", ".join(keys[:12])
        more = f" (+{len(keys) - 12} more)" if len(keys) > 12 else ""
        lines.append(f"- Env vars ({snapshot.get('env_var_count')}): {preview}{more}")
    return "\n".join(lines)


def format_plan_text(snapshot: dict[str, Any]) -> str:
    pid = snapshot.get("project_id", "app")
    lines = [
        f"**{pid}** — plan & scale (read-only)",
        f"- Plan ID: **{snapshot.get('plan_id')}**",
        f"- Bundle plan: {snapshot.get('bundle_plan_id') or '—'}",
        f"- Hourly price: {snapshot.get('hourly_price_toman')} Toman"
        + (
            f" (bundle {snapshot.get('bundle_hourly_price_toman')})"
            if snapshot.get("bundle_hourly_price_toman")
            else ""
        ),
        f"- Replicas / scale: **{snapshot.get('scale')}**",
        f"- {snapshot.get('resize_note')}",
    ]
    return "\n".join(lines)
