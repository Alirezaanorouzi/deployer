# Vendored wheels for Liara

Linux (`manylinux`, CPython 3.12) wheels so Liara `pip install -r requirements.txt` does **not** download from the platform PyPI mirror.

Refresh after changing `requirements-base.txt`:

```bash
.venv\Scripts\python scripts/refresh_liara_wheels.py
```
