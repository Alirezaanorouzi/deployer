"""Parse Liara deploy/runtime logs — same signals we used for manual 502/503 fixes."""

from __future__ import annotations

import re

_LOG_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Read-only file system|OSError: \[Errno 30\]", re.I),
        "App root on Liara is read-only. Use `app.paths.data_root()` / **DATA_DIR** "
        "(e.g. `/tmp/deployer-data`); do not `mkdir data` in the project root. "
        "Avoid `DATABASE_URL=sqlite:///./data/...` on the platform.",
    ),
    (
        re.compile(r"Application startup failed", re.I),
        "Process exits during FastAPI lifespan. Read the traceback above in logs, fix code, "
        "then call **liara.deploy_service** again.",
    ),
    (
        re.compile(r"Path\(\"data\"\)\.mkdir", re.I),
        "Old code tried to create `./data` at startup. Redeploy with current `main.py` "
        "that uses `data_root()`.",
    ),
    (
        re.compile(r"command --team-id not found", re.I),
        "CLI flag order: `liara deploy --app X --team-id Y` (team id after subcommand).",
    ),
    (
        re.compile(r"Deployment failed|BUILD_FAILED", re.I),
        "Liara build/deploy failed. Check Dockerfile/liara.json, `requirements.txt`, and platform.",
    ),
    (
        re.compile(r"Error: listen EADDRINUSE|address already in use", re.I),
        "Port conflict. Use `python main.py` so **PORT** from Liara matches `liara.json` `port` (e.g. 8000).",
    ),
    (
        re.compile(r"Uvicorn running on http://0\.0\.0\.0", re.I),
        "App is listening on 0.0.0.0 — good. If URL still 502/503, wait for health check or check router.",
    ),
    (
        re.compile(r"multirun: one or more of the provided commands ended abnormally", re.I),
        "Start command crashed. Ensure a single start in liara.json `args` (e.g. `python main.py`).",
    ),
]

_LIARA_PYTHON_RUNTIME_CHECKLIST = (
    "**Liara Python runtime (after prepare):** "
    "`liara.json` → `port` 8000, `args`: `[\"python main.py\"]`; "
    "listen on **0.0.0.0** + **PORT**; writable DB via `DATA_DIR` / `paths.data_root()`."
)


def hints_from_logs(log_text: str) -> list[str]:
    if not log_text:
        return ["No logs returned — try `liara.diagnose_deploy_health` or `liara.logs` with a larger window."]
    found: list[str] = []
    for pattern, hint in _LOG_RULES:
        if pattern.search(log_text) and hint not in found:
            found.append(hint)
    if not found and ("502" in log_text or "503" in log_text):
        found.append(
            "Gateway 502/503: confirm app listens on 0.0.0.0 and the port in liara.json; "
            "check Events tab for last deploy status."
        )
    return found


def format_diagnosis(
    *,
    url: str,
    status_code: int | None,
    log_tail: str,
    extra_hints: list[str] | None = None,
) -> str:
    hints = hints_from_logs(log_tail)
    if extra_hints:
        for h in extra_hints:
            if h not in hints:
                hints.append(h)
    status = status_code if status_code else "error"
    body = f"**{url}** → HTTP {status}\n\n"
    if log_tail.strip():
        body += f"**Recent logs:**\n```\n{log_tail[-2800:]}\n```\n\n"
    if hints:
        body += "**Likely fixes:**\n" + "\n".join(f"- {h}" for h in hints)
    return body
