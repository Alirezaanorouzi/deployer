import asyncio
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from queue import Empty, Queue

from app.liara.redact import redact
from app.sandbox.docker_runner import host_path_to_container, run_in_sandbox

OnLine = Callable[[str], Awaitable[None] | None]

_HEARTBEAT_SEC = 40.0
_DEPLOY_TAIL_LINES = 28
_DEPLOY_TIMEOUT_SEC = 1800.0  # pip on Liara mirror can take 10+ minutes on first build


class _DeployBuildProgress:
    """Track Liara Docker/pip build so heartbeats explain long silent periods."""

    def __init__(self) -> None:
        self.in_build = False
        self.in_pip = False
        self.last_hint = ""

    def observe(self, line: str) -> None:
        low = (line or "").lower()
        if "- building" in low or "preparing the build environment" in low:
            self.in_build = True
        if "installing dependencies using pip" in low:
            self.in_pip = True
            self.in_build = True
            self.last_hint = "pip install on Liara (first build often 3–10 min)"
        if low.startswith("collecting "):
            pkg = line.strip().split("(")[0].replace("Collecting ", "").strip()
            self.last_hint = f"pip resolving {pkg}"
        if "downloading " in low:
            self.last_hint = line.strip()[:140]
        if "installing collected packages" in low:
            self.last_hint = "pip installing wheels"
        if "deployment finished successfully" in low:
            self.in_pip = False
            self.in_build = False

    def heartbeat_message(self) -> str:
        if self.in_pip:
            hint = self.last_hint or "pip install"
            return (
                f"— Liara build: still installing Python packages ({hint}). "
                "Large wheels (e.g. SQLAlchemy ~5MB) can take several minutes — not frozen —"
            )
        if self.in_build:
            return "— Liara build still running (Docker/build step) —"
        return "— Liara CLI still running…"


class _DeployTailTracker:
    """Liara CLI tails logs after deploy; stop reading stdout once deploy succeeded + short tail."""

    def __init__(self) -> None:
        self._finished = False
        self._lines_after = 0

    @property
    def succeeded(self) -> bool:
        return self._finished

    def observe(self, line: str) -> bool:
        """Return True when we should stop reading stdout (do not kill the CLI process)."""
        low = (line or "").lower()
        if "deployment finished successfully" in low or "open up the url below" in low:
            self._finished = True
        if self._finished:
            self._lines_after += 1
            return self._lines_after > _DEPLOY_TAIL_LINES
        return False


def _deploy_stdout_success(stdout: str) -> bool:
    low = (stdout or "").lower()
    return (
        "deployment finished successfully" in low
        or "open up the url below" in low
        or "deployed successfully" in low
    )


@dataclass
class CliResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _finalize_cli_result(
    code: int,
    stdout: str,
    stderr: str,
    *,
    deploy_tail: _DeployTailTracker | None,
) -> CliResult:
    stdout = redact(stdout)
    if deploy_tail and deploy_tail.succeeded:
        return CliResult(0, stdout, "")
    if code != 0 and _deploy_stdout_success(stdout):
        return CliResult(0, stdout, "")
    if code != 0 and "timeout" in stdout.lower():
        return CliResult(code, stdout, "BUILD_TIMEOUT: " + stdout[-500:])
    return CliResult(code, stdout, "" if code == 0 else (stderr or stdout[-2000:]))


