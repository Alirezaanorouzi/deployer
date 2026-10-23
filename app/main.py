import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import get_settings
from app.paths import data_root
from app.llm.client import LlmNotConfiguredError, llm_configured
from app.sandbox.docker_runner import docker_available
from app.database import SessionLocal, get_db, init_db
from app.executor import RunExecutor
from app.github.client import GitHubClient, GitHubError
from app.liara.adapter import LiaraAdapter
from app.models import Run, RunState
from app.pipeline_planner import PIPELINE_SKILL
from app.repo.analyzer import analyze_repository, open_deploy_pr
from app.cursor_agent.client import cursor_sdk_configured
from app.cursor_agent.sessions import chat_turn, clear_session
from app.schemas import (
    ChatRequest,
    CursorChatRequest,
    CursorChatResponse,
    TerminalLogResponse,
    PipelineStartRequest,
    Plan,
    RunOut,
    RunStepOut,
)
from app.streaming import sse_format, stream_bus


def _resume_interrupted_react_runs() -> None:
    """uvicorn --reload kills in-flight deploy jobs; continue them after restart."""
    db = SessionLocal()
    try:
        runs = db.query(Run).filter(Run.state == RunState.executing).all()
        for run in runs:
            try:
                ctx = json.loads(run.context_json or "{}")
            except json.JSONDecodeError:
                continue
            if not ctx.get("react_mode"):
                continue
            asyncio.create_task(_react_agent_run(run.id))
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.llm.client import clear_llm_client_cache

    clear_llm_client_cache()
    root = data_root()
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    init_db()
    _resume_interrupted_react_runs()
    yield


app = FastAPI(title="Liara DevOps Agent", lifespan=lifespan)

static_dir = Path(__file__).parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if x_api_key != settings.workspace_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def verify_api_key_query(api_key: str | None = Query(default=None)) -> None:
    settings = get_settings()
    if api_key != settings.workspace_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def run_to_out(run: Run) -> RunOut:
    plan = None
    if run.plan_json:
        plan = Plan.model_validate_json(run.plan_json)
    repo = None
    liara_app = None
    agent_message = None
    if run.context_json:
        try:
            ctx = json.loads(run.context_json)
            repo = ctx.get("repo")
            liara_app = ctx.get("liara_app_name")
            agent_message = ctx.get("agent_message")
        except json.JSONDecodeError:
            pass
    return RunOut(
        id=run.id,
        state=run.state,
        user_message=run.user_message,
        skill_id=run.skill_id,
        max_risk=run.max_risk,
        plan=plan,
        steps=[
            RunStepOut(
                seq=s.seq,
                name=s.name,
                risk=s.risk,
                status=s.status,
                output=s.output,
            )
            for s in sorted(run.steps, key=lambda x: x.seq)
        ],
        error_code=run.error_code,
        error_message=run.error_message,
        result_summary=run.result_summary,
        created_at=run.created_at,
        approved_at=run.approved_at,
        site_url=run.site_url,
        repo=repo,
        liara_app_name=liara_app,
        agent_message=agent_message,
    )


async def _pipeline_phase1(run_id: str) -> None:
    db = SessionLocal()
    try:
        executor = RunExecutor(db, run_id)
        await executor.execute_until_approval(run_id)
    finally:
        db.close()


async def _pipeline_phase2(run_id: str) -> None:
    db = SessionLocal()
    try:
        executor = RunExecutor(db, run_id)
        await executor.execute_run(run_id)
    finally:
        db.close()


async def _agent_execute_run(run_id: str) -> None:
    db = SessionLocal()
    try:
        executor = RunExecutor(db, run_id)
        await executor.execute_run(run_id)
    finally:
        db.close()


def _run_is_react_mode(run: Run) -> bool:
    if not run.context_json:
        return False
    try:
        return bool(json.loads(run.context_json).get("react_mode"))
    except json.JSONDecodeError:
        return False


async def _react_agent_run(run_id: str) -> None:
    from app.agent.react_agent import run_react_agent

    db = SessionLocal()
    try:
        executor = RunExecutor(db, run_id)
        await run_react_agent(executor, run_id)
    finally:
        db.close()


