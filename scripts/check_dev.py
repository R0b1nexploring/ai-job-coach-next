"""Check the local development baseline without using existing application data."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_key": re.compile(r"\bsk-(?:proj-|ant-api03-)?[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def run(label: str, command: list[str], env: dict[str, str]) -> None:
    print(f"[check] {label}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check_repository() -> None:
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    )
    paths = sorted(set(listing.stdout.decode().split("\0")) - {""})
    failures: list[str] = []
    for relative in paths:
        path = ROOT / relative
        if not path.is_file():
            continue
        if path.name.startswith(".env") and path.name not in {".env.example", ".env.production.example", ".env.docker.example"}:
            failures.append(f"{relative}: private environment file")
            continue
        if path.suffix in {".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx"}:
            failures.append(f"{relative}: private data or key file")
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(source.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    failures.append(f"{relative}:{number}: {label}")
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^)]+)\)", source):
                target = target.split("#", 1)[0]
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (path.parent / target).exists():
                    failures.append(f"{relative}: missing link {target}")
    if failures:
        # Report only locations and categories, never secret matches or materials.
        raise RuntimeError("\n".join(failures))
    print(f"[check] repository boundaries, known secret patterns and Markdown links: {len(paths)} files", flush=True)


def main() -> int:
    env = dict(os.environ)
    env.update({"COACH_DATABASE_URL": "sqlite:///:memory:", "COACH_PROVIDER": "local", "COACH_API_KEY": "", "COACH_API_BASE_URL": "", "COACH_MODEL": "", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        check_repository()
        run("dependency lock", ["uv", "lock", "--check"], env)
        run("offline tests", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], env)
        run("browser JavaScript syntax", ["node", "--check", "app/static/app.js"], env)
        run("v2 browser JavaScript syntax", ["node", "--check", "app/static/v2.js"], env)
        run("empty and historical temporary SQLite migrations", [sys.executable, "scripts/check_migrations.py"], env)
    except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
        print(f"[failed] {error}", file=sys.stderr)
        return 1
    print("[passed] local offline development baseline; production and model quality require separate evidence", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
