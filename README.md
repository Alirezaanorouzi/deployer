# Liara DevOps Agent

Agentic FastAPI service: LLM planner, Liara/GitHub tools, deploy pipeline with approval, and streaming chat UI.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Configure `.env` (never commit it). Run:

```bash
.venv\Scripts\uvicorn app.main:app --reload --reload-dir app --port 8000
```

Use `--reload-dir app` only so clones under `data/workspaces` do not restart the server mid-deploy.

Open `http://localhost:8000` for the UI.

## Publish to GitHub

1. Remote: **`https://github.com/Alirezaanorouzi/deployer`**
2. To push updates: `.\scripts\push_to_github.ps1 -RepoName deployer`
3. In the deploy UI, pick **`Alirezaanorouzi/deployer`** for **Deploy to Liara**.

Your `GITHUB_TOKEN` must allow **push** to that repo. If repo creation via API fails, create the repo in the browser and run the script above.
