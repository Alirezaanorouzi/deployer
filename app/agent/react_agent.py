"""LLM-first ReAct: intent (once) → plan_next_step → tool → observe → repeat until finish."""

import asyncio
import json
from typing import Any

from app.agent.context_builder import (
    CHAT_ONLY_SYSTEM,
    REACT_PLANNER_SYSTEM,
    REPLY_SYSTEM,
    build_chat_payload,
    build_judge_payload,
    build_react_planner_payload,
    build_reply_payload,
)
from app.agent.conversation_params import enrich_planner_params
from app.agent.deploy_params import enrich_deploy_params, infer_deploy_repo
from app.agent.failure_harness import (
    build_max_iterations_guide,
    format_react_exception,
    plan_failure_recovery,
)
from app.agent.skill_pipelines import PIPELINE_SKILLS
from app.agent.tools.registry import ToolResult, execute_tool, list_tool_catalog, tools_for_skill
from app.config import get_settings
from app.llm.chat_json import chat_json_object
from app.llm.client import get_llm_client
from app.models import Run, RunState
from app.streaming import stream_bus


async def _llm_json(system: str, user: str) -> dict[str, Any]:
    client = get_llm_client()
    settings = get_settings()
    return await chat_json_object(
        client,
        settings,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.15,
    )


