import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import Base, make_engine
from app.fact_candidates_v2 import extract_candidates
from app.models import Batch, FactSnapshot, GuideRevision, MaterialRevision, ResumeRevision
from app.service import InputConflict, NotFound
from app.service_v2 import create_v2_batch, export_v2_document, get_v2_batch, load_fact_snapshot, retry_v2_guide, save_v2_resume


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'fictional.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_create_freezes_fact_evidence_and_explicit_confirmation(sessions):
    source = "虚构项目记录\n整理设备台账"
    rows = extract_candidates(source)["candidates"]
    rows[0]["fact_type"] = "project"
    rows[1]["fact_type"] = "action"
    rows[1]["confirmed"] = True
    args = {"jd": "虚构岗位负责工程记录整理、资料核对及相关协作工作。", "material": source,
            "candidates": rows, "header": {"name": "虚构人物"}, "resume_locale": "en", "guide_locale": "zh-CN",
            "idempotency_key": "fictional-request"}
    result = create_v2_batch(sessions, **args)
    assert create_v2_batch(sessions, **args) == result
    view = get_v2_batch(sessions, result["id"])
    assert view["status"] == "queued" and view["resumes"] == {} and view["facts"][1]["confirmation"] == "user_confirmed"
    with sessions() as session:
        batch = session.get(Batch, result["id"])
        snap = session.get(FactSnapshot, batch.fact_snapshot_id)
        material = session.get(MaterialRevision, batch.material_revision_ids[0])
        assert batch.schema_version == 2 and batch.material_text == ""
        assert snap.source_copies[0]["extracted_text"] == material.extracted_text == source
        facts = load_fact_snapshot(snap)
        assert [claim.confirmation for claim in facts.claims] == ["unconfirmed", "user_confirmed"]
        assert facts.claims[1].evidence[0].source_revision_id == material.id
    with pytest.raises(InputConflict):
        create_v2_batch(sessions, **{**args, "guide_locale": "en"})


def test_rejects_forged_evidence_and_rolls_back_all_rows(sessions):
    candidate = extract_candidates("虚构素材")["candidates"][0]
    candidate["quote_hash"] = "a" * 64
    with pytest.raises(ValueError):
        create_v2_batch(sessions, jd="虚构岗位负责工程记录整理、资料核对及相关协作工作。", material="虚构素材",
                        candidates=[candidate], header={}, resume_locale="zh-CN", guide_locale="zh-CN", idempotency_key="fictional-invalid")
    with sessions() as session:
        assert not session.scalars(select(Batch)).all()
        assert not session.scalars(select(FactSnapshot)).all()
        assert not session.scalars(select(MaterialRevision)).all()


def test_empty_material_is_valid_without_invented_facts(sessions):
    result = create_v2_batch(sessions, jd="虚构岗位负责工程记录整理、资料核对及相关协作工作。", material="",
                             candidates=[], header={}, resume_locale="zh-CN", guide_locale="zh-CN", idempotency_key="fictional-empty")
    with sessions() as session:
        batch = session.get(Batch, result["id"])
        assert batch.material_revision_ids == []
        assert session.get(FactSnapshot, batch.fact_snapshot_id).claims == []


