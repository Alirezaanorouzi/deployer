# Liara deploy playbook (reference for agent + humans)

## Ordered steps

1. **Clone** — `repo.clone_github` (GitHub → `data/workspaces/<run_id>/repo`).
2. **Prepare** — `repo.prepare_deploy_workspace` (detect stack, write `liara.json`, set `deploy_path` + `port`).
3. **Create app** — `liara.create_app` (platform service; app is **ready**, not **running**).
4. **Deploy** — `liara.deploy_service` (console **Deploy / استقرار**; upload + build + start).
5. **URL** — `liara.get_app_url` → `https://<app-id>.liara.run`.
6. **Health** — `liara.verify_app_health` (tries `/` and `/health`).
7. **Diagnose** — `liara.diagnose_deploy_health` (auto after failed deploy or unhealthy URL; logs + fix hints).

Agent `liara.deploy` runs steps 1–7 in order; `liara.deploy_service` on failure and `verify_app_health` on 502/503 trigger diagnose automatically.

## Liara Python gotchas (502)

- App root on Liara is **read-only**; do not `mkdir data` in project root.
- Use **`DATA_DIR`** (e.g. `/tmp/deployer-data`) or rely on app fallback in `app/paths.py`.
- Start command: **`python main.py`** (reads `PORT` from Liara).
- Set platform env vars for production: `WORKSPACE_API_KEY`, `LLM_*`, `LIARA_API_TOKEN`, optional `DATABASE_URL`.

## CLI equivalents

```bash
liara app:create --name myapp --platform python --team-id <TEAM>
liara deploy --app myapp --port 8000 --path . --team-id <TEAM>
curl -sI https://myapp.liara.run
liara logs -a myapp --team-id <TEAM>
```
