"""Cursor SDK bridge + client pinned to a single worker thread (Windows-safe)."""

from __future__ import annotations

import asyncio
import codecs
import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cursor_sdk import Client


def _patch_windows_bridge_discovery() -> None:
    """cursor-sdk uses selectors on stderr pipes; that breaks on Windows (WinError 10038)."""
    if sys.platform != "win32":
        return

    from cursor_sdk._bridge import parse_discovery_line
    from cursor_sdk.errors import CursorSDKError

    def _read_discovery(
        process: subprocess.Popen[str], timeout: float
    ) -> Mapping[str, Any]:
        if process.stderr is None:
            raise CursorSDKError("Bridge process stderr is unavailable")
        stderr_fd = process.stderr.fileno()
        was_blocking = os.get_blocking(stderr_fd)
        os.set_blocking(stderr_fd, False)
        try:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            deadline = time.monotonic() + timeout
            stderr_lines: list[str] = []
            pending = ""

            def drain_available() -> Mapping[str, Any] | None:
                nonlocal pending
                while True:
                    try:
                        chunk = os.read(stderr_fd, 8192)
                    except BlockingIOError:
                        return None
                    if not chunk:
                        final_text = decoder.decode(b"", final=True)
                        if final_text:
                            pending += final_text
                        if pending:
                            line = pending
                            pending = ""
                            stderr_lines.append(line)
                            return parse_discovery_line(line)
                        return None
                    pending += decoder.decode(chunk)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        line += "\n"
                        stderr_lines.append(line)
                        discovery = parse_discovery_line(line)
                        if discovery is not None:
                            return discovery

            while time.monotonic() < deadline:
                discovery = drain_available()
                if discovery is not None:
                    return discovery
                exit_code = process.poll()
                if exit_code is not None:
                    discovery = drain_available()
                    if discovery is not None:
                        return discovery
                    raise CursorSDKError(
                        f"Bridge exited before discovery with status {exit_code}: "
                        + "".join(stderr_lines)
                        + pending
                    )
                time.sleep(0.05)
            raise CursorSDKError("Timed out waiting for bridge discovery")
        finally:
            os.set_blocking(stderr_fd, was_blocking)

    import cursor_sdk._bridge as bridge_mod

    bridge_mod._read_discovery = _read_discovery


_patch_windows_bridge_discovery()

_sdk_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="cursor-sdk",
)
_state_lock = threading.Lock()
_clients: dict[str, Client] = {}
_bridges: dict[str, object] = {}


def _bridge_http_client():
    """Local bridge must not use system HTTP_PROXY (often breaks 127.0.0.1 on Windows)."""
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(120.0),
        follow_redirects=True,
        trust_env=False,
    )


def _client_for_cwd_on_worker(cache_key: str, launch_workspace: str) -> Client:
    from cursor_sdk import Bridge, Client

    with _state_lock:
        existing = _clients.get(cache_key)
        if existing is not None:
            return existing

    bridge = Bridge.launch(workspace=launch_workspace)
    client = Client(
        bridge.endpoint,
        allow_api_key_env_fallback=True,
        http_client=_bridge_http_client(),
        unary_timeout=120.0,
        stream_timeout=900.0,
    )
    with _state_lock:
        _bridges[cache_key] = bridge
        _clients[cache_key] = client
    return client


def _on_sdk_worker_thread() -> bool:
    return threading.current_thread().name.startswith("cursor-sdk")


def reset_bridge_client(cache_key: str) -> None:
    """Drop cached bridge/client after connection loss (caller may launch a fresh one)."""
    with _state_lock:
        _bridges.pop(cache_key, None)
        _clients.pop(cache_key, None)


def client_for_cwd(cache_key: str, *, launch_workspace: str | None = None) -> Client:
    """Return a bridge-backed Client; `cache_key` dedupes, `launch_workspace` is bridge cwd."""
    launch = launch_workspace or cache_key
    if _on_sdk_worker_thread():
        return _client_for_cwd_on_worker(cache_key, launch)
    fut = _sdk_executor.submit(_client_for_cwd_on_worker, cache_key, launch)
    return fut.result()


async def run_on_sdk_thread(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _sdk_executor,
        lambda: fn(*args, **kwargs),
    )
