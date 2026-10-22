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
from app.schemas import ChatRequest, PipelineStartRequest, Plan, RunOut, RunStepOut
from app.streaming import sse_format, stream_bus


@asynccontextmanager
async def lifespan(_: FastAPI):
    root = data_root()
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    init_db()
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
        try:
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
