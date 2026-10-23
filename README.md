# Liara DevOps Agent

Agentic FastAPI service: **LLM-first** ReAct loop (`plan_with_agent` once, then `plan_next_step` → tools), Liara/GitHub tools, deploy pipeline with approval, and streaming chat UI. See [docs/agent-architecture.md](docs/agent-architecture.md).

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Configure `.env` (never commit it). For **Liara AI** (`LLM_BASE_URL` like `https://ai.liara.ir/api/.../v1`), set `LLM_MODEL` to an id from your Liara AI dashboard (e.g. `openai/gpt-5.6-luna`). Restart uvicorn after changing `LLM_API_KEY` or model.

Run:

```bash
.venv\Scripts\uvicorn app.main:app --reload --reload-dir app --port 8000
```

Use `--reload-dir app` only so clones under `data/workspaces` do not restart the server mid-deploy.

Open `http://localhost:8000` for the UI.

### Cursor SDK (left panel)

Optional: [docs/cursor-sdk.md](docs/cursor-sdk.md) — `CURSOR_API_KEY`, default **`CURSOR_SDK_RUNTIME=local`** (agent runs in `data/workspaces/…`). Set `cloud` to use Cursor’s cloud VM (GitHub connected in Cursor).

## Publish to GitHub

1. Remote: **`https://github.com/Alirezaanorouzi/deployer`**
2. Push updates with git (`git push origin main` or your branch).
3. In the deploy UI, pick **`Alirezaanorouzi/deployer`** for **Deploy to Liara**.

Your `GITHUB_TOKEN` must allow **push** to that repo. If repo creation via API fails, create the repo in the browser and run the script above.
