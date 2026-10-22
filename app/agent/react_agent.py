"""ReAct-style agent: plan → pick tool → execute → evaluate → replan or respond."""

import json
from typing import Any

from app.agent.tools.registry import ToolResult, execute_tool, list_tool_catalog, tools_for_skill
from app.config import get_settings
from app.llm.client import get_llm_client
from app.models import Run, RunState
from app.streaming import stream_bus


async def _llm_json(system: str, user: str) -> dict[str, Any]:
    client = get_llm_client()
    settings = get_settings()
    kwargs: dict = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    if "boxapi" not in settings.llm_base_url.lower():
        kwargs["temperature"] = 0.15
    resp = await client.chat.completions.create(**kwargs)
    raw = (resp.choices[0].message.content or "{}").strip()
    return json.loads(raw)


async def select_next_action(
    user_goal: str,
    observations: list[dict[str, Any]],
    *,
    allowed_tool_ids: list[str],
    planner_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = [t for t in list_tool_catalog() if t["id"] in allowed_tool_ids]
    system = """You are the tool-selection planner for a Liara DevOps agent.
Pick the next action to satisfy the user's goal.

Return ONLY JSON:
{
  "action": "call_tool" | "finish" | "clarify",
  "tool_id": "<id from allowed_tools or null>",
  "tool_params": {},
  "message": "user-facing text when action is finish or clarify"
}

Rules:
- Use call_tool when a tool can gather facts needed for the goal.
- Use finish when observations already contain enough data to answer the user (set message to the final answer).
- Use clarify only if the goal is ambiguous and no tool helps.
- tool_id must be one of allowed_tools when action is call_tool.
- Do not repeat the same tool with identical params if it already succeeded in observations.
- For logs: if observations show LOGS_EMPTY or a narrow window, call again with larger since_hours or different since_ts/until_ts.
- For write tools (restart, deploy, env, domain, disk, plan change, scale, create_app, deploy_service): explain impact first; only call with confirm_write true after user approved. If CONFIRMATION_REQUIRED, ask for approval — do not set confirm_write without user consent.
- For liara.deploy: clone → prepare → create_app → **liara.deploy_service** → get_app_url → verify_app_health; on failed deploy or HTTP 5xx run **liara.diagnose_deploy_health** (logs + fix hints). Never finish after create_app alone.
- For disks: list_app_disks first; disk names must use hyphens (shop-cache). If DISK_CREATE_FAILED mentions max_disks_reached, do not retry create — tell the user the plan disk limit is reached. If INVALID_DISK_NAME, fix the name and retry once."""

    user = json.dumps(
        {
            "user_goal": user_goal,
            "allowed_tools": catalog,
            "observations": observations[-8:],
            "planner_params": planner_params or {},
        },
        ensure_ascii=False,
    )
    data = await _llm_json(system, user)
    action = data.get("action", "call_tool")
    if action not in ("call_tool", "finish", "clarify"):
        action = "call_tool"
    return {
        "action": action,
        "tool_id": data.get("tool_id"),
        "tool_params": data.get("tool_params") or {},
        "message": data.get("message") or "",
    }


_REPLY_SYSTEM = """You are the Liara DevOps agent on Liara cloud.
Answer the user's goal using ONLY the tool observations provided — never invent balances, counts, or API results.
Write in the same language as the user (Persian or English). Use clear prose; **bold** for key values is OK.
If observations show errors, explain what failed and what to try next."""


async def stream_user_reply(
    executor: Any,
    run: Run,
    ctx: dict[str, Any],
    user_goal: str,
    observations: list[dict[str, Any]],
) -> str:
    user = json.dumps(
        {"user_goal": user_goal, "observations": observations[-8:]},
        ensure_ascii=False,
    )
    return await executor._agent_chat_stream(
        run,
        ctx,
        system=_REPLY_SYSTEM,
        user=user,
        temperature=0.25,
        new_block=True,
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
- satisfied=true for deploy only after a successful deploy or verify_app_health with HTTP 2xx/3xx."""

    user = json.dumps({"user_goal": user_goal, "observations": observations[-8:]}, ensure_ascii=False)
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
) -> dict[str, Any] | None:
    """Deterministic GitHub→Liara steps so we never stop at create_app without deploy."""
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
    user_message = await stream_user_reply(executor, run, ctx, user_goal, observations)
    verdict = await judge_goal(user_goal, observations)
    if skill_id == "liara.deploy" and not _deploy_pipeline_complete(observations):
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

    if run.skill_id == "agent.summarize" or ctx.get("summarize_only"):
        await stream_bus.publish(run_id, "agent_thinking", {"text": "Summarizing our conversation…"})
        try:
            user = json.dumps(
                {
                    "user_goal": goal,
                    "conversation_history": (ctx.get("conversation_history") or [])[-12:],
                },
                ensure_ascii=False,
            )
            system = """You are the Liara DevOps agent. The user wants a conclusion or recap.
Use ONLY facts from the conversation history — do not invent metrics.
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
    max_iter = 16 if skill_id == "liara.deploy" else max_iterations

    run.state = RunState.executing
    executor.db.commit()

    intro = (ctx.get("planner_intro") or "").strip()
    if intro:
        await executor._agent_chat(run, ctx, intro, append=False)
        await stream_bus.publish(run_id, "agent_message", {"text": intro, "append": False})

    final_message = ""
    try:
        for _ in range(max_iter):
            allowed = tools_for_skill(skill_id, ctx.get("active_app"))
            await stream_bus.publish(run_id, "agent_thinking", {"text": "Planning next step…"})

            planner_params = ctx.get("planner_params") or {}
            action = None
            if skill_id == "liara.deploy":
                action = _forced_liara_deploy_action(observations, planner_params)
                if action and action.get("tool_id"):
                    await stream_bus.publish(
                        run_id,
                        "agent_thinking",
                        {"text": f"Deploy pipeline → {action['tool_id']}…"},
                    )
            if not action:
                action = await select_next_action(
                    goal,
                    observations,
                    allowed_tool_ids=allowed,
                    planner_params=planner_params,
                )
                if (
                    skill_id == "liara.deploy"
                    and action.get("action") == "finish"
                    and not _deploy_pipeline_complete(observations)
                ):
                    action = _forced_liara_deploy_action(observations, planner_params) or action

            if action["action"] == "clarify":
                hint = action["message"] or ""
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
                    final_message = await stream_user_reply(executor, run, ctx, goal, observations)
                else:
                    final_message = await executor._agent_chat_stream(
                        run,
                        ctx,
                        system=_REPLY_SYSTEM,
                        user=json.dumps({"user_goal": goal}, ensure_ascii=False),
                        new_block=True,
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
            ):
                if tool_params.get(key) is None and planner_params.get(key) is not None:
                    tool_params[key] = planner_params[key]

            async def _on_log(line: str) -> None:
                await executor._log(line)

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

            if skill_id == "liara.deploy" and not _deploy_pipeline_complete(observations):
                # No LLM between pipeline steps — create_app alone leaves app "ready" not "running".
                verdict = {"satisfied": False, "replan": True, "user_message": ""}
            else:
                verdict = await evaluate_goal(
                    executor, run, ctx, goal, observations, skill_id=skill_id
                )
            if _deploy_blocked_by_failure(skill_id, observations):
                err_msg = _last_tool_error(observations)
                await executor._agent_chat(run, ctx, err_msg, append=True)
                verdict = {"satisfied": False, "replan": False, "user_message": err_msg}

            if verdict["satisfied"]:
                final_message = verdict["user_message"]
                run.state = RunState.succeeded
                run.result_summary = final_message
                break

            if not verdict["replan"]:
                run.state = RunState.failed
                run.error_message = verdict["user_message"] or "Could not satisfy goal"
                run.error_code = "GOAL_NOT_MET"
                await stream_bus.publish(run_id, "run_error", {"message": run.error_message})
                break

        else:
            run.state = RunState.failed
            run.error_message = "Agent exceeded max tool iterations"
            run.error_code = "MAX_ITERATIONS"
            await stream_bus.publish(run_id, "run_error", {"message": run.error_message})

        executor.db.commit()
        await stream_bus.publish(run_id, "agent_thinking", {"text": ""})
        if run.state == RunState.succeeded:
            await stream_bus.publish(
                run_id,
                "succeeded",
                {"site_url": run.site_url, "summary": run.result_summary},
            )
    except Exception as e:
        run.state = RunState.failed
        run.error_message = str(e)
        run.error_code = "REACT_FAILED"
        executor.db.commit()
        await stream_bus.publish(run_id, "run_error", {"message": str(e)})
        await executor._log(f"✖ {e}")
