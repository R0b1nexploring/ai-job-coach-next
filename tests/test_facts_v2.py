import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.content_v2 import Claim, ClaimEvidence, text_hash
from app.facts_v2 import FactStorageError, create_fact_snapshot, create_material_revision
from app.models import FactSnapshot, MaterialRevision


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", hide_parameters=True)
    MaterialRevision.__table__.create(engine)
    FactSnapshot.__table__.create(engine)
    from app.privacy_v2 import v2_write_transaction
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with v2_write_transaction(sessions, batch_id="b" * 32) as db:
        yield db
    engine.dispose()


def test_revisions_normalize_and_preserve_old_text(session):
    first = create_material_revision(session, scope_id="local", source_type="text", extracted_text="甲\r\n😀乙\r丙")
    second = create_material_revision(session, scope_id="local", source_type="manual", extracted_text="修改", material_id=first.material_id)
    assert first.id != second.id and (first.revision, second.revision) == (1, 2)
    assert first.extracted_text == "甲\n😀乙\n丙" and first.content_hash == text_hash(first.extracted_text)
    session.flush()
    session.expire_all()
    assert session.get(MaterialRevision, first.id).extracted_text == "甲\n😀乙\n丙"
    with pytest.raises(FactStorageError):
        create_material_revision(session, scope_id="other", source_type="text", extracted_text="x", material_id=first.material_id)


def test_snapshot_uses_exact_unicode_source_and_frozen_copies(session):
    source = create_material_revision(session, scope_id="local", source_type="text", extracted_text="甲\r\n😀乙\n丙")
    evidence = ClaimEvidence(source_revision_id=source.id, start=2, end=4, quote_hash=text_hash("😀乙"))
    claims = [Claim(claim_id="candidate-a", fact_type="action", value="😀乙", evidence=(evidence,), conflict_ids=("candidate-b",)),
              Claim(claim_id="candidate-b", fact_type="action", value=None, confirmation="unknown")]
    created = create_fact_snapshot(session, scope_id="local", claims=claims)
    snapshot = created.snapshot
    assert created.claim_ids["candidate-a"] == snapshot.claims[0]["claim_id"]
    assert created.claim_ids["candidate-b"] == snapshot.claims[1]["claim_id"]
    with pytest.raises(TypeError):
        created.claim_ids["candidate-a"] = "forged"
    assert snapshot.claims[0]["claim_id"] != "candidate-a"
    assert snapshot.claims[0]["conflict_ids"] == [created.claim_ids["candidate-b"]]
    assert all(claim["claim_id"] not in {"candidate-a", "candidate-b"} for claim in snapshot.claims)
    assert snapshot.source_copies[0]["extracted_text"] == "甲\n😀乙\n丙"
    assert snapshot.source_copies[0]["content_hash"] == source.content_hash
    assert "display_name" not in snapshot.source_copies[0]
    session.delete(source)
    session.flush()
    assert snapshot.source_copies[0]["revision_id"] == source.id


def test_snapshot_rejects_wrong_scope_offsets_hash_and_unconfirmed_upgrade(session):
    source = create_material_revision(session, scope_id="local", source_type="manual", extracted_text="甲😀乙")
    good = ClaimEvidence(source_revision_id=source.id, start=1, end=2, quote_hash=text_hash("😀"))
    claim = Claim(claim_id="draft", fact_type="action", value="😀", evidence=(good,))
    with pytest.raises(FactStorageError):
        create_fact_snapshot(session, scope_id="other", claims=[claim])
    with pytest.raises(FactStorageError):
        create_fact_snapshot(session, scope_id="local", claims=[claim, claim])
    with pytest.raises(FactStorageError):
        create_fact_snapshot(session, scope_id="local", claims=[claim.model_copy(update={"conflict_ids": ("missing",)})])
    bad_evidence = ClaimEvidence(source_revision_id=source.id, start=0, end=1, quote_hash=text_hash("😀"))
    with pytest.raises(FactStorageError):
        create_fact_snapshot(session, scope_id="local", claims=[claim.model_copy(update={"evidence": (bad_evidence,)})])
    confirmed = claim.model_copy(update={"confirmation": "user_confirmed"})
    with pytest.raises(FactStorageError):
        create_fact_snapshot(session, scope_id="local", claims=[confirmed])
    assert session.scalars(select(FactSnapshot)).all() == []
    saved = create_fact_snapshot(session, scope_id="local", claims=[confirmed], user_confirmed_claim_keys=frozenset({"draft"}))
    assert saved.snapshot.claims[0]["confirmation"] == "user_confirmed"
    assert saved.claim_ids["draft"] == saved.snapshot.claims[0]["claim_id"]


def test_transaction_rollback_and_tampered_source_hash(session):
    source = create_material_revision(session, scope_id="local", source_type="text", extracted_text="fiction")
    evidence = ClaimEvidence(source_revision_id=source.id, start=0, end=7, quote_hash=text_hash("fiction"))
    claim = Claim(claim_id="draft", fact_type="skill", value="fiction", evidence=(evidence,))
    source.content_hash = text_hash("tampered")
    session.flush()
    with pytest.raises(FactStorageError):
        create_fact_snapshot(session, scope_id="local", claims=[claim])
    session.rollback()
    assert session.scalars(select(MaterialRevision)).all() == []
    assert session.scalars(select(FactSnapshot)).all() == []
