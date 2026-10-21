"""Exercise maintenance failure paths through a fake CLI, without Docker access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


FAKE_DOCKER = r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with open(os.environ["COACH_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps(arguments) + "\n")
if arguments == ["compose", "version"]:
    raise SystemExit(0)
if "ps" in arguments:
    print(os.environ.get("COACH_TEST_RUNNING", ""))
if "exec" in arguments:
    sys.stdout.write("fictional database dump")
if "scripts.archive_tools" in arguments and "pack" in arguments:
    if os.environ.get("COACH_TEST_FAIL_PHASE") != "pack":
        mount = arguments[arguments.index("-v") + 1].rsplit(":", 1)[0]
        (Path(mount) / "backup.tar.gz").write_bytes(b"fictional archive")
if "scripts.archive_tools" in arguments and "verify" in arguments:
    if os.environ.get("COACH_TEST_FAIL_PHASE") != "verify":
        mount = arguments[arguments.index("-v") + 1].rsplit(":", 1)[0]
        verified = Path(mount) / "verified"
        verified.mkdir()
        (verified / "database.dump").write_bytes(b"fictional database dump")
if os.environ.get("COACH_TEST_FAIL_PHASE") in arguments:
    raise SystemExit(42)
'''


def maintenance(tmp_path, monkeypatch, command, fail_phase="", running=""):
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    script = checkout / "scripts/coach.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/coach.sh", script)
    executable = tmp_path / "bin/docker"
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    executable.chmod(0o700)
    calls = tmp_path / "docker-calls.jsonl"
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("COACH_TEST_CALLS", str(calls))
    monkeypatch.setenv("COACH_TEST_FAIL_PHASE", fail_phase)
    monkeypatch.setenv("COACH_TEST_RUNNING", running)
    source = checkout / "fictional-backup.tar.gz"
    source.write_bytes(b"fictional archive")
    path = checkout / "result.tar.gz" if command == "backup" else source
    result = subprocess.run(["bash", str(script), command, str(path)], cwd=checkout, capture_output=True, text=True)
    inventory = [json.loads(line) for line in calls.read_text().splitlines()]
    return result, inventory, checkout


@pytest.mark.parametrize("command,fail_phase", [("restore", "verify"), ("restore", "pg_restore"), ("restore", "migrate"), ("restore", "restore-data"), ("journal-restore", "import")])
def test_restore_failure_never_reopens_writers(tmp_path, monkeypatch, command, fail_phase):
    # pg_restore is part of a quoted container command; the fake needs to match it.
    if fail_phase == "pg_restore":
        fail_phase = 'exec pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --clean --if-exists --no-owner --exit-on-error'
    result, calls, checkout = maintenance(tmp_path, monkeypatch, command, fail_phase)
    assert result.returncode == 42
    assert any("stop" in call and "api" in call and "worker" in call for call in calls)
    assert not any(("up" in call or "start" in call) and ("api" in call or "worker" in call) for call in calls)
    assert not (checkout / ".coach-maintenance.lock").exists()


@pytest.mark.parametrize("fail_phase", ["", "pack"])
@pytest.mark.parametrize("running", ["", "worker", "api\nworker"])
def test_backup_preserves_which_writers_were_running(tmp_path, monkeypatch, fail_phase, running):
    result, calls, checkout = maintenance(tmp_path, monkeypatch, "backup", fail_phase, running)
    assert result.returncode == (42 if fail_phase else 0)
    started = [call[call.index("start") + 1:] for call in calls if "start" in call]
    assert started == ([running.splitlines()] if running else [])
    assert not any("up" in call and ("api" in call or "worker" in call) for call in calls)
    assert not (checkout / ".coach-maintenance.lock").exists()