def test_saved_revision_guides_and_export_stay_bound(sessions):
    from app.worker_v2 import process_batch_v2_once, process_guide_v2_once

    candidate = extract_candidates("整理虚构设备台账")["candidates"][0]
    candidate["fact_type"] = "action"
    result = create_v2_batch(sessions, jd="虚构岗位负责工程记录整理、资料核对及相关协作工作。", material="整理虚构设备台账",
                             candidates=[candidate], header={}, resume_locale="zh-CN", guide_locale="en", idempotency_key="fictional-flow")
    batch_id = result["id"]
    assert process_batch_v2_once(sessions)
    assert process_guide_v2_once(sessions) and process_guide_v2_once(sessions)
    before = get_v2_batch(sessions, batch_id)
    assert before["review"]["status"] == "unavailable"
    assert before["guides"]["job"]["status"] == before["guides"]["blueprint"]["status"] == "ready"
    original = before["resumes"]["job"]
    saved = save_v2_resume(sessions, batch_id=batch_id, kind="job", expected_revision=1,
                           title=original["document"]["title"], unit_texts={}, header={"name": "虚构姓名"})
    assert saved["revision"] == 2
    after_header = get_v2_batch(sessions, batch_id)
    assert after_header["resumes"]["job"]["semantic_hash"] == original["semantic_hash"]
    assert after_header["guides"]["job"]["resume_revision"] == 1
    assert after_header["guides"]["job"]["applies_to_revision"] == 2
    # Omitting the header while editing does not clear independently saved data.
    summary = after_header["resumes"]["job"]["document"]["summary"]
    save_v2_resume(sessions, batch_id=batch_id, kind="job", expected_revision=2,
                   title=original["document"]["title"], unit_texts={summary["unit_id"]: "重新整理虚构资料"}, header=None)
    changed = get_v2_batch(sessions, batch_id)
    assert changed["resumes"]["job"]["header"]["name"] == "虚构姓名"
    assert changed["guides"]["job"]["status"] == "stale"
    assert changed["guides"]["blueprint"]["status"] == "ready"
    assert changed["resumes"]["job"]["review"]["status"] == "unavailable"
    with pytest.raises(InputConflict):
        save_v2_resume(sessions, batch_id=batch_id, kind="job", expected_revision=2, title="旧编辑", unit_texts={}, header={})
    html, meta = export_v2_document(sessions, batch_id=batch_id, kind="job", document_type="resume", file_format="html", revision=1)
    assert "虚构姓名" not in html.decode() and meta["revision"] == 1 and meta["draft"]
    with pytest.raises(NotFound):
        export_v2_document(sessions, batch_id=batch_id, kind="job", document_type="guide", file_format="docx")
    retry_v2_guide(sessions, batch_id=batch_id, kind="job", guide_locale="zh-CN")
    assert process_guide_v2_once(sessions)
    current = get_v2_batch(sessions, batch_id)
    assert current["guides"]["job"]["resume_revision"] == 3
    assert current["guides"]["job"]["locale"] == "zh-CN"
    with sessions() as session:
        assert len(session.scalars(select(ResumeRevision).where(ResumeRevision.resume_id == original["id"])).all()) == 3
        assert len(session.scalars(select(GuideRevision).where(GuideRevision.batch_id == batch_id, GuideRevision.guide_locale == "en")).all()) == 2


def test_edit_reorders_sections_and_adds_deletes_units_with_stable_ids(sessions):
    from app.worker_v2 import process_batch_v2_once
    candidate = extract_candidates("虚构设备记录")["candidates"][0]
    candidate["fact_type"] = "action"
    batch_id = create_v2_batch(sessions, jd="虚构岗位负责工程记录整理、资料核对及相关协作工作。", material="虚构设备记录",
                               candidates=[candidate], header={}, resume_locale="zh-CN", guide_locale="zh-CN", idempotency_key="fictional-edit-order")["id"]
    assert process_batch_v2_once(sessions)
    initial = get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]
    sections = initial["document"]["sections"]
    assert len(sections) == 2
    entry = sections[0]["entries"][0]
    old_id = entry["units"][0]["unit_id"]
    save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                   title=initial["document"]["title"], unit_texts={}, header=None,
                   section_order=[section["section_id"] for section in reversed(sections)],
                   delete_unit_ids=[old_id], add_units=[{"entry_id": entry["entry_id"], "text": "手动补充的虚构内容"}])
    updated = get_v2_batch(sessions, batch_id)
    new_sections = updated["resumes"]["blueprint"]["document"]["sections"]
    assert [section["section_id"] for section in new_sections] == [section["section_id"] for section in reversed(sections)]
    new_unit = new_sections[1]["entries"][0]["units"][0]
    assert new_unit["unit_id"] != old_id and new_unit["claim_refs"] == []
    assert new_unit["authorship"] == "user_edited" and new_unit["completion"] == "unknown"
    assert updated["resumes"]["job"]["revision"] == 1 and updated["guides"]["blueprint"]["status"] == "stale"
    with pytest.raises(ValueError):
        save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=2,
                       title=initial["document"]["title"], unit_texts={new_unit["unit_id"]: "改写"}, header=None,
                       delete_unit_ids=[new_unit["unit_id"]])
