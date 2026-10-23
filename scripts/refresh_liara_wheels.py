"""Download manylinux cp312 wheels for Liara offline pip install."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / "wheels"
BASE = ROOT / "requirements-base.txt"


def main() -> int:
    WHEELS.mkdir(exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-r",
        str(BASE),
        "-d",
        str(WHEELS),
        "--platform",
        "manylinux_2_17_x86_64",
        "--python-version",
        "312",
        "--implementation",
        "cp",
        "--abi",
        "cp312",
        "--only-binary=:all:",
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