async def plan_next_step(
    user_goal: str,
    observations: list[dict[str, Any]],
    *,
    allowed_tool_ids: list[str],
    planner_params: dict[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    skill_id: str | None = None,
) -> dict[str, Any]:
    """Single LLM call per loop iteration: choose tool, clarify, or finish from observations."""
    catalog = [t for t in list_tool_catalog() if t["id"] in allowed_tool_ids]
    planner_body = json.loads(
        build_react_planner_payload(
            skill_id=skill_id,
            user_goal=user_goal,
            conversation_history=conversation_history,
            observations=observations,
            planner_params=planner_params,
        )
    )
    planner_body["allowed_tools"] = catalog
    user = json.dumps(planner_body, ensure_ascii=False)
    data = await _llm_json(REACT_PLANNER_SYSTEM, user)
    action = data.get("action", "call_tool")
    if action not in ("call_tool", "finish", "clarify"):
        action = "call_tool"
    return {
        "action": action,
        "tool_id": data.get("tool_id"),
        "tool_params": data.get("tool_params") or {},
        "message": data.get("message") or "",
    }


async def stream_user_reply(
    executor: Any,
    run: Run,
    ctx: dict[str, Any],
    user_goal: str,
    observations: list[dict[str, Any]],
    *,
    latest_user_message: str | None = None,
) -> str:
    user = build_reply_payload(
        user_goal=user_goal,
        latest_user_message=latest_user_message or run.user_message,
        conversation_history=ctx.get("conversation_history"),
        observations=observations,
    )
    return await executor._agent_chat_stream(
        run,
        ctx,
        system=REPLY_SYSTEM,
        user=user,
        temperature=0.25,
        new_block=False,
        replace=True,
    )


async def judge_goal(
    user_goal: str,
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    system = """Judge whether the user's goal is satisfied by tool observations.

Return ONLY JSON:
{
  "satisfied": true | false,
  "replan": true | false
}

Rules:
- satisfied=true when the goal is fully answered.
- replan=true when satisfied=false and a different tool or retry could help.
- replan=false when satisfied=false and stopping is appropriate.
- For logs: LOGS_EMPTY → satisfied=false, replan=true.
- For deploy/publish goals: satisfied=false if clone, create_app, or deploy_service failed (CLONE_FAILED, CREATE_APP_FAILED, DEPLOY_FAILED) unless the user only asked why it failed.
- satisfied=true for deploy only after a successful deploy or verify_app_health with HTTP 2xx/3xx.
- For liara.health / status questions: satisfied=true once verify_app_health or diagnose_deploy_health explains HTTP status and next steps — even if the site is down (502/503)."""

    user = build_judge_payload(user_goal, observations)
    data = await _llm_json(system, user)
    return {
        "satisfied": bool(data.get("satisfied")),
        "replan": bool(data.get("replan")),
    }


_DEPLOY_FAIL_CODES = frozenset(
    {
        "CLONE_FAILED",
        "CREATE_APP_FAILED",
        "DEPLOY_FAILED",
        "PLATFORM_UNKNOWN",
        "INVALID_INPUT",
        "CONFIRMATION_REQUIRED",
    }
)


def _last_tool_error(observations: list[dict[str, Any]]) -> str:
    for obs in reversed(observations):
        if not obs.get("ok"):
            text = (obs.get("output") or "").strip()
            code = obs.get("code") or "ERROR"
            tool = obs.get("tool") or "tool"
            if text:
                return f"**{tool}** failed ({code}): {text}"
            return f"**{tool}** failed ({code})."
    return "Deploy step failed."


def _deploy_blocked_by_failure(skill_id: str, observations: list[dict[str, Any]]) -> bool:
    if skill_id != "liara.deploy":
        return False
    if _last_obs_for_tool(observations, "liara.diagnose_deploy_health"):
        return False
    for obs in observations:
        if obs.get("ok"):
            continue
        code = obs.get("code") or ""
        tool = obs.get("tool") or ""
        if code in _DEPLOY_FAIL_CODES or tool in ("repo.clone_github", "liara.deploy_service", "liara.create_app"):
            return True
    return False


_LIARA_DEPLOY_CHAIN = (
    "repo.clone_github",
    "repo.prepare_deploy_workspace",
    "liara.create_app",
    "liara.deploy_service",
    "liara.get_app_url",
    "liara.verify_app_health",
)

# If verify fails with 5xx, agent may call diagnose_deploy_health before finishing.


def _last_obs_for_tool(observations: list[dict[str, Any]], tool_id: str) -> dict[str, Any] | None:
    for obs in reversed(observations):
        if obs.get("tool") == tool_id:
            return obs
    return None


def _deploy_pipeline_complete(observations: list[dict[str, Any]]) -> bool:
    verify = _last_obs_for_tool(observations, "liara.verify_app_health")
    if verify and verify.get("ok"):
        return True
    diag = _last_obs_for_tool(observations, "liara.diagnose_deploy_health")
    if diag is not None:
        # Ran after failed deploy or failed health — stop forcing tools; summarize for user.
        return True
    return False


def _needs_deploy_diagnosis(observations: list[dict[str, Any]]) -> bool:
    if _last_obs_for_tool(observations, "liara.diagnose_deploy_health"):
        return False
    verify = _last_obs_for_tool(observations, "liara.verify_app_health")
    if verify and not verify.get("ok"):
        return True
    deploy = _last_obs_for_tool(observations, "liara.deploy_service")
    if deploy and not deploy.get("ok"):
        return True
    return False


def _tool_params_for_deploy_step(tool_id: str, planner_params: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if planner_params.get("confirm_write"):
        params["confirm_write"] = True
    if tool_id == "repo.clone_github":
        if planner_params.get("repo"):
            params["repo"] = planner_params["repo"]
        if planner_params.get("subpath"):
            params["subpath"] = planner_params["subpath"]
        repos = planner_params.get("github_repos")
        if isinstance(repos, list):
            params["github_repos"] = repos
    elif tool_id == "repo.prepare_deploy_workspace":
        for key in ("repo", "subpath", "app_name", "liara_app_name", "deploy_path"):
            if planner_params.get(key) is not None:
                params[key] = planner_params[key]
    elif tool_id in (
        "liara.create_app",
        "liara.deploy_service",
        "liara.get_app_url",
        "liara.verify_app_health",
        "liara.diagnose_deploy_health",
    ):
        app = (
            planner_params.get("liara_app_name")
            or planner_params.get("app_name")
            or planner_params.get("app")
        )
        if app:
            params["app_name"] = app
        if planner_params.get("url"):
            params["url"] = planner_params["url"]
        if tool_id == "liara.create_app" and planner_params.get("platform"):
            params["platform"] = planner_params["platform"]
        if tool_id == "liara.deploy_service":
            if planner_params.get("deploy_path"):
                params["deploy_path"] = planner_params["deploy_path"]
            if planner_params.get("port") is not None:
                params["port"] = planner_params["port"]
        if tool_id in ("liara.verify_app_health", "liara.diagnose_deploy_health"):
            if planner_params.get("health_url"):
                params["health_url"] = planner_params["health_url"]
    return params


def _forced_liara_deploy_action(
    observations: list[dict[str, Any]],
    planner_params: dict[str, Any],
    *,
    user_goal: str = "",
) -> dict[str, Any] | None:
    """Deterministic GitHub→Liara steps so we never stop at create_app without deploy."""
    if user_goal:
        enrich_deploy_params(user_goal, planner_params, planner_params.get("github_repos"))
    if _deploy_pipeline_complete(observations):
        return None
    if _needs_deploy_diagnosis(observations):
        return {
            "action": "call_tool",
            "tool_id": "liara.diagnose_deploy_health",
            "tool_params": _tool_params_for_deploy_step("liara.diagnose_deploy_health", planner_params),
            "message": "",
        }
    for tool_id in _LIARA_DEPLOY_CHAIN:
        last = _last_obs_for_tool(observations, tool_id)
        if last is None:
            if tool_id == "repo.clone_github" and not planner_params.get("repo"):
                repos = planner_params.get("github_repos") or []
                hint = infer_deploy_repo(user_goal, repos if isinstance(repos, list) else None)
                if hint:
                    planner_params["repo"] = hint
                else:
                    return {
                        "action": "clarify",
                        "message": (
                            "Which GitHub repository should I deploy? "
                            "Please send **owner/repo** (e.g. Alirezaanorouzi/deployer)."
                        ),
                    }
            return {
                "action": "call_tool",
                "tool_id": tool_id,
                "tool_params": _tool_params_for_deploy_step(tool_id, planner_params),
                "message": "",
            }
        if last.get("ok"):
            continue
        if last.get("code") == "CONFIRMATION_REQUIRED":
            return {
                "action": "call_tool",
                "tool_id": tool_id,
                "tool_params": _tool_params_for_deploy_step(tool_id, planner_params),
                "message": "",
            }
        return None
    return None


async def evaluate_goal(
    executor: Any,
    run: Run,
    ctx: dict[str, Any],
    user_goal: str,
    observations: list[dict[str, Any]],
    *,
    skill_id: str | None = None,
) -> dict[str, Any]:
    user_message = await stream_user_reply(
        executor, run, ctx, user_goal, observations, latest_user_message=run.user_message
    )
    verdict = await judge_goal(user_goal, observations)
    if skill_id == "liara.deploy" and not _deploy_pipeline_complete(observations):
        verdict["satisfied"] = False
        verdict["replan"] = True
    if skill_id in PIPELINE_SKILLS:
        planner_params = ctx.get("planner_params") or {}
        if write_pending(
            skill_id,
            planner_params,
            user_goal,
            observations,
            conversation_history=ctx.get("conversation_history"),
        ):
            verdict["satisfied"] = False
            verdict["replan"] = True
    return {**verdict, "user_message": user_message}


async def run_react_agent(executor: Any, run_id: str, max_iterations: int = 6) -> None:
    """Run tool loop for runs with context react_mode=true."""
    from app.executor import RunExecutor  # noqa: F401 — type hint only

    run = executor.db.get(Run, run_id)
    if not run:
        return
    executor._bind_run(run_id)
    ctx = executor._ctx(run)
    if not ctx.get("react_mode"):
        return

    goal = ctx.get("user_goal") or run.user_message

    if run.skill_id == "agent.chat" or ctx.get("chat_only"):
        await stream_bus.publish(run_id, "agent_thinking", {"text": ""})
        try:
            chat_user = build_chat_payload(run.user_message, ctx.get("conversation_history"))
            text = await executor._agent_chat_stream(
                run,
                ctx,
                system=CHAT_ONLY_SYSTEM,
                user=chat_user,
                temperature=0.35,
                new_block=False,
            )
            if not text:
                text = "سلام — بگو چه کاری روی لیارا انجام بدهم."
            run.state = RunState.succeeded
            run.result_summary = text
            executor.db.commit()
            await stream_bus.publish(run_id, "succeeded", {"site_url": None, "summary": text})
        except Exception as e:
            run.state = RunState.failed
            run.error_message = str(e)
            executor.db.commit()
            await stream_bus.publish(run_id, "run_error", {"message": str(e)})
        return

    if run.skill_id == "agent.summarize" or ctx.get("summarize_only"):
        await stream_bus.publish(run_id, "agent_thinking", {"text": "Summarizing our conversation…"})
        try:
            user = build_chat_payload(goal, ctx.get("conversation_history"))
            system = """You are the Liara DevOps agent. The user wants a conclusion or recap.
Use ONLY facts from conversation_history — do not invent metrics.
Write in the user's language (Persian or English). Brief, friendly summary."""
            text = await executor._agent_chat_stream(
                run,
                ctx,
                system=system,
                user=user,
                temperature=0.3,
                new_block=False,
            )
            if not text:
                text = "I don't have enough prior messages to summarize."
            run.state = RunState.succeeded
            run.result_summary = text
            executor.db.commit()
            await stream_bus.publish(run_id, "agent_thinking", {"text": ""})
            await stream_bus.publish(run_id, "succeeded", {"site_url": None, "summary": text})
        except Exception as e:
            run.state = RunState.failed
            run.error_message = str(e)
            executor.db.commit()
            await stream_bus.publish(run_id, "run_error", {"message": str(e)})
        return

    goal = ctx.get("user_goal") or run.user_message
    skill_id = run.skill_id or "agent.tools"
    observations: list[dict[str, Any]] = list(ctx.get("observations") or [])
    max_iter = 16 if skill_id in ("liara.deploy", *PIPELINE_SKILLS) else max_iterations
    conversation_history = ctx.get("conversation_history") or []

    run.state = RunState.executing
    executor.db.commit()

    intro = (ctx.get("planner_intro") or "").strip()
    if intro:
        await stream_bus.publish(run_id, "agent_thinking", {"text": intro})

    final_message = ""
    try:
        for _ in range(max_iter):
            ctx = executor._ctx(run)
            if ctx.get("cancel_requested"):
                run.state = RunState.cancelled
                run.result_summary = "Run cancelled."
                run.error_message = "Cancelled"
                break
            allowed = tools_for_skill(skill_id, ctx.get("active_app"))
            await stream_bus.publish(run_id, "agent_thinking", {"text": "Planning next step…"})

            planner_params = ctx.get("planner_params") or {}
            enrich_planner_params(
                skill_id,
                goal,
                planner_params,
                conversation_history=conversation_history,
                liara_projects=ctx.get("liara_projects"),
                github_repos=ctx.get("github_repos"),
                default_app=ctx.get("active_app"),
            )
            ctx["planner_params"] = planner_params
            executor._save_ctx(run, ctx)

            action = ctx.pop("recovery_follow_up", None)
            if action:
                await stream_bus.publish(
                    run_id,
                    "agent_thinking",
                    {"text": "Recovering from error…"},
                )
            if not action and skill_id == "liara.deploy":
                action = _forced_liara_deploy_action(observations, planner_params, user_goal=goal)
                if action and action.get("tool_id"):
                    await stream_bus.publish(
                        run_id,
                        "agent_thinking",
                        {"text": f"Deploy pipeline → {action['tool_id']}…"},
                    )
            if not action:
                action = await plan_next_step(
                    goal,
                    observations,
                    allowed_tool_ids=allowed,
                    planner_params=planner_params,
                    conversation_history=conversation_history,
                    skill_id=skill_id,
                )
                if (
                    skill_id == "liara.deploy"
                    and action.get("action") == "finish"
                    and not _deploy_pipeline_complete(observations)
                ):
                    action = _forced_liara_deploy_action(observations, planner_params, user_goal=goal) or action

            if action["action"] == "clarify":
                hint = action["message"] or ""
                if hint.strip():
                    final_message = hint.strip()
                    await executor._agent_chat(run, ctx, final_message, append=True)
                else:
                    user = json.dumps({"user_goal": goal, "hint": hint}, ensure_ascii=False)
                    final_message = await executor._agent_chat_stream(
                        run,
                        ctx,
                        system="Ask one short clarifying question about what the user wants on Liara.",
                        user=user,
                        temperature=0.2,
                        new_block=True,
                    )
                    if not final_message:
                        final_message = "Could you clarify what you need on Liara?"
                run.state = RunState.succeeded
                run.result_summary = final_message
                break

            if action["action"] == "finish":
                if observations:
                    final_message = await stream_user_reply(
                        executor, run, ctx, goal, observations, latest_user_message=run.user_message
                    )
                else:
                    final_message = await executor._agent_chat_stream(
                        run,
                        ctx,
                        system=REPLY_SYSTEM,
                        user=json.dumps({"user_goal": goal}, ensure_ascii=False),
                        replace=True,
                    )
                if not final_message:
                    final_message = action.get("message") or "Done."
                run.state = RunState.succeeded
                run.result_summary = final_message
                break

            tool_id = action.get("tool_id")
            if not tool_id or tool_id not in allowed:
                observations.append(
                    {"tool": tool_id or "none", "ok": False, "output": "Invalid tool selection; replanning."}
                )
                continue

            await stream_bus.publish(
                run_id,
                "tool_call",
                {"tool_id": tool_id, "params": action.get("tool_params") or {}},
            )
            await executor._log(f"▶ tool: {tool_id}")
            if tool_id == "liara.deploy_service":
                await stream_bus.publish(
                    run_id,
                    "agent_thinking",
                    {
                        "text": "Deploying via Liara CLI (may take several minutes; terminal shows live output)…",
                    },
                )
            else:
                await stream_bus.publish(run_id, "agent_thinking", {"text": f"Calling {tool_id}…"})

            tool_params = dict(action.get("tool_params") or {})
            planner_params = ctx.get("planner_params") or {}
            if not tool_params.get("project_id") and not tool_params.get("app_name"):
                for key in ("app_name", "project_id", "app"):
                    if planner_params.get(key):
                        tool_params["app_name"] = planner_params[key]
                        break
            if tool_params.get("since_hours") is None and planner_params.get("since_hours") is not None:
                tool_params["since_hours"] = planner_params["since_hours"]
            if tool_params.get("lines") is None and planner_params.get("lines") is not None:
                tool_params["lines"] = planner_params["lines"]
            if not tool_params.get("focus") and planner_params.get("focus"):
                tool_params["focus"] = planner_params["focus"]
            if not tool_params.get("user_goal"):
                tool_params["user_goal"] = goal
            for key in ("repo", "subpath", "platform", "github_repos", "liara_app_name"):
                pv = planner_params.get(key)
                if pv is not None and pv != "" and not tool_params.get(key):
                    tool_params[key] = pv
            if ctx.get("active_app") and not tool_params.get("app_name"):
                tool_params["app_name"] = ctx["active_app"]
            if not tool_params.get("app_name"):
                tool_params["app_name"] = (
                    planner_params.get("liara_app_name")
                    or planner_params.get("app_name")
                )

            for key in (
                "since_hours",
                "since_ts",
                "until_ts",
                "since_iso",
                "until_iso",
                "page",
                "count",
                "search",
                "start_ts",
                "end_ts",
                "confirm_write",
                "plan_id",
                "scale",
                "env_key",
                "env_value",
                "variables",
                "enabled",
                "domain_name",
                "domain_id",
                "redirect_to",
                "redirect_status",
                "disk_name",
                "size_gb",
                "deploy_path",
                "port",
                "source_id",
                "platform_type",
                "ftp_username",
                "ftp_name",
                "read_only",
            ):
                if tool_params.get(key) is None and planner_params.get(key) is not None:
                    tool_params[key] = planner_params[key]

            async def _on_log(line: str) -> None:
                try:
                    await executor._log(line)
                except Exception:
                    pass

            if tool_id == "liara.create_app" and not tool_params.get("platform"):
                if planner_params.get("platform"):
                    tool_params["platform"] = planner_params["platform"]
                elif not tool_params.get("deploy_path") and planner_params.get("deploy_path"):
                    tool_params["deploy_path"] = planner_params["deploy_path"]

            result: ToolResult = await execute_tool(
                tool_id,
                liara=executor.liara,
                params=tool_params,
                run_id=run_id,
                on_log=_on_log,
            )
            if result.ok and result.data:
                if tool_id == "liara.get_app_url" and result.data.get("url"):
                    run.site_url = str(result.data["url"])
                    planner_params["url"] = str(result.data["url"])
                    ctx["planner_params"] = planner_params
                active = result.data.get("active_app") or result.data.get("app_name")
                if tool_id in ("liara.bind_app", "liara.create_app") and active:
                    ctx["active_app"] = str(active)
                    planner_params["app_name"] = str(active)
                    planner_params["liara_app_name"] = str(active)
                    ctx["planner_params"] = planner_params
                if tool_id == "repo.prepare_deploy_workspace":
                    for k in ("deploy_path", "port", "platform", "app_name", "stack"):
                        if result.data.get(k) is not None:
                            planner_params[k] = result.data[k]
                    ctx["planner_params"] = planner_params
                if tool_id == "liara.create_app" and result.data.get("platform"):
                    planner_params["platform"] = result.data["platform"]
                    ctx["planner_params"] = planner_params
            executor._save_ctx(run, ctx)
            obs_entry = {
                "tool": tool_id,
                "ok": result.ok,
                "code": result.code,
                "output": result.text_for_agent,
                "data": result.data,
            }
            observations.append(obs_entry)
            ctx["observations"] = observations
            executor._save_ctx(run, ctx)

            await executor._log(result.text_for_agent[:500])
            await stream_bus.publish(run_id, "agent_thinking", {"text": "Reviewing results…"})

            recovery_verdict: dict[str, Any] | None = None
            if not result.ok:
                tried: list[str] = list(ctx.get("recovery_tried") or [])
                rec = plan_failure_recovery(
                    skill_id=skill_id,
                    tool_id=tool_id,
                    code=result.code or "ERROR",
                    output=result.text_for_agent,
                    observations=observations,
                    planner_params=planner_params,
                    user_goal=goal,
                    recovery_tried=tried,
                )
                if rec.terminal_line:
                    await executor._log(f"↻ {rec.terminal_line}")
                if rec.thinking:
                    await stream_bus.publish(run_id, "agent_thinking", {"text": rec.thinking})
                if rec.recovery_key and rec.recovery_key not in tried:
                    tried.append(rec.recovery_key)
                    ctx["recovery_tried"] = tried
                follow_tid = (rec.follow_up or {}).get("tool_id")
                if rec.follow_up and follow_tid in allowed:
                    ctx["recovery_follow_up"] = rec.follow_up
                    executor._save_ctx(run, ctx)
                    recovery_verdict = {"satisfied": False, "replan": True, "user_message": ""}
                elif rec.follow_up and follow_tid not in allowed:
                    rec.hint = (
                        f"{rec.hint} Suggested tool {follow_tid} is not in allowlist for this skill."
                    ).strip()
                elif rec.guide_message:
                    recovery_verdict = {
                        "satisfied": True,
                        "replan": False,
                        "user_message": rec.guide_message,
                    }
                elif rec.hint:
                    hints: list[str] = list(ctx.get("failure_hints") or [])
                    hints.append(rec.hint)
                    ctx["failure_hints"] = hints[-8:]
                    planner_params["failure_recovery_hints"] = ctx["failure_hints"]
                    ctx["planner_params"] = planner_params
                    executor._save_ctx(run, ctx)

            if recovery_verdict is not None and recovery_verdict.get("satisfied"):
                final_message = recovery_verdict.get("user_message") or ""
                if not final_message.strip() and observations:
                    final_message = await stream_user_reply(
                        executor,
                        run,
                        ctx,
                        goal,
                        observations,
                        latest_user_message=run.user_message,
                    )
                run.result_summary = final_message or "Done."
                if skill_id == "liara.deploy" and _deploy_blocked_by_failure(
                    skill_id, observations
                ):
                    run.state = RunState.failed
                    run.error_code = run.error_code or "DEPLOY_FAILED"
                    run.error_message = (final_message or "Deploy failed")[:2000]
                else:
                    run.state = RunState.succeeded
                break
            if _deploy_blocked_by_failure(skill_id, observations):
                err_msg = _last_tool_error(observations)
                await executor._agent_chat(run, ctx, err_msg, append=True)
                run.state = RunState.failed
                run.error_code = "DEPLOY_FAILED"
                run.error_message = err_msg[:2000]
                run.result_summary = err_msg
                break
            continue

        else:
            guide = build_max_iterations_guide(observations)
            run.state = RunState.succeeded
            run.result_summary = guide
            run.error_code = "MAX_ITERATIONS"
            run.error_message = None

        executor.db.commit()
        await stream_bus.publish(run_id, "agent_thinking", {"text": ""})
        if run.state == RunState.succeeded:
            await stream_bus.publish(
                run_id,
                "succeeded",
                {"site_url": run.site_url, "summary": run.result_summary},
            )
    except asyncio.CancelledError:
        run.state = RunState.executing
        run.error_message = None
        executor.db.commit()
        await stream_bus.publish(
            run_id,
            "agent_thinking",
            {"text": "Deploy interrupted (server reload); will resume if auto-resume is enabled."},
        )
        raise
    except Exception as e:
        guide = format_react_exception(e)
        try:
            ctx = executor._ctx(run)
            if observations:
                ctx["observations"] = observations
                executor._save_ctx(run, ctx)
        except Exception:
            pass
        incomplete_deploy = run.skill_id == "liara.deploy" and not _deploy_pipeline_complete(
            observations
        )
        run.result_summary = guide
        run.error_code = "REACT_FAILED"
        run.error_message = str(e) or type(e).__name__
        if incomplete_deploy:
            run.state = RunState.failed
            executor.db.commit()
            await stream_bus.publish(run_id, "run_error", {"message": guide})
        else:
            run.state = RunState.succeeded
            executor.db.commit()
            await stream_bus.publish(
                run_id,
                "succeeded",
                {"site_url": run.site_url, "summary": guide},
            )
        await executor._log(f"✖ {e}")
