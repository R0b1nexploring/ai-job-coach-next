"""Container roles; fail clearly without printing configuration or credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    role = sys.argv[1] if len(sys.argv) == 2 else ""
    if role == "check-api":
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3) as response:
                return 0 if response.status == 200 else 1
        except (OSError, urllib.error.URLError):
            return 1
    if role not in {"api", "worker", "migrate"}:
        print("容器命令必须是 api、worker 或 migrate", file=sys.stderr)
        return 1
    from app.runtime import ConfigurationError, validate_configuration
    try:
        configuration = validate_configuration()
    except ConfigurationError as error:
        print(f"启动配置错误：{error}", file=sys.stderr)
        return 1
    print(json.dumps({"role": role, **configuration}, ensure_ascii=False), flush=True)
    if role == "migrate":
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT)
        if result.returncode:
            return result.returncode
        from app.db import SessionLocal
        from app.privacy import initialize_journal, replay_deletions
        initialize_journal(SessionLocal, bootstrap=True)
        replay_deletions(SessionLocal)
        return 0
    arguments = [sys.executable, "-m", "app.worker"] if role == "worker" else [
        sys.executable, "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers",
    ]
    os.execv(sys.executable, arguments)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
