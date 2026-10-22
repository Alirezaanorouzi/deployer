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