class LiaraCli:
    def __init__(self, api_token: str, team_id: str | None = None) -> None:
        self._token = api_token
        self._team_id = (team_id or "").strip() or None

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self._token:
            env["LIARA_API_TOKEN"] = self._token
        return env

    def _local_cmd(self, *args: str) -> list[str]:
        exe = shutil.which("liara") or shutil.which("npx")
        if not exe:
            return []
        if exe.endswith("npx") or "npx" in exe.lower():
            return [exe, "@liara/cli", *args]
        return [exe, *args]

    async def _emit(self, on_line: OnLine | None, line: str) -> None:
        if not on_line:
            return
        result = on_line(redact(line.rstrip("\n")))
        if asyncio.iscoroutine(result):
            await result

    def _argv(self, *args: str) -> tuple[str, ...]:
        """Liara expects `liara deploy ... --team-id ID`, not `liara --team-id ID deploy`."""
        if not self._team_id or not args:
            return args
        subcommand, *rest = args
        return (subcommand, *rest, "--team-id", self._team_id)

    async def run(
        self,
        *args: str,
        cwd: str | None = None,
        timeout: float = 900,
        on_line: OnLine | None = None,
        sandbox_run_id: str | None = None,
    ) -> CliResult:
        args = self._argv(*args)
        if args and args[0] == "deploy":
            timeout = max(timeout, _DEPLOY_TIMEOUT_SEC)
        if sandbox_run_id:
            workdir = host_path_to_container(sandbox_run_id, cwd or ".")
            argv = ["liara", *args]
            res = await run_in_sandbox(
                sandbox_run_id,
                argv,
                workdir_in_container=workdir,
                env=self._env(),
                on_line=on_line,
                timeout=timeout,
            )
            stderr = "" if res.ok else res.output[-2000:]
            return CliResult(res.exit_code, res.output, stderr)

        cmd = self._local_cmd(*args)
        if not cmd:
            return CliResult(127, "", "liara CLI not found in PATH")

        await self._emit(on_line, "$ " + " ".join(cmd[-len(args) :] if args else cmd))

        env = os.environ.copy()
        env.update(self._env())
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("NODE_NO_READLINE", "1")
        is_deploy = bool(args and args[0] == "deploy")
        deploy_tail = _DeployTailTracker() if is_deploy else None
        build_progress = _DeployBuildProgress() if is_deploy else None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except NotImplementedError:
            return await self._run_host_sync_subprocess(
                cmd, cwd=cwd, env=env, on_line=on_line, timeout=timeout
            )

        stdout_parts: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            if proc.stdout:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    wait = min(_HEARTBEAT_SEC, remaining)
                    try:
                        line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=wait)
                    except asyncio.TimeoutError:
                        msg = (
                            build_progress.heartbeat_message()
                            if build_progress
                            else "— Liara CLI still running…"
                        )
                        await self._emit(on_line, msg)
                        continue
                    if not line_b:
                        break
                    line = line_b.decode(errors="replace")
                    stdout_parts.append(line)
                    if build_progress:
                        build_progress.observe(line)
                    await self._emit(on_line, line.rstrip("\n"))
                    if deploy_tail and deploy_tail.observe(line):
                        break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=min(remaining, 45.0))
                except asyncio.TimeoutError:
                    if deploy_tail and deploy_tail.succeeded:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=10.0)
                    else:
                        raise
            else:
                raise asyncio.TimeoutError
        except asyncio.TimeoutError:
            proc.kill()
            return CliResult(124, "".join(stdout_parts), "BUILD_TIMEOUT: command exceeded timeout")

        code = proc.returncode or 0
        return _finalize_cli_result(
            code,
            "".join(stdout_parts),
            "",
            deploy_tail=deploy_tail,
        )

    async def _run_host_sync_subprocess(
        self,
        cmd: list[str],
        *,
        cwd: str | None,
        env: dict[str, str],
        on_line: OnLine | None,
        timeout: float,
    ) -> CliResult:
        """Windows + some uvicorn loops: stream host CLI via thread + queue."""

        line_q: Queue[str | None] = Queue()
        proc_box: list[subprocess.Popen[str]] = []
        read_err: list[BaseException] = []

        is_deploy = bool(cmd and "deploy" in cmd)
        deploy_tail = _DeployTailTracker() if is_deploy else None
        build_progress = _DeployBuildProgress() if is_deploy else None

        def _reader() -> None:
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                proc_box.append(proc)
                assert proc.stdout is not None
                for line in proc.stdout:
                    line_q.put(line)
                    if deploy_tail and deploy_tail.observe(line):
                        break
            except BaseException as e:
                read_err.append(e)
            finally:
                line_q.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        loop = asyncio.get_running_loop()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait = min(_HEARTBEAT_SEC, remaining)
            try:
                line = await loop.run_in_executor(
                    None,
                    lambda w=wait: line_q.get(timeout=w),
                )
            except Empty:
                msg = (
                    build_progress.heartbeat_message()
                    if build_progress
                    else "— Liara CLI still running…"
                )
                await self._emit(on_line, msg)
                continue
            if line is None:
                break
            chunks.append(line)
            if build_progress:
                build_progress.observe(line)
            await self._emit(on_line, line.rstrip("\n"))

        proc = proc_box[0] if proc_box else None
        timed_out = time.monotonic() >= deadline
        if proc and proc.poll() is None:
            if timed_out:
                proc.kill()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    timed_out = True
                else:
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(None, proc.wait),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        timed_out = True

        if read_err and not chunks:
            return CliResult(127, "", f"CLI subprocess error: {read_err[0]}")

        if timed_out:
            stdout = redact("".join(chunks) + "\nBUILD_TIMEOUT")
            return CliResult(124, stdout, "BUILD_TIMEOUT: command exceeded timeout")
        code = int(proc.returncode or 0) if proc else 127
        return _finalize_cli_result(
            code,
            "".join(chunks),
            "",
            deploy_tail=deploy_tail,
        )
