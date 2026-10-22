import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.liara.redact import redact
from app.repo.paths import workspace_root

OnLine = Callable[[str], Awaitable[None] | None]


@dataclass
class SandboxResult:
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


async def docker_available() -> bool:
    if not shutil.which("docker"):
        return False

    def _docker_info_ok() -> bool:
        try:
            r = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    return await asyncio.to_thread(_docker_info_ok)


def _use_docker() -> bool:
    settings = get_settings()
    if not settings.docker_sandbox:
        return False
    return True


def host_path_to_container(run_id: str, host_path: str | Path) -> str:
    ws = workspace_root(run_id).resolve()
    p = Path(host_path).resolve()
    try:
        rel = p.relative_to(ws)
        return "/workspace/" + rel.as_posix()
    except ValueError:
        return "/workspace"


def _host_workspace_mount(run_id: str) -> tuple[str, str]:
    """Return host path and in-container path /workspace."""
    host = workspace_root(run_id).resolve()
    host.mkdir(parents=True, exist_ok=True)
    return str(host), "/workspace"


async def _emit(on_line: OnLine | None, line: str) -> None:
    if not on_line:
        return
    result = on_line(redact(line))
    if asyncio.iscoroutine(result):
        await result


async def run_in_sandbox(
    run_id: str,
    argv: list[str],
    *,
    workdir_in_container: str = "/workspace",
    env: dict[str, str] | None = None,
    on_line: OnLine | None = None,
    timeout: float = 900,
) -> SandboxResult:
    """
    Run a command in the per-run workspace.
    Uses Docker when docker_sandbox=true and Docker is available; else host shell.
    """
    settings = get_settings()
    env = env or {}
    display_cmd = " ".join(argv)
    await _emit(on_line, f"$ {display_cmd}")

    use_docker = _use_docker() and await docker_available()
    if use_docker:
        host_ws, container_ws = _host_workspace_mount(run_id)
        if not workdir_in_container.startswith(container_ws):
            workdir_in_container = container_ws

        docker_argv = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{host_ws}:{container_ws}",
            "-w",
            workdir_in_container,
        ]
        for key, val in env.items():
            if val:
                docker_argv.extend(["-e", f"{key}={val}"])
        docker_argv.append(settings.docker_executor_image)
        docker_argv.extend(argv)
        await _emit(on_line, f"[docker] image={settings.docker_executor_image}")
        proc_argv = docker_argv
        cwd = None
    else:
        if not _use_docker():
            await _emit(on_line, "[host] docker sandbox disabled — running on host")
        else:
            await _emit(on_line, "[host] Docker unavailable — running on host")
        host_ws, _ = _host_workspace_mount(run_id)
        proc_argv = argv
        cwd = str(Path(host_ws))
        if workdir_in_container != "/workspace":
            rel = workdir_in_container.removeprefix("/workspace").lstrip("/")
            cwd = str(Path(host_ws) / rel) if rel else cwd

    try:
        proc = await asyncio.create_subprocess_exec(
            *proc_argv,
            cwd=cwd,
            env=None if use_docker else _merge_host_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except NotImplementedError:
        return SandboxResult(
            127,
            "Subprocess not supported in this asyncio event loop (Windows/uvicorn). "
            "Use GitHub zipball or host git instead.",
        )
    parts: list[str] = []
    try:
        if proc.stdout:
            while True:
                line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not line_b:
                    break
                line = line_b.decode(errors="replace")
                parts.append(line)
                await _emit(on_line, line.rstrip("\n"))
        exit_code = await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return SandboxResult(124, "".join(parts) + "\nBUILD_TIMEOUT")

    return SandboxResult(proc.returncode if proc.returncode is not None else exit_code, redact("".join(parts)))


def _merge_host_env(extra: dict[str, str]) -> dict[str, str]:
    import os

    env = os.environ.copy()
    for k, v in extra.items():
        if v:
            env[k] = v
    return env


async def run_shell_script(
    run_id: str,
    script: str,
    *,
    workdir_in_container: str = "/workspace",
    env: dict[str, str] | None = None,
    on_line: OnLine | None = None,
    timeout: float = 900,
) -> SandboxResult:
    """Run bash -lc script inside sandbox (GitOps helper)."""
    return await run_in_sandbox(
        run_id,
        ["bash", "-lc", script],
        workdir_in_container=workdir_in_container,
        env=env,
        on_line=on_line,
        timeout=timeout,
    )
