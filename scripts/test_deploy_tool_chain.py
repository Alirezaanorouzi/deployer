"""Verify liara.deploy pipeline calls deploy_service after create_app (no HTTP)."""
import asyncio
import json
import uuid

from app.agent.react_agent import _forced_liara_deploy_action
from app.agent.tools.registry import execute_tool
from app.database import SessionLocal
from app.executor import RunExecutor
from app.liara.adapter import LiaraAdapter
from app.models import Run, RunState


async def main() -> int:
    observations = [
        {"tool": "repo.clone_github", "ok": True, "code": "OK"},
        {"tool": "repo.prepare_deploy_workspace", "ok": True, "code": "OK"},
        {"tool": "liara.create_app", "ok": True, "code": "OK"},
    ]
    planner = {
        "confirm_write": True,
        "repo": "Alirezaanorouzi/deployer",
        "liara_app_name": "deploychattest3",
        "app_name": "deploychattest3",
        "deploy_path": r"data\workspaces\deploychattest3\repo",
        "port": 8000,
        "platform": "python",
    }
    action = _forced_liara_deploy_action(observations, planner)
    assert action and action["tool_id"] == "liara.deploy_service", action
    print("forced next tool:", action["tool_id"], action["tool_params"])

    run_id = str(uuid.uuid4())
    db = SessionLocal()
    run = Run(id=run_id, user_message="test", state=RunState.executing, skill_id="liara.deploy")
    db.add(run)
    db.commit()

    liara = LiaraAdapter()
    params = dict(action["tool_params"])
    params["app_name"] = "deploychattest3"
    params["confirm_write"] = True
    params["deploy_path"] = planner["deploy_path"]

    async def on_log(line: str) -> None:
        print(line[:120])

    print("calling deploy_service (may take several minutes)...")
    result = await execute_tool(
        "liara.deploy_service",
        liara=liara,
        params=params,
        run_id=run_id,
        on_log=on_log,
    )
    print("deploy ok:", result.ok, result.code, (result.text_for_agent or "")[:300])
    db.delete(run)
    db.commit()
    db.close()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
