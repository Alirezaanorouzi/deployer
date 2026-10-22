"""Writable paths for local dev vs Liara (read-only app root)."""

import os
from pathlib import Path


def data_root() -> Path:
    """Directory for SQLite, workspaces, etc. Falls back to /tmp on read-only roots."""
    env = (os.environ.get("DATA_DIR") or "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("data"))
    candidates.append(Path("/tmp/deployer-data"))
    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return root.resolve()
        except OSError:
            continue
    raise OSError("No writable DATA_DIR (tried DATA_DIR, ./data, /tmp/deployer-data)")


def default_sqlite_url() -> str:
    db = data_root() / "deployer.db"
    return f"sqlite:///{db.as_posix()}"
