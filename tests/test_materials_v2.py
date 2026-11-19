import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.content_v2 import text_hash
from app.db import Base, make_engine
from app.materials_v2 import freeze_reused_material
from app.models import Batch, FactSnapshot, MaterialRevision
from app.privacy import JournalUnavailable
from app.privacy_v2 import bind_batch_snapshot, v2_write_transaction


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    from app import materials_api_v2
    monkeypatch.setenv("COACH_V2_ENABLED", "1")
    engine = make_engine(f"sqlite:///{tmp_path / 'fictional.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(materials_api_v2, "_sessions", lambda: sessions)
    app = FastAPI()
    @app.exception_handler(JournalUnavailable)
    async def journal_error(request: Request, error: JournalUnavailable):
        return JSONResponse({"detail":"删除记录不可用"}, status_code=503)
    app.include_router(materials_api_v2.router)
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def create_material(client, text="整理设备记录😀", source_type="manual"):
    response = client.post("/api/v2/materials", json={"extracted_text":text,"source_type":source_type})
    assert response.status_code == 201
    return response.json()


def attach(sessions, revision, text, batch_id="b" * 32, *, confirmed=False):
    candidates = [{"fact_type":"action","value":text,"start":0,"end":len(text),"quote_hash":text_hash(text),"confirmed":confirmed}]
    with v2_write_transaction(sessions, batch_id=batch_id) as db:
        reused = freeze_reused_material(db, revision_id=revision, candidates=candidates)
        db.add(Batch(id=batch_id, idempotency_key=batch_id, input_hash="f" * 64, jd_text="fiction", material_text="", schema_version=2))
        db.flush()
        bind_batch_snapshot(db, batch_id=batch_id, snapshot_id=reused.fact_snapshot_id, material_revision_ids=(reused.material_revision_id,))
        return reused.fact_snapshot_id, dict(reused.claim_ids)


def test_explicit_save_revision_listing_and_immutable_read(app_client):
    client, sessions = app_client
    first = create_material(client)
    material_id = first["material_id"]
    created = client.post(f"/api/v2/materials/{material_id}/revisions", json={"extracted_text":"新虚构素材\r\n第二行", "source_type":"text"})
    assert created.status_code == 201 and created.json()["revision"] == 2
    latest = created.json()
    assert client.get("/api/v2/materials").json()["materials"] == [latest]
    history = client.get(f"/api/v2/materials/{material_id}/revisions").json()["revisions"]
    assert [item["revision"] for item in history] == [2,1]
    assert client.get(f"/api/v2/materials/{material_id}/revisions/{first['revision_id']}").json()["extracted_text"] == "整理设备记录😀"
    assert client.get(f"/api/v2/materials/{material_id}/revisions/{latest['revision_id']}").json()["extracted_text"] == "新虚构素材\n第二行"
    with sessions() as db:
        assert all(row.retention == "independent" and row.owner_batch_id is None for row in db.scalars(select(MaterialRevision)))


def test_batch_owned_material_is_never_visible_as_independent(app_client):
    client, sessions = app_client
    from app.facts_v2 import create_material_revision
    with v2_write_transaction(sessions, batch_id="a" * 32) as db:
        private = create_material_revision(db, scope_id="local", source_type="manual", extracted_text="虚构批次专属")
        mid, rid = private.material_id, private.id
    assert client.get("/api/v2/materials").json()["materials"] == []
    assert client.get(f"/api/v2/materials/{mid}/revisions/{rid}").status_code == 404
    assert client.post(f"/api/v2/materials/{mid}/revisions",json={"extracted_text":"新的内容","source_type":"text"}).status_code == 404


def test_reuse_checks_exact_evidence_and_freezes_old_source(app_client):
    client, sessions = app_client
    text="整理设备记录😀"
    first=create_material(client,text)
    snapshot_id,mapping=attach(sessions,first["revision_id"],text,confirmed=True)
    assert mapping["c1"] != "c1"
    client.post(f"/api/v2/materials/{first['material_id']}/revisions",json={"extracted_text":"新修订","source_type":"manual"})
    with sessions() as db:
        snapshot=db.get(FactSnapshot,snapshot_id)
        assert snapshot.claims[0]["confirmation"] == "user_confirmed"
        assert snapshot.claims[0]["claim_id"] == mapping["c1"]
        assert snapshot.source_copies[0]["extracted_text"] == text
    bad={"fact_type":"action","value":"伪造","start":0,"end":2,"quote_hash":text_hash("伪造"),"confirmed":False}
    with pytest.raises(ValueError):
        with v2_write_transaction(sessions,batch_id="d" * 32) as db:
            freeze_reused_material(db,revision_id=first["revision_id"],candidates=[bad])
    with sessions() as db:
        assert len(db.scalars(select(FactSnapshot)).all()) == 1


def test_deletion_requires_fresh_impact_graph_and_keeps_evidence_if_requested(app_client):
    client,sessions=app_client
    text="虚构记录"
    first=create_material(client,text)
    url=f"/api/v2/materials/{first['material_id']}"
    preview=client.get(url+"/deletion-preview",params={"mode":"preserve_existing"}).json()
    assert preview["affected_batch_ids"] == []
    snapshot_id,_=attach(sessions,first["revision_id"],text)
    old=client.request("DELETE",url,json={"mode":"preserve_existing","impact_token":preview["impact_token"]})
    assert old.status_code == 409
    assert client.get(url+f"/revisions/{first['revision_id']}").status_code == 200
    fresh=client.get(url+"/deletion-preview",params={"mode":"preserve_existing"}).json()
    assert fresh["affected_batch_ids"] == ["b" * 32]
    assert client.request("DELETE",url,json={"mode":"preserve_existing","impact_token":fresh["impact_token"]}).status_code == 204
    assert client.get(url+f"/revisions/{first['revision_id']}").status_code == 404
    with sessions() as db:
        assert db.get(Batch,"b" * 32) is not None
        assert db.get(FactSnapshot,snapshot_id).source_copies[0]["extracted_text"] == text
    with pytest.raises(ValueError):
        with v2_write_transaction(sessions,batch_id="e" * 32) as db:
            freeze_reused_material(db,revision_id=first["revision_id"],candidates=[])


def test_cascade_deletes_affected_batches_and_requires_enabled_flag(app_client,monkeypatch):
    client,sessions=app_client
    first=create_material(client,"虚构记录")
    snapshot_id,_=attach(sessions,first["revision_id"],"虚构记录")
    url=f"/api/v2/materials/{first['material_id']}"
    preview=client.get(url+"/deletion-preview").json()
    assert client.request("DELETE",url,json={"impact_token":preview["impact_token"]}).status_code == 204
    with sessions() as db:
        assert db.get(Batch,"b" * 32) is None and db.get(FactSnapshot,snapshot_id) is None
    monkeypatch.setenv("COACH_V2_ENABLED","0")
    assert client.get("/api/v2/materials").json() == {"materials": []}
    assert client.post("/api/v2/materials",json={"extracted_text":"fiction","source_type":"text"}).status_code == 503


def test_tampered_source_hash_rejected_even_with_no_claims(app_client):
    client,sessions=app_client
    first=create_material(client,"虚构原文")
    with sessions() as db:
        db.get(MaterialRevision,first["revision_id"]).content_hash="a" * 64
        db.commit()
    assert client.get(f"/api/v2/materials/{first['material_id']}/revisions/{first['revision_id']}").status_code == 503
    with pytest.raises(ValueError):
        with v2_write_transaction(sessions,batch_id="c" * 32) as db:
            freeze_reused_material(db,revision_id=first["revision_id"],candidates=[])


@pytest.mark.parametrize("mode", ["cascade", "preserve_existing"])
def test_api_deletion_replays_against_older_independent_business_backup(app_client, tmp_path, mode):
    import sqlite3
    from app.privacy import replay_deletions
    client, sessions = app_client
    text = "虚构设备记录"
    material = create_material(client, text)
    snapshot_id,_ = attach(sessions, material["revision_id"], text)
    database_path = sessions.kw["bind"].url.database
    backup_path = tmp_path / "fictional-old-backup.sqlite"
    with sqlite3.connect(database_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    url = f"/api/v2/materials/{material['material_id']}"
    preview = client.get(url + "/deletion-preview", params={"mode":mode}).json()
    assert client.request("DELETE", url, json={"mode":mode,"impact_token":preview["impact_token"]}).status_code == 204
    sessions.kw["bind"].dispose()
    with sqlite3.connect(backup_path) as backup, sqlite3.connect(database_path) as target:
        backup.backup(target)
    assert replay_deletions(sessions) == (1 if mode == "cascade" else 0)
    assert client.get(url + f"/revisions/{material['revision_id']}").status_code == 404
    with sessions() as db:
        assert db.get(MaterialRevision,material["revision_id"]) is None
        assert (db.get(Batch,"b" * 32) is None) == (mode == "cascade")
        assert (db.get(FactSnapshot,snapshot_id) is None) == (mode == "cascade")


def test_impact_token_includes_delete_mode_and_can_only_be_used_for_previewed_graph(app_client):
    client,sessions=app_client
    material = create_material(client, "虚构内容")
    url=f"/api/v2/materials/{material['material_id']}"
    preview=client.get(url + "/deletion-preview",params={"mode":"preserve_existing"}).json()
    assert client.request("DELETE",url,json={"mode":"cascade","impact_token":preview["impact_token"]}).status_code == 409
    assert client.get(url+f"/revisions/{material['revision_id']}").status_code == 200


def test_reuse_rejects_unbounded_candidate_payloads_and_bool_offsets(app_client):
    client,sessions=app_client
    material=create_material(client,"虚构内容")
    for candidate in (
        {"fact_type":"x" * 81,"value":"虚构","start":0,"end":2,"quote_hash":text_hash("虚构")},
        {"fact_type":"action","value":"x" * 2001,"start":0,"end":2,"quote_hash":text_hash("虚构")},
        {"fact_type":"action","value":"虚构","start":False,"end":2,"quote_hash":text_hash("虚构")},
        {"fact_type":"action","value":"虚构","start":0,"end":2,"quote_hash":text_hash("虚构"),"confirmed":"yes"},
    ):
        with pytest.raises(ValueError):
            with v2_write_transaction(sessions,batch_id="c" * 32) as db:
                freeze_reused_material(db,revision_id=material["revision_id"],candidates=[candidate])


def test_material_deletion_and_recovery_remain_available_when_v2_disabled(app_client, monkeypatch):
    from app.privacy import deleted_ids, replay_deletions
    client, sessions = app_client
    material = create_material(client,"虚构保留内容")
    attach(sessions,material["revision_id"],"虚构保留内容")
    url=f"/api/v2/materials/{material['material_id']}"
    monkeypatch.setenv("COACH_V2_ENABLED","0")
    listed = client.get("/api/v2/materials")
    assert listed.status_code == 200 and listed.json()["materials"][0]["material_id"] == material["material_id"]
    assert "extracted_text" not in listed.json()["materials"][0]
    assert client.get(url+f"/revisions/{material['revision_id']}").status_code == 503
    preview=client.get(url+"/deletion-preview")
    assert preview.status_code == 200 and preview.json()["affected_batch_ids"] == ["b" * 32]
    assert client.request("DELETE",url,json={"mode":"cascade","impact_token":preview.json()["impact_token"]}).status_code == 204
    assert "b" * 32 in deleted_ids()
    assert replay_deletions(sessions) == 0
    with sessions() as db:
        assert db.get(MaterialRevision,material["revision_id"]) is None and db.get(Batch,"b" * 32) is None


def test_unknown_material_preview_and_delete_do_not_claim_success(app_client):
    client,_=app_client
    url = "/api/v2/materials/" + "a" * 32
    assert client.get(url+"/deletion-preview").status_code == 404
    assert client.request("DELETE",url,json={"mode":"cascade","impact_token":"0"*64}).status_code == 404
