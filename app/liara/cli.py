import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.liara.redact import redact
from app.sandbox.docker_runner import host_path_to_container, run_in_sandbox

OnLine = Callable[[str], Awaitable[None] | None]


@dataclass
class CliResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


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

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout_parts: list[str] = []
        try:
            if proc.stdout:
                while True:
                    line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                    if not line_b:
                        break
                    line = line_b.decode(errors="replace")
                    stdout_parts.append(line)
                    await self._emit(on_line, line.rstrip("\n"))
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return CliResult(124, "".join(stdout_parts), "BUILD_TIMEOUT: command exceeded timeout")

        stdout = redact("".join(stdout_parts))
        code = proc.returncode or 0
        if code != 0 and "timeout" in stdout.lower():
            return CliResult(code, stdout, "BUILD_TIMEOUT: " + stdout[-500:])
        return CliResult(code, stdout, "" if code == 0 else stdout[-2000:])
