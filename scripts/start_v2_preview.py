"""Start a loopback-only, local-provider v2 preview with an isolated database."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "v2-preview"
PORT = 18849


def main() -> int:
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"端口 {PORT} 已被占用，请先检查已有进程。", file=sys.stderr)
            return 1
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    app_data, journal = DATA / "app-data", DATA / "deletion-journal"
    app_data.mkdir(mode=0o700, exist_ok=True)
    journal.mkdir(mode=0o700, exist_ok=True)
    database = app_data / "preview.sqlite"
    if database.exists() and not (journal / "deletions.jsonl").exists():
        print("预览数据库已有数据但删除账本缺失，已停止启动。", file=sys.stderr)
        return 1
    env = {key: value for key, value in os.environ.items() if not key.startswith("COACH_")}
    env.update(COACH_PROVIDER="local", COACH_V2_ENABLED="1", COACH_DATA_DIR=str(app_data),
               COACH_JOURNAL_DIR=str(journal), COACH_DATABASE_URL="sqlite:///" + str(database))
    migration = subprocess.run([sys.executable, "-m", "scripts.container_entrypoint", "migrate"], cwd=ROOT, env=env)
    if migration.returncode:
        print("预览初始化失败，未启动服务。", file=sys.stderr)
        return 1
    log_path = DATA / "preview.log"
    processes = []
    with log_path.open("a", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        for command in ([sys.executable, "-m", "app.worker"],
                        [sys.executable, "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", str(PORT), "--no-proxy-headers"]):
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                              stdout=log, stderr=log, start_new_session=True))
    for _ in range(30):
        if any(process.poll() is not None for process in processes):
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1) as response:
                if response.status == 200:
                    print(f"已启动本地演示：http://127.0.0.1:{PORT}/v2")
                    print("使用独立预览数据库和本地替身，不调用真实模型。")
                    return 0
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    for process in processes:
        if process.poll() is None:
            process.terminate()
    print("预览启动失败，请检查 data/v2-preview/preview.log。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
