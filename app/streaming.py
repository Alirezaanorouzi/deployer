import asyncio
import json
from collections import defaultdict
from typing import Any


class RunStreamBus:
    """In-process SSE event bus per run_id."""

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._queues[run_id].append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        if run_id in self._queues:
            self._queues[run_id] = [x for x in self._queues[run_id] if x is not q]
            if not self._queues[run_id]:
                del self._queues[run_id]

    async def publish(self, run_id: str, event: str, data: dict[str, Any] | None = None) -> None:
        from app.terminal_log import append, line_from_stream_event

        payload = {"event": event, "data": data or {}}
        tline = line_from_stream_event(event, data or {})
        if tline:
            append(run_id, tline)
        for q in list(self._queues.get(run_id, [])):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def log(self, run_id: str, line: str) -> None:
        await self.publish(run_id, "log_line", {"line": line.rstrip("\n")})


stream_bus = RunStreamBus()


def sse_format(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
