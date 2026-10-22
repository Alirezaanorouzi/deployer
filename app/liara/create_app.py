"""Liara PaaS app creation (API) — validation and platform mapping."""

import re

# Platforms accepted by POST /v1/projects (see Liara docs / OpenAPI).
LIARA_CREATE_PLATFORMS = frozenset(
    {
        "node",
        "next",
        "react",
        "angular",
        "vue",
        "python",
        "django",
        "flask",
        "laravel",
        "php",
        "dotnet",
        "static",
        "docker",
    }
)

_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$|^[a-z0-9]$")


def normalize_liara_app_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", (raw or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug or "liara-app")[:40]


def validate_app_id(app_id: str) -> str | None:
    """Return error message or None if valid."""
    if not app_id:
        return "app id is required"
    if len(app_id) < 1 or len(app_id) > 40:
        return "app id must be 1–40 characters"
    if not _APP_ID_RE.match(app_id):
        return "app id: lowercase letters, digits, hyphens only"
    return None


def validate_platform(platform: str) -> str | None:
    p = (platform or "").strip().lower()
    if p not in LIARA_CREATE_PLATFORMS:
        return (
            f"unsupported platform '{platform}'. "
            f"Use one of: {', '.join(sorted(LIARA_CREATE_PLATFORMS))}"
        )
    return None
