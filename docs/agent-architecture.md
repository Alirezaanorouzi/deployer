# Agent architecture (LLM-first)

## Flow

```
User message
  → orchestrator: chat-only? → agent.chat (no tools)
  → plan_with_agent (once): skill_id + params + intro
  → ReAct loop:
       plan_next_step (LLM) → call_tool | clarify | finish
       execute_tool → observations[]
       repeat until finish / max iterations
```

## Layers

| Layer | Responsibility |
|--------|----------------|
| **Orchestrator** | Greetings / pure chat vs tool run |
| **Intent planner** (`plan_with_agent`) | One-shot skill + structured `params` |
| **Step planner** (`plan_next_step`) | Every iteration: next tool from observations + history |
| **Tool harness** | `confirm_write`, allowlists, failure recovery, deploy step order |
| **Executor** | Run DB, SSE, Liara/GitHub adapters |

## What is *not* rule-based anymore

- `forced_skill_action` pipelines are **not** used in the ReAct loop (settings, history, env, disks, …).
- No `judge_goal` / `evaluate_goal` after each tool — the step planner decides when to `finish`.
- Skill overrides in `planner_agent` are **minimal** (chat-only, recap, advisory follow-up).

## What stays deterministic (on purpose)

- **`confirm_write`** on Liara write tools
- **`liara.deploy`** forced step chain if the LLM tries to `finish` too early
- **Failure harness** after tool errors (optional log/diagnose follow-up)
- **Param enrichment** (`app` from UI, `enrich_planner_params` — not intent routing)

## Extending

- Add tools in `app/agent/tools/registry.py` and `_SKILL_TOOL_HINTS`.
- Teach recipes in `plan_next_step` system prompt.
- Avoid new keyword routers in `planner_agent` unless safety-critical.
