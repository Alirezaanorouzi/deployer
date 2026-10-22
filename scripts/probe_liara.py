"""Dev script: python scripts/probe_liara.py"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.liara.adapter import LiaraAdapter


async def main() -> None:
    a = LiaraAdapter()
    for name, coro in [
        ("auth", a.auth_check()),
        ("projects", a.list_projects()),
    ]:
        r = await coro
        print(name, r.ok, r.code)
        if r.data:
            print(json.dumps(r.data, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    asyncio.run(main())
