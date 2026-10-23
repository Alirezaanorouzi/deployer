# Cursor SDK (left panel)

The left column is a **Cursor coding chat** (not the Liara DevOps agent on the right).

## Runtimes

| Mode | Env | What happens |
|------|-----|----------------|
| **Local** (default) | `CURSOR_SDK_RUNTIME=local` | Agent on this machine under `data/workspaces/…/repo` (`GITHUB_TOKEN`). **Streaming** in the Cursor panel via `POST /cursor/chat/stream`. |
| **Cloud** | `CURSOR_SDK_RUNTIME=cloud` | Cursor **Cloud Agent** clones on Cursor’s VM (GitHub App). May hit API quota bugs on Plus until Cursor fixes billing. |

### Cloud prerequisites

1. **API key** — Cursor Dashboard → API Keys (`CURSOR_API_KEY`).
2. **GitHub** — Connect the repo in Cursor ([Cloud Agent docs](https://cursor.com/docs/cloud-agent)) so Cursor can clone it.
3. **Repo dropdown** in the UI — sent as `owner/name` on each `/cursor/chat` call.

Optional `.env`:

```env
CURSOR_CLOUD_BRANCH=main
CURSOR_CLOUD_AUTO_PR=true   # open a PR when the agent finishes
```

## Setup

```bash
.venv\Scripts\pip install -r requirements.txt
```

```env
CURSOR_API_KEY=...
CURSOR_SDK_MODEL=composer-2.5
CURSOR_SDK_RUNTIME=cloud
```

Restart uvicorn after `.env` changes (`--reload-dir app` does not watch `.env`).

Plus / Start / X plans need fixed model params — see `app/cursor_agent/model_selection.py`.

On **Windows**, the deployer shims bridge stderr and sets `trust_env=False` on bridge HTTP to avoid proxy/timeouts.

## UI

- **Cursor** — multi-turn chat (`session_id` in browser).
- **Terminal ↑** — fetches `GET /terminal/log?run_id=…` (full server buffer + step outputs) and sends via `include_terminal_log` on Cursor chat.
- **Local** workspace path or **cloud:**`owner/repo` shown after a run.

## API

- `GET /terminal/log?run_id=` — deploy terminal text for Cursor (`line_count`, `truncated`)
- `GET /cursor/status` — `runtime`, `model`, `cloud_branch`, …
- `POST /cursor/chat/stream` — SSE: `status`, `delta`, `done` (UI uses this)
- `POST /cursor/chat` — non-streaming JSON (same body)
- `DELETE /cursor/session/{session_id}`
