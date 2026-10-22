"""Simulate user chat deploy flow: POST /chat + approval follow-up, poll to terminal state."""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
API_KEY = "dev-local-key"


def api(method: str, path: str, body: dict | None = None) -> dict:
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def wait_run(run_id: str, timeout_s: float = 1200) -> dict:
    terminal = {"succeeded", "failed", "cancelled"}
    deadline = time.time() + timeout_s
    last_state = None
    while time.time() < deadline:
        run = api("GET", f"/runs/{run_id}")
        state = run.get("state")
        if state != last_state:
            print(f"[{run_id[:8]}] state={state}")
            last_state = state
        if state in terminal:
            return run
        time.sleep(4)
    raise TimeoutError(f"run {run_id} timed out (last={last_state})")


def summarize(run: dict) -> None:
    print("state:", run.get("state"), "| skill:", run.get("skill_id"))
    if run.get("error_message"):
        print("error_message:", run.get("error_message"))
    if run.get("site_url"):
        print("site_url:", run.get("site_url"))
    text = run.get("result_summary") or run.get("agent_message") or ""
    if text:
        print("reply:", text[:600].replace("\n", " "))
    for step in run.get("steps") or []:
        st = step.get("status")
        name = step.get("name")
        if st and st != "pending":
            print(f"  step: {name} ({st})")


def main() -> int:
    repo = "Alirezaanorouzi/deployer"
    app_name = "deploychattest3"  # may already exist; agent should still deploy
    msg1 = f"Deploy GitHub repo {repo} to Liara team app {app_name} (Python)."
    print("USER:", msg1)
    run1 = api("POST", "/chat", {"message": msg1, "history": []})
    rid1 = run1["id"]
    print("run:", rid1)
    run1 = wait_run(rid1)
    summarize(run1)

    assistant = (run1.get("agent_message") or run1.get("result_summary") or "")[:3500]
    history = [{"role": "user", "content": msg1}]
    if assistant:
        history.append({"role": "assistant", "content": assistant})

    need_approve = run1.get("state") == "succeeded" and not run1.get("site_url")
    if run1.get("state") == "failed" and "CONFIRMATION" in (run1.get("error_message") or "").upper():
        need_approve = True

    if not need_approve and run1.get("state") == "succeeded" and run1.get("site_url"):
        print("\nOK: deploy completed with URL")
        return 0
    if run1.get("state") == "failed" and not need_approve:
        return 1

    # Approval without the word "deploy" so planner sets confirm_write
    msg2 = "Yes, I confirm — go ahead with create and publish."
    print("\nUSER:", msg2)
    history.append({"role": "user", "content": msg2})
    run2 = api("POST", "/chat", {"message": msg2, "history": history})
    rid2 = run2["id"]
    print("run:", rid2)
    run2 = wait_run(rid2, timeout_s=1500)
    summarize(run2)
    if run2.get("state") == "succeeded" and run2.get("site_url"):
        print("\nOK: full flow succeeded")
        return 0
    return 0 if run2.get("state") == "succeeded" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:500], file=sys.stderr)
        raise SystemExit(2)
