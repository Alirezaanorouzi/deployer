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
.venv\Scripts\uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` for the UI.

## Publish to GitHub

1. Create an **empty** private repo on GitHub (e.g. `Alirezaanorouzi/liara-deployer`) — no README/license.
2. From the project root:

```powershell
.\scripts\push_to_github.ps1
```

3. In the deploy UI, pick **`Alirezaanorouzi/liara-deployer`** (or your repo name) for **Deploy to Liara**.

Your `GITHUB_TOKEN` must allow **push** to that repo. If repo creation via API fails, create the repo in the browser and run the script above.