@app.get("/")
async def index():
    index_path = static_dir / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return {"message": "Liara DevOps Agent API", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/agent/status")
async def agent_status():
    settings = get_settings()
    docker_ok = await docker_available() if settings.docker_sandbox else False
    return {
        "agentic_mode": True,
        "llm_configured": llm_configured(),
        "model": settings.llm_model if llm_configured() else None,
        "docker_sandbox": settings.docker_sandbox,
        "docker_available": docker_ok,
        "docker_image": settings.docker_executor_image,
        "message": "Git/liara run in Docker sandbox when enabled; logs stream to terminal.",
    }


async def _github_repo_names() -> list[str]:
    try:
        gh = GitHubClient()
        repos = await gh.list_repos(30)
        return [r.get("full_name") for r in repos if r.get("full_name")]
    except Exception:
        return []


async def _liara_project_ids() -> list[str]:
    adapter = LiaraAdapter()
    result = await adapter.list_projects()
    if not result.ok or not result.data:
        return []
    projects = result.data.get("projects", [])
    return [p.get("project_id") for p in projects if p.get("project_id")]


@app.post("/runs/pipeline", response_model=RunOut, dependencies=[Depends(verify_api_key)])
async def start_pipeline(
    body: PipelineStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    executor = RunExecutor(db)
    run = await executor.create_pipeline_run(body.repo, body.subpath, body.liara_app_name)
    background_tasks.add_task(_pipeline_phase1, run.id)
    return run_to_out(run)


@app.get("/runs/{run_id}/stream", dependencies=[Depends(verify_api_key_query)])
async def run_stream(run_id: str):
    q = stream_bus.subscribe(run_id)

    async def event_generator():
        from app.terminal_log import append

        try:
            append(run_id, "— stream connected —")
            yield sse_format("connected", {"run_id": run_id})
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=120.0)
                    yield sse_format(msg["event"], msg["data"])
                    if msg["event"] in ("succeeded", "run_error"):
                        break
                except asyncio.TimeoutError:
                    yield sse_format("ping", {})
        finally:
            stream_bus.unsubscribe(run_id, q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/chat", response_model=RunOut, dependencies=[Depends(verify_api_key)])
async def chat(body: ChatRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not llm_configured():
        raise HTTPException(
            503,
            detail="Agent requires LLM_API_KEY and LLM_BASE_URL in .env (agentic planner).",
        )
    executor = RunExecutor(db)
    repos = await _github_repo_names()
    projects = await _liara_project_ids()
    try:
        history = [{"role": h.role, "content": h.content} for h in body.history]
        run = await executor.create_with_agent(
            body.message,
            app=body.app,
            target_env=body.target_env,
            health_url=body.health_url,
            github_repos=repos,
            liara_projects=projects,
            conversation_history=history,
        )
    except LlmNotConfiguredError as e:
        raise HTTPException(503, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except json.JSONDecodeError as e:
        raise HTTPException(
            502,
            detail="LLM returned invalid JSON. Check LLM_API_KEY, model name, and Box API quota; then restart the server.",
        ) from e
    except Exception as e:
        msg = str(e).strip() or type(e).__name__
        if "401" in msg or "unauthorized" in msg.lower():
            raise HTTPException(502, detail="LLM API key rejected (401). Update LLM_API_KEY and restart uvicorn.") from e
        if "429" in msg or "rate" in msg.lower() or "quota" in msg.lower():
            raise HTTPException(502, detail=f"LLM rate limit or quota: {msg}") from e
        if "timeout" in msg.lower() or type(e).__name__ in ("APITimeoutError", "ReadTimeout", "ConnectTimeout"):
            raise HTTPException(
                502,
                detail="LLM request timed out. Check LLM_BASE_URL, model name, and network access to ai.liara.ir.",
            ) from e
        if "connection" in msg.lower() or type(e).__name__ == "APIConnectionError":
            raise HTTPException(
                502,
                detail="Cannot reach LLM API (connection error). Verify LLM_BASE_URL and internet/VPN/firewall.",
            ) from e
        raise HTTPException(502, detail=f"Agent planner failed: {msg}") from e

    if run.skill_id == PIPELINE_SKILL:
        background_tasks.add_task(_pipeline_phase1, run.id)
    elif _run_is_react_mode(run):
        background_tasks.add_task(_react_agent_run, run.id)
    elif run.state == RunState.executing:
        background_tasks.add_task(_agent_execute_run, run.id)
    return run_to_out(run)


@app.get("/runs/{run_id}", response_model=RunOut, dependencies=[Depends(verify_api_key)])
async def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run_to_out(run)


@app.post("/runs/{run_id}/cancel", response_model=RunOut, dependencies=[Depends(verify_api_key)])
async def cancel_run(run_id: str, db: Session = Depends(get_db)):
    """Mark a stuck run cancelled (in-flight CLI may still finish until timeout)."""
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.state in (RunState.succeeded, RunState.failed, RunState.cancelled):
        return run_to_out(run)
    try:
        ctx = json.loads(run.context_json or "{}")
    except json.JSONDecodeError:
        ctx = {}
    ctx["cancel_requested"] = True
    run.context_json = json.dumps(ctx)
    run.state = RunState.cancelled
    run.error_message = "Cancelled by user"
    run.result_summary = (
        "Run cancelled. If Liara deploy was in progress, check the Liara console or wait for the CLI to time out, "
        "then start a new chat deploy."
    )
    db.commit()
    db.refresh(run)
    await stream_bus.publish(run_id, "run_error", {"message": run.result_summary})
    return run_to_out(run)


@app.get("/integrations/liara/status", dependencies=[Depends(verify_api_key)])
async def liara_status():
    adapter = LiaraAdapter()
    return (await adapter.auth_check()).__dict__


@app.get("/integrations/liara/projects", dependencies=[Depends(verify_api_key)])
async def liara_projects():
    adapter = LiaraAdapter()
    result = await adapter.list_projects()
    if not result.ok:
        raise HTTPException(400, detail=result.message)
    return result.data


@app.get("/integrations/liara/projects/{project_id}", dependencies=[Depends(verify_api_key)])
async def liara_project(project_id: str):
    adapter = LiaraAdapter()
    result = await adapter.get_project(project_id)
    if not result.ok:
        raise HTTPException(result.code == "NOT_FOUND" and 404 or 400, detail=result.message)
    return result.data


@app.get("/integrations/github/repos", dependencies=[Depends(verify_api_key)])
async def github_repos():
    import httpx

    try:
        gh = GitHubClient()
        repos = await gh.list_repos()
        slim = [
            {"full_name": r.get("full_name"), "private": r.get("private"), "default_branch": r.get("default_branch")}
            for r in repos
        ]
        return {"repos": slim}
    except GitHubError as e:
        raise HTTPException(400, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(
            502,
            detail=f"Could not reach GitHub API ({e}). Check network; disable bad HTTP_PROXY or set NO_PROXY for api.github.com.",
        )


@app.get("/terminal/log", response_model=TerminalLogResponse, dependencies=[Depends(verify_api_key)])
async def terminal_log_api(
    run_id: str | None = None,
    db: Session = Depends(get_db),
):
    from app.terminal_log import get_terminal_log_text

    text, line_count, truncated = get_terminal_log_text(run_id, db)
    return TerminalLogResponse(
        run_id=run_id,
        text=text,
        line_count=line_count,
        truncated=truncated,
    )


@app.get("/cursor/status", dependencies=[Depends(verify_api_key)])
async def cursor_status():
    s = get_settings()
    return {
        "configured": cursor_sdk_configured(),
        "model": s.cursor_sdk_model,
        "runtime": (s.cursor_sdk_runtime or "cloud").strip().lower(),
        "cloud_branch": s.cursor_cloud_branch,
        "cloud_auto_pr": bool(s.cursor_cloud_auto_pr),
    }


@app.post("/cursor/chat/stream", dependencies=[Depends(verify_api_key)])
async def cursor_chat_stream(body: CursorChatRequest, db: Session = Depends(get_db)):
    if not cursor_sdk_configured():
        raise HTTPException(503, detail="CURSOR_API_KEY is not set in .env")

    from app.cursor_agent.stream_chat import stream_chat_events

    async def event_generator():
        yield sse_format("connected", {})
        async for event, data in stream_chat_events(
            body.message,
            session_id=body.session_id,
            run_id=body.run_id,
            repo=body.repo,
            deploy_log=body.deploy_log,
            include_terminal_log=body.include_terminal_log,
            db=db,
        ):
            yield sse_format(event, data)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/cursor/chat", response_model=CursorChatResponse, dependencies=[Depends(verify_api_key)])
async def cursor_chat(body: CursorChatRequest, db: Session = Depends(get_db)):
    if not cursor_sdk_configured():
        raise HTTPException(503, detail="CURSOR_API_KEY is not set in .env")
    data = await chat_turn(
        body.message,
        session_id=body.session_id,
        run_id=body.run_id,
        repo=body.repo,
        deploy_log=body.deploy_log,
        include_terminal_log=body.include_terminal_log,
        db=db,
    )
    return CursorChatResponse(
        ok=bool(data.get("ok")),
        session_id=data.get("session_id") or "",
        status=data.get("status") or "",
        reply=data.get("reply") or "",
        workspace=data.get("workspace") or "",
        error=data.get("error"),
    )


@app.delete("/cursor/session/{session_id}", dependencies=[Depends(verify_api_key)])
async def cursor_session_clear(session_id: str):
    clear_session(session_id)
    return {"ok": True}


@app.get("/integrations/github/sample-analysis", dependencies=[Depends(verify_api_key)])
async def github_sample_analysis():
    settings = get_settings()
    try:
        analysis = await analyze_repository(
            settings.github_sample_repo,
            subpath=settings.github_sample_path,
        )
        return analysis.to_dict()
    except GitHubError as e:
        raise HTTPException(400, detail=str(e))


@app.post("/integrations/github/analyze", dependencies=[Depends(verify_api_key)])
async def github_analyze(repo: str, subpath: str = ""):
    try:
        return (await analyze_repository(repo, subpath=subpath)).to_dict()
    except GitHubError as e:
        raise HTTPException(400, detail=str(e))


@app.post("/integrations/github/deploy-pr", dependencies=[Depends(verify_api_key)])
async def github_deploy_pr(repo: str, project_id: str, subpath: str = ""):
    try:
        return await open_deploy_pr(repo, project_id, subpath=subpath)
    except GitHubError as e:
        raise HTTPException(400, detail=str(e))


@app.post("/runs/{run_id}/approve", response_model=RunOut, dependencies=[Depends(verify_api_key)])
async def approve_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    executor = RunExecutor(db)
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    try:
        run = executor.approve(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if run.skill_id == PIPELINE_SKILL:
        background_tasks.add_task(_pipeline_phase2, run.id)
        return run_to_out(run)

    run = await executor.execute_run(run.id)
    return run_to_out(run)
