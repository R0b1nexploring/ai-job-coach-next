from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import api
from app.db import Base, make_engine
from app.models import Batch, GuideRevision, ResumeRevision, TaskExecution
from app.privacy import JournalUnavailable, delete_batch, deleted_ids, journal_path, replay_deletions, initialize_journal, installation_id
from app.providers import LocalProvider
from app.service import create_batch, get_batch, process_batch_once, process_guide_once

JD = "虚构机械岗位：负责装配结构设计、图纸输出与方案验证。"


@pytest.fixture
def personal(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'personal.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    yield sessions, TestClient(api.app), engine
    engine.dispose()


def ready(sessions, key="fictional-personal"):
    batch = create_batch(sessions, jd=JD, material="虚构课程项目：整理装配图及零部件清单。", confirmed=False, idempotency_key=key)
    process_batch_once(sessions, LocalProvider())
    process_guide_once(sessions, LocalProvider())
    process_guide_once(sessions, LocalProvider())
    return batch.id


def test_cross_origin_cannot_read_or_mutate_personal_data(personal):
    _, client, _ = personal
    for method, path in (("GET", "/api/batches"), ("POST", "/api/materials/extract"), ("DELETE", "/api/batches/unknown")):
        assert client.request(method, path, headers={"Origin": "https://example.invalid"}).status_code == 403
    assert client.get("/api/batches", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/batches", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/batches", headers={"Host": "rebinding.invalid"}).status_code == 400


def test_history_download_and_export_remain_bound_to_original_revision(personal):
    sessions, client, _ = personal
    identifier = ready(sessions)
    batch = get_batch(sessions, identifier)
    resume = batch["resumes"]["job"]
    original = resume["items"][0]["text"]
    resume["items"][0]["text"] = "本人在虚构新方案中检查连接方法并整理新的成果记录。"
    saved = client.put(f"/api/batches/{identifier}/resumes/job", json={"revision": 1, "title": resume["title"], "summary": resume["summary"], "items": resume["items"]})
    assert saved.status_code == 200
    assert client.get(f"/api/batches/{identifier}/guides/job/download").status_code == 409
    old_resume = client.get(f"/api/batches/{identifier}/resumes/job/download/html?revision=1")
    assert original in old_resume.text and resume["items"][0]["text"] not in old_resume.text
    old_guide = client.get(f"/api/batches/{identifier}/guides/job/revisions/1/download")
    assert old_guide.status_code == 200 and original in old_guide.text
    archive = client.get(f"/api/batches/{identifier}/archive")
    assert archive.status_code == 200
    assert len(archive.json()["resume_history"]["job"]) == 2
    assert archive.json()["batch"]["resumes"]["blueprint"]["revision"] == 1


def test_deletion_cascades_and_replays_after_restoring_old_business_database(personal, tmp_path):
    sessions, client, engine = personal
    identifier = ready(sessions)
    engine.dispose()
    old = tmp_path / "old-business.sqlite"
    shutil.copyfile(Path(engine.url.database), old)
    assert client.delete(f"/api/batches/{identifier}").status_code == 204
    assert identifier in deleted_ids()
    assert client.get(f"/api/batches/{identifier}").status_code == 404
    assert client.get("/api/batches").json() == []
    for model in (Batch, GuideRevision, ResumeRevision, TaskExecution):
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(model)) == 0
    restored_engine = make_engine(f"sqlite:///{old}")
    restored = sessionmaker(bind=restored_engine, expire_on_commit=False)
    assert replay_deletions(restored) == 1
    with restored() as session:
        assert session.scalar(select(func.count()).select_from(Batch)) == 0
    restored_engine.dispose()


def test_late_worker_cannot_recreate_deleted_batch(personal):
    sessions, _, _ = personal
    batch = create_batch(sessions, jd=JD, material="", confirmed=False, idempotency_key="fictional-delete-running")
    class DeleteDuringCall(LocalProvider):
        def bundle(self, jd, material):
            delete_batch(sessions, batch.id)
            return super().bundle(jd, material)
    assert process_batch_once(sessions, DeleteDuringCall())
    with sessions() as session:
        assert session.get(Batch, batch.id) is None
        assert session.scalar(select(func.count()).select_from(ResumeRevision)) == 0


def test_missing_or_corrupt_journal_blocks_restore_and_delete(personal):
    sessions, client, _ = personal
    identifier = ready(sessions)
    journal_path().unlink()
    with pytest.raises(JournalUnavailable):
        replay_deletions(sessions)
    assert client.delete(f"/api/batches/{identifier}").status_code == 503
    assert get_batch(sessions, identifier)["status"] == "ready"
    journal_path().write_text('{"version": 1, "batch_id": "invalid"}\n')
    assert client.get(f"/api/batches/{identifier}").status_code == 503


def test_normal_startup_cannot_recreate_missing_deletion_state(personal):
    sessions, _, _ = personal
    identifier = ready(sessions)
    delete_batch(sessions, identifier)
    journal_path().unlink()
    with pytest.raises(JournalUnavailable):
        initialize_journal(sessions, bootstrap=True)
    assert not journal_path().exists()


def test_old_business_database_requires_journal_even_if_volumes_are_lost(personal, tmp_path, monkeypatch):
    sessions, _, _ = personal
    ready(sessions)
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "lost-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "lost-data"))
    with pytest.raises(JournalUnavailable):
        initialize_journal(sessions, bootstrap=True)


def test_clean_reinstallation_has_new_backup_identity(tmp_path, monkeypatch):
    old = installation_id()
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "new-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "new-data"))
    initialize_journal(bootstrap=True)
    assert installation_id() != old
