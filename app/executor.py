import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.diagnosis import diagnose_with_llm
from app.liara.adapter import LiaraAdapter
from app.models import Run, RunState, RunStep
from app.pipeline_planner import PIPELINE_SKILL, approval_step_index, plan_pipeline_github_to_liara
from app.agent.loop import observe_after_step, observe_awaiting_approval, step_label
from app.agent.plan_builder import plan_from_decision
from app.agent.planner_agent import REACT_SKILLS, AgentDecision, plan_with_agent
from app.agent.tools.registry import tools_for_skill
from app.planner import plan_max_risk
from app.repo.paths import deploy_root, normalize_subpath
from app.repo.workspace import (
    analyze_local_directory,
    clone_repository,
    write_liara_config,
)
from app.risk import RiskTier, requires_approval
from app.schemas import Plan
from app.streaming import stream_bus


class RunExecutor:
    def __init__(self, db: Session, run_id: str | None = None) -> None:
        self.db = db
        self._run_id = run_id
        self.liara = LiaraAdapter(run_id=run_id)

    def _ctx(self, run: Run) -> dict[str, Any]:
        if not run.context_json:
            return {}
        return json.loads(run.context_json)

    def _save_ctx(self, run: Run, ctx: dict[str, Any]) -> None:
        run.context_json = json.dumps(ctx)
        self.db.commit()

    def _bind_run(self, run_id: str) -> None:
        self._run_id = run_id
        self.liara = LiaraAdapter(run_id=run_id)

    async def _log(self, line: str) -> None:
        if self._run_id:
            await stream_bus.log(self._run_id, line)

    async def _agent_chat(
        self,
        run: Run,
        ctx: dict[str, Any],
        text: str,
        *,
        append: bool = True,
    ) -> None:
        if not text:
            return
        if append:
            prev = (ctx.get("agent_message") or "").strip()
            ctx["agent_message"] = f"{prev}\n\n{text}".strip() if prev else text
        else:
            ctx["agent_message"] = text
        self._save_ctx(run, ctx)
        await stream_bus.publish(run.id, "agent_message", {"text": text, "append": append})

    async def _agent_chat_stream(
        self,
        run: Run,
        ctx: dict[str, Any],
        *,
        system: str,
        user: str,
        temperature: float | None = 0.25,
        new_block: bool = True,
    ) -> str:
        from app.llm.streaming import stream_chat_to_run

        prev = (ctx.get("agent_message") or "").strip()
        text = await stream_chat_to_run(
            run.id,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
            new_block=new_block and bool(prev),
        )
        if not text:
            return ""
        if prev and new_block:
            ctx["agent_message"] = f"{prev}\n\n{text}".strip()
        elif prev:
            ctx["agent_message"] = f"{prev}{text}"
        else:
            ctx["agent_message"] = text
        self._save_ctx(run, ctx)
        return text

    async def create_with_agent(
        self,
        message: str,
        *,
        app: str | None,
        target_env: str,
        health_url: str | None,
        github_repos: list[str] | None = None,
        liara_projects: list[str] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> Run:
        decision = await plan_with_agent(
            message,
            app=app,
            target_env=target_env,
            health_url=health_url,
            github_repos=github_repos,
            liara_projects=liara_projects,
            conversation_history=conversation_history,
        )

        if decision.is_pipeline:
            repo = decision.params.get("repo") or decision.params.get("github_repo")
            if not repo:
                raise ValueError("Agent could not determine GitHub repo — please specify owner/name.")
            run = await self.create_pipeline_run(
                repo,
                normalize_subpath(decision.params.get("subpath")),
                decision.params.get("liara_app_name") or decision.params.get("app_name"),
            )
            ctx = self._ctx(run)
            ctx["agent_message"] = decision.assistant_message
            ctx["agent_loop"] = True
            ctx["user_goal"] = message
            ctx["agent_transcript"] = []
            self._save_ctx(run, ctx)
            await stream_bus.publish(
                run.id,
                "agent_message",
                {"text": decision.assistant_message, "append": False},
            )
            return run

        if decision.skill_id in REACT_SKILLS:
            run = await self.create_react_run(
                message,
                decision,
                conversation_history=conversation_history,
            )
            if decision.skill_id == "agent.summarize":
                ctx = self._ctx(run)
                ctx["summarize_only"] = True
                self._save_ctx(run, ctx)
            return run

        run = Run(user_message=message, state=RunState.planning)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        self._run_id = run.id

        plan = plan_from_decision(decision)
        app_slug = decision.params.get("app_name") or decision.params.get("liara_app_name")
        run.context_json = json.dumps(
            {
                "agent_message": decision.assistant_message,
                "agent_loop": True,
                "user_goal": message,
                "agent_transcript": [],
                "defer_execution": True,
                "liara_app_name": app_slug,
                "log_since_hours": decision.params.get("since_hours") or 24,
            }
        )
        run = await self._finalize_plan(run, plan)
        await stream_bus.publish(
            run.id,
            "agent_message",
            {"text": decision.assistant_message, "append": False},
        )
        return run

    async def create_react_run(
        self,
        message: str,
        decision: AgentDecision,
        *,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> Run:
        run = Run(
            user_message=message,
            state=RunState.executing,
            skill_id=decision.skill_id,
            max_risk=RiskTier.low,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        self._run_id = run.id

        ctx = {
            "react_mode": True,
            "agent_loop": True,
            "user_goal": message,
            "agent_message": "",
            "planner_intro": decision.assistant_message,
            "agent_transcript": [],
            "observations": [],
            "suggested_tools": tools_for_skill(decision.skill_id),
            "planner_params": decision.params or {},
            "conversation_history": conversation_history or [],
        }
        run.context_json = json.dumps(ctx)
        self.db.commit()
        return run

    async def create_and_plan(
        self,
        message: str,
        *,
        app: str | None,
        target_env: str,
        health_url: str | None,
        github_repos: list[str] | None = None,
        liara_projects: list[str] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> Run:
        return await self.create_with_agent(
            message,
            app=app,
            target_env=target_env,
            health_url=health_url,
            github_repos=github_repos,
            liara_projects=liara_projects,
            conversation_history=conversation_history,
        )

    async def create_pipeline_run(
        self,
        repo: str,
        subpath: str | None = "",
        liara_app_name: str | None = None,
    ) -> Run:
        subpath = normalize_subpath(subpath)
        run = Run(
            user_message=f"Pipeline deploy {repo} to Liara",
            state=RunState.planning,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        self._run_id = run.id

        plan = plan_pipeline_github_to_liara(repo, subpath, liara_app_name)
        ctx = {
            "repo": repo,
            "subpath": subpath,
            "liara_app_name": plan.steps[4].params.get("app_name"),
            "analysis": {},
        }
        run.context_json = json.dumps(ctx)
        run = await self._finalize_plan(run, plan)
        await stream_bus.publish(run.id, "plan_created", {"skill_id": plan.skill_id, "summary": plan.summary_en})
        return run

    async def _finalize_plan(self, run: Run, plan: Plan) -> Run:
        run.skill_id = plan.skill_id
        run.plan_json = plan.model_dump_json()
        run.max_risk = plan_max_risk(plan)

        for i, step in enumerate(plan.steps):
            self.db.add(
                RunStep(
                    run_id=run.id,
                    seq=i,
                    name=step.name,
                    risk=step.risk,
                    status="pending",
                )
            )

        if run.skill_id != PIPELINE_SKILL and requires_approval(run.max_risk):
            run.state = RunState.awaiting_approval
        elif run.skill_id != PIPELINE_SKILL:
            run.state = RunState.executing
        else:
            run.state = RunState.executing
        self.db.commit()
        self.db.refresh(run)

        if run.skill_id != PIPELINE_SKILL and run.state == RunState.executing:
            if not self._ctx(run).get("defer_execution"):
                await self.execute_run(run.id)
                run = self.db.get(Run, run.id) or run
        return run

    async def execute_until_approval(self, run_id: str) -> Run:
        run = self.db.get(Run, run_id)
        if not run or not run.plan_json:
            raise ValueError("Run not found")
        self._bind_run(run_id)
        plan = Plan.model_validate_json(run.plan_json)
        stop = approval_step_index(plan)
        await self._execute_steps(run, plan, start=0, stop=stop)
        run = self.db.get(Run, run_id) or run
        if run.state != RunState.failed:
            run.state = RunState.awaiting_approval
            self.db.commit()
            ctx = self._ctx(run)
            await stream_bus.publish(
                run_id,
                "approval_required",
                {
                    "message": "Approve Liara app creation and deploy?",
                    "app_name": ctx.get("liara_app_name"),
                    "repo": ctx.get("repo"),
                },
            )
            if ctx.get("agent_loop"):
                approval_msg = await observe_awaiting_approval(
                    user_goal=ctx.get("user_goal", run.user_message),
                    repo=ctx.get("repo"),
                    app_name=ctx.get("liara_app_name"),
                )
                await self._agent_chat(run, ctx, approval_msg, append=True)
        return run

    async def execute_run(self, run_id: str) -> Run:
        run = self.db.get(Run, run_id)
        if not run or not run.plan_json:
            raise ValueError("Run not found")
        self._bind_run(run_id)
        plan = Plan.model_validate_json(run.plan_json)

        if plan.skill_id == PIPELINE_SKILL:
            start = approval_step_index(plan)
            return await self._execute_steps(run, plan, start=start, stop=len(plan.steps))

        run.state = RunState.executing
        self.db.commit()
        return await self._execute_steps(run, plan, start=0, stop=len(plan.steps), legacy_chat=True)

    async def _execute_steps(
        self,
        run: Run,
        plan: Plan,
        *,
        start: int,
        stop: int,
        legacy_chat: bool = False,
    ) -> Run:
        run.state = RunState.executing
        self.db.commit()
        self._bind_run(run.id)
        evidence: dict[str, Any] = {}
        ctx = self._ctx(run)
        last_step_name = "step"

        try:
            steps_sorted = sorted(run.steps, key=lambda s: s.seq)
            for step_row in steps_sorted[start:stop]:
                if step_row.status == "succeeded":
                    continue
                step_def = plan.steps[step_row.seq]
                last_step_name = step_def.name
                step_row.status = "running"
                step_row.started_at = datetime.now(timezone.utc)
                self.db.commit()
                await stream_bus.publish(
                    run.id,
                    "step_start",
                    {"name": step_def.name, "seq": step_row.seq},
                )
                await self._log(f"▶ step: {step_def.name}")

                if ctx.get("agent_loop"):
                    await stream_bus.publish(
                        run.id,
                        "agent_thinking",
                        {"text": step_label(step_def.name) + "…"},
                    )

                out = await self._run_step(run, step_def.name, step_def.params, evidence, ctx)
                step_row.output = out[:50000] if out else None
                step_row.status = "succeeded"
                step_row.finished_at = datetime.now(timezone.utc)
                self.db.commit()
                await stream_bus.publish(run.id, "step_end", {"name": step_def.name, "status": "succeeded"})
                self._save_ctx(run, ctx)

                if ctx.get("agent_loop"):
                    ctx.setdefault("agent_transcript", []).append(
                        {"step": step_def.name, "output": (out or "")[:2000]}
                    )
                    pending = [
                        plan.steps[s.seq].name
                        for s in steps_sorted
                        if s.seq > step_row.seq and s.seq < stop
                    ]
                    obs = await observe_after_step(
                        user_goal=ctx.get("user_goal", run.user_message),
                        skill_id=plan.skill_id,
                        step_name=step_def.name,
                        step_output=out,
                        transcript=ctx["agent_transcript"],
                        pending_step_names=pending,
                        run_state=str(run.state),
                    )
                    await self._agent_chat(run, ctx, obs.message, append=True)
                    await stream_bus.publish(run.id, "agent_thinking", {"text": ""})
                    if not obs.continue_execution:
                        raise RuntimeError(obs.message)

            if legacy_chat:
                run.state = RunState.verifying
                self.db.commit()
                if plan.skill_id == "incident.diagnose":
                    run.result_summary = await diagnose_with_llm(evidence)
                elif plan.domain_playbook:
                    run.result_summary = plan.summary_fa + "\n\nDomain:\n" + "\n".join(
                        f"- {p}" for p in plan.domain_playbook
                    )
                else:
                    run.result_summary = (plan.summary_en or plan.summary_fa) + " — completed."
                run.state = RunState.succeeded
                if ctx.get("agent_loop") and run.result_summary:
                    await self._agent_chat(run, ctx, run.result_summary, append=True)
                await stream_bus.publish(
                    run.id,
                    "succeeded",
                    {"site_url": run.site_url, "summary": run.result_summary},
                )
            elif stop >= len(plan.steps) and plan.skill_id == PIPELINE_SKILL:
                run.state = RunState.succeeded
                url = run.site_url or ""
                run.result_summary = f"Deploy complete. Open: {url}" if url else "Pipeline complete."
                await stream_bus.publish(
                    run.id,
                    "succeeded",
                    {"site_url": url, "summary": run.result_summary},
                )
            self.db.commit()
        except Exception as e:
            run.state = RunState.failed
            run.error_code = str(e).split(":")[0] if ":" in str(e) else "STEP_FAILED"
            run.error_message = str(e)
            self.db.commit()
            ctx = self._ctx(run)
            if ctx.get("agent_loop"):
                err_obs = await observe_after_step(
                    user_goal=ctx.get("user_goal", run.user_message),
                    skill_id=plan.skill_id,
                    step_name=last_step_name,
                    step_output=None,
                    transcript=ctx.get("agent_transcript", []),
                    pending_step_names=[],
                    run_state="failed",
                    error=str(e),
                )
                await self._agent_chat(run, ctx, err_obs.message, append=True)
                await stream_bus.publish(run.id, "agent_thinking", {"text": ""})
            await stream_bus.publish(run.id, "run_error", {"message": str(e)})
            await self._log(f"✖ {e}")
        self.db.refresh(run)
        return run

    async def _run_step(
        self,
        run: Run,
        name: str,
        params: dict[str, Any],
        evidence: dict[str, Any],
        ctx: dict[str, Any],
    ) -> str:
        async def on_line(line: str) -> None:
            await self._log(line)

        if name == "github_clone":
            info = await clone_repository(run.id, params["repo"], on_log=on_line)
            ctx["clone_path"] = info["path"]
            ctx["commit"] = info["commit"]
            return json.dumps(info)

        if name == "repo_analyze":
            root = deploy_root(run.id, normalize_subpath(params.get("subpath") or ctx.get("subpath")))
            analysis = analyze_local_directory(root)
            ctx["analysis"] = analysis
            ctx["deploy_path"] = str(root)
            ctx["stack"] = analysis.get("stack", "node")
            summary = json.dumps(analysis, indent=2)
            for f in analysis.get("findings", []):
                await self._log(f"  [{f.get('severity')}] {f.get('code')}: {f.get('message')}")
            return summary

        if name == "write_liara_config":
            root = deploy_root(run.id, normalize_subpath(params.get("subpath") or ctx.get("subpath")))
            app_name = params.get("app_name") or ctx.get("liara_app_name", "app")
            msg = write_liara_config(root, app_name, ctx.get("analysis") or {})
            await self._log(msg)
            return msg

        if name == "liara_auth_check" or name == "auth_check":
            r = await self.liara.auth_check()
            if not r.ok:
                raise RuntimeError(f"{r.code}: {r.message}")
            return r.message

        if name == "liara_create_app":
            app = params.get("app_name") or ctx.get("liara_app_name")
            platform = "next" if ctx.get("stack") == "next" else "node"
            r = await self.liara.create_project(app, platform=platform, on_line=on_line)
            if not r.ok:
                raise RuntimeError(f"{r.code}: {r.message}")
            ctx["liara_app_name"] = app
            return r.message

        if name == "liara_deploy" or name == "deploy" or name == "first_deploy":
            app = params.get("app_name") or params.get("app") or ctx.get("liara_app_name")
            path = params.get("path") or ctx.get("deploy_path") or str(
                deploy_root(run.id, normalize_subpath(ctx.get("subpath")))
            )
            port = int(params.get("port", 3000))
            r = await self.liara.deploy(app, path=path, port=port, on_line=on_line)
            if not r.ok:
                raise RuntimeError(f"{r.code}: {r.message}")
            return r.data.get("stdout", r.message) if r.data else r.message

        if name == "resolve_site_url":
            app = params.get("app_name") or ctx.get("liara_app_name")
            r = await self.liara.resolve_site_url(app)
            if not r.ok:
                raise RuntimeError(r.message)
            url = (r.data or {}).get("url", "")
            run.site_url = url
            ctx["site_url"] = url
            await stream_bus.publish(run.id, "site_url", {"url": url})
            await self._log(f"Site URL: {url}")
            return url

        if name == "verify_health" or name == "health_check":
            app = params.get("app_name") or params.get("app") or ctx.get("liara_app_name")
            url = params.get("url") or ctx.get("site_url") or self.liara.project_url(app or "my-app")
            r = await self.liara.health_http(url)
            evidence["health"] = r.data or {}
            status = (r.data or {}).get("status_code", 0)
            soft = run.skill_id == "incident.diagnose" or ctx.get("soft_health_check")
            if not r.ok or status >= 500:
                msg = f"HTTP health check: {r.message} {json.dumps(r.data or {})}"
                if soft:
                    evidence["health"]["degraded"] = True
                    return f"NOTE (continuing diagnosis): site returned {status or 'error'} — {msg}"
                raise RuntimeError(f"VERIFY_FAILED: {r.message}")
            return f"{r.message} {json.dumps(r.data or {})}"

        if name == "recommend_plan":
            return params.get("stack_hint", "node")

        if name == "tail_logs":
            app = params.get("app") or params.get("app_name") or ctx.get("liara_app_name") or "my-app"
            lines = int(params.get("lines", 120))
            since_h = params.get("since_hours") or ctx.get("log_since_hours") or 24
            got = await self.liara.get_service_logs(
                app_name=app,
                lines=lines,
                time_range={"since_hours": since_h},
            )
            if not got.ok:
                r = await self.liara.logs(app, lines=lines)
                if not r.ok:
                    raise RuntimeError(got.message or r.message)
                logs = (r.data or {}).get("logs", "")
            else:
                logs = (got.data or {}).get("logs", "")
            evidence["logs"] = logs
            evidence["log_window_hours"] = since_h
            return (logs or got.message)[:8000]

        if name == "fetch_metrics":
            app = params.get("app") or params.get("app_name") or ctx.get("liara_app_name") or "my-app"
            usage = await self.liara.get_service_resource_usage(app_name=app, since_hours=1)
            if usage.ok and usage.data:
                evidence["metrics"] = usage.data
                return usage.message[:4000]
            r = await self.liara.metrics(app)
            if not r.ok:
                evidence["metrics"] = {"limitation": True, "message": r.message}
                return f"LIMITATION: {r.message}"
            evidence["metrics"] = r.data
            return json.dumps(r.data, ensure_ascii=False)[:4000]

        if name == "diagnose_llm":
            return "evidence collected"

        raise RuntimeError(f"Unknown step: {name}")

    def approve(self, run_id: str) -> Run:
        run = self.db.get(Run, run_id)
        if not run:
            raise ValueError("Run not found")
        if run.state != RunState.awaiting_approval:
            raise ValueError("Run is not awaiting approval")
        run.approved_at = datetime.now(timezone.utc)
        run.state = RunState.executing
        self.db.commit()
        return run
