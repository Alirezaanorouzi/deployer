"""Simulate 5 sequential chat turns with session history (like the UI)."""

import json
import time
import urllib.request

API = "http://127.0.0.1:8000"
KEY = "dev-local-key"

TURNS = [
    "Check my account remaining balance on Liara",
    "Get CPU and memory resource usage for my qaim-magham service",
    "Analyze logs for qaim-magham in the last 24 hours — any errors?",
    "Now narrow to the last 2 hours only and focus on HTTP 500 errors",
    "Give me a brief conclusion: balance, resources, and logs for what we checked",
]


def post_chat(message: str, history: list[dict]) -> dict:
    body = json.dumps(
        {"message": message, "target_env": "staging", "history": history},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/chat",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def get_run(run_id: str) -> dict:
    req = urllib.request.Request(
        f"{API}/runs/{run_id}",
        headers={"X-API-Key": KEY},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def wait_run(run_id: str, timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = get_run(run_id)
        if run.get("state") in ("succeeded", "failed"):
            return run
        time.sleep(3)
    return get_run(run_id)


def main() -> None:
    history: list[dict] = []
    results = []

    for i, message in enumerate(TURNS, 1):
        print(f"\n{'='*60}\nTurn {i}: {message}\n{'='*60}")
        started = time.time()
        try:
            out = post_chat(message, history)
        except Exception as e:
            results.append({"turn": i, "error": str(e)})
            print("CHAT ERROR:", e)
            continue

        run_id = out["id"]
        skill = out.get("skill_id")
        print(f"Run {run_id} skill={skill} initial_state={out.get('state')}")

        run = wait_run(run_id)
        elapsed = time.time() - started
        agent_msg = run.get("agent_message") or out.get("agent_message") or ""
        summary = run.get("result_summary") or ""
        state = run.get("state")

        display = (agent_msg or summary)[:1200]
        print(f"state={state} ({elapsed:.0f}s)")
        print(display)

        if state == "succeeded":
            assistant_text = summary or agent_msg
        else:
            assistant_text = agent_msg or run.get("error_message") or "failed"

        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": assistant_text[:4000]})
        history = history[-12:]

        results.append(
            {
                "turn": i,
                "message": message,
                "skill_id": skill,
                "state": state,
                "run_id": run_id,
                "elapsed_s": round(elapsed, 1),
                "preview": assistant_text[:500],
            }
        )

    print("\n\n### SUMMARY ###")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
