"""Parse Liara PaaS metrics (CPU / memory) and summarize for agents."""

from __future__ import annotations

from typing import Any


def _parse_series(result: list[dict[str, Any]] | None) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not result:
        return points
    for block in result:
        for pair in block.get("values") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                ts = float(pair[0])
                val = float(pair[1])
                points.append((ts, val))
            except (TypeError, ValueError):
                continue
    return points


def summarize_metric_points(points: list[tuple[float, float]]) -> dict[str, float | None]:
    if not points:
        return {"latest": None, "avg": None, "max": None}
    vals = [v for _, v in points]
    return {
        "latest": vals[-1],
        "avg": sum(vals) / len(vals),
        "max": max(vals),
    }


def format_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def format_cpu_value(n: float | None) -> str:
    """Liara returns fractional core usage; show as % of one CPU."""
    if n is None:
        return "—"
    pct = n * 100
    if pct < 0.01:
        return f"{pct:.4f}%"
    if pct < 1:
        return f"{pct:.3f}%"
    return f"{pct:.2f}%"


def build_usage_summary(
    project_id: str,
    *,
    project_meta: dict[str, Any],
    cpu: dict[str, Any],
    memory: dict[str, Any],
    window_hours: float,
) -> dict[str, Any]:
    cpu_pts = _parse_series(cpu.get("result"))
    mem_pts = _parse_series(memory.get("result"))
    cpu_s = summarize_metric_points(cpu_pts)
    mem_s = summarize_metric_points(mem_pts)
    return {
        "project_id": project_id,
        "window_hours": window_hours,
        "plan_id": project_meta.get("planID"),
        "scale": project_meta.get("scale"),
        "status": project_meta.get("status"),
        "type": project_meta.get("type"),
        "hourly_price": project_meta.get("hourlyPrice"),
        "reserved_disk_mb": project_meta.get("reservedDiskSpace"),
        "cpu": {
            "latest": cpu_s["latest"],
            "avg": cpu_s["avg"],
            "max": cpu_s["max"],
            "latest_display": format_cpu_value(cpu_s["latest"]),
            "avg_display": format_cpu_value(cpu_s["avg"]),
            "max_display": format_cpu_value(cpu_s["max"]),
            "sample_count": len(cpu_pts),
        },
        "memory": {
            "latest_bytes": mem_s["latest"],
            "avg_bytes": mem_s["avg"],
            "max_bytes": mem_s["max"],
            "latest_display": format_bytes(mem_s["latest"]),
            "avg_display": format_bytes(mem_s["avg"]),
            "max_display": format_bytes(mem_s["max"]),
            "sample_count": len(mem_pts),
        },
    }


def usage_summary_text(data: dict[str, Any]) -> str:
    pid = data.get("project_id", "app")
    hours = data.get("window_hours", 1)
    cpu = data.get("cpu") or {}
    mem = data.get("memory") or {}
    lines = [
        f"**{pid}** — resource usage (last **{hours:g}h**)",
        f"Plan: **{data.get('plan_id') or '—'}** · scale: **{data.get('scale') or '—'}** · status: **{data.get('status') or '—'}**",
        f"CPU (of 1 core): latest **{cpu.get('latest_display')}**, avg **{cpu.get('avg_display')}**, peak **{cpu.get('max_display')}**",
        f"Memory: latest **{mem.get('latest_display')}**, avg **{mem.get('avg_display')}**, peak **{mem.get('max_display')}**",
    ]
    if data.get("hourly_price") is not None:
        lines.append(f"Hourly price (plan): **{data['hourly_price']}** Toman")
    return "\n".join(lines)
