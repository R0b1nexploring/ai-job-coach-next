import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Batch, FactSnapshot, MaterialRevision
from app.privacy_v2 import BatchDeletionIntent, DeletionPlanError, freeze_batch_intent, validate_replay


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", hide_parameters=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def batch(db, key, snapshot_id, version=2):
    row = Batch(id=key * 32, idempotency_key=key, input_hash="f" * 64,
                jd_text="fictional", material_text="", schema_version=version,
                fact_snapshot_id=snapshot_id, material_revision_ids=[] if version == 2 else None)
    db.add(row)
    db.flush()
    return row


def setup_batch(db):
    fact = FactSnapshot(id="a" * 32, scope_id="local", claims=[], source_copies=[])
    material = MaterialRevision(id="c" * 32, material_id="d" * 32, revision=1,
                                scope_id="local", source_type="manual", extracted_text="fiction",
                                content_hash="e" * 64)
    db.add_all([fact, material])
    db.flush()
    return batch(db, "b", fact.id), fact, material


def test_freezes_exact_private_dependency_without_deleting_or_materials(session):
    row, fact, material = setup_batch(session)
    intent = freeze_batch_intent(session, scope_id="local", batch_id=row.id)
    assert intent.fact_snapshot_ids == (fact.id,)
    assert material.id not in intent.model_dump_json()
    assert validate_replay(session, BatchDeletionIntent.model_validate_json(intent.model_dump_json())) == (fact.id,)
    assert session.get(Batch, row.id) is not None
    assert session.get(FactSnapshot, fact.id) is not None
    assert session.get(MaterialRevision, material.id) is not None
    with pytest.raises(ValidationError):
        intent.scope_id = "changed"


def test_scope_and_shared_snapshot_are_rejected(session):
    row, fact, _ = setup_batch(session)
    with pytest.raises(DeletionPlanError):
        freeze_batch_intent(session, scope_id="other", batch_id=row.id)
    intent = freeze_batch_intent(session, scope_id="local", batch_id=row.id)
    batch(session, "f", fact.id)
    with pytest.raises(DeletionPlanError):
        freeze_batch_intent(session, scope_id="local", batch_id=row.id)
    with pytest.raises(DeletionPlanError):
        validate_replay(session, intent)


def test_replay_requires_frozen_graph_and_scope(session):
    row, fact, _ = setup_batch(session)
    intent = freeze_batch_intent(session, scope_id="local", batch_id=row.id)
    row.fact_snapshot_id = "f" * 32
    session.flush()
    with pytest.raises(DeletionPlanError):
        validate_replay(session, intent)
    row.fact_snapshot_id = fact.id
    fact.scope_id = "other"
    session.flush()
    with pytest.raises(DeletionPlanError):
        validate_replay(session, intent)


def test_missing_frozen_rows_idempotent_but_v1_batch_cannot_be_planned(session):
    row, fact, _ = setup_batch(session)
    intent = freeze_batch_intent(session, scope_id="local", batch_id=row.id)
    session.delete(row)
    session.delete(fact)
    session.flush()
    assert validate_replay(session, intent) == ("a" * 32,)
    legacy = batch(session, "e", None, version=None)
    with pytest.raises(DeletionPlanError):
        freeze_batch_intent(session, scope_id="local", batch_id=legacy.id)


def test_malformed_frozen_intents_cannot_pass_validation():
    with pytest.raises(ValidationError):
        BatchDeletionIntent(scope_id="local", resource_id="not-an-id", fact_snapshot_ids=("a" * 32,))
    with pytest.raises(ValidationError):
        BatchDeletionIntent(scope_id="local", resource_id="b" * 32, fact_snapshot_ids=("a" * 32, "a" * 32))
    with pytest.raises(ValidationError):
        BatchDeletionIntent(scope_id="local", resource_id="b" * 32, fact_snapshot_ids=(), raw_personal_text="fiction")


@pytest.fixture
def guarded_sessions(tmp_path):
    from sqlalchemy.orm import sessionmaker
    from app.db import make_engine
    engine = make_engine(f"sqlite:///{tmp_path / 'v2-business.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def create_guarded_batch(sessions, batch_id="b" * 32, snapshot_id="a" * 32):
    from app.privacy_v2 import bind_batch_snapshot, v2_write_transaction
    with v2_write_transaction(sessions, batch_id=batch_id) as db:
        db.add(FactSnapshot(id=snapshot_id, scope_id="local", claims=[], source_copies=[]))
        db.add(Batch(id=batch_id, idempotency_key=batch_id, input_hash="f" * 64,
                     jd_text="fictional", material_text="", schema_version=2))
        db.flush()
        bind_batch_snapshot(db, batch_id=batch_id, snapshot_id=snapshot_id, material_revision_ids=())


def test_durable_delete_replay_and_late_writer_denied(guarded_sessions):
    from app.privacy import JournalUnavailable, deleted_ids, replay_deletions
    from app.privacy_v2 import delete_v2_batch, v2_write_transaction, V2Deleted
    from sqlalchemy.orm import Session, sessionmaker

    create_guarded_batch(guarded_sessions)
    class FailCommit(Session):
        def commit(self):
            raise RuntimeError("synthetic commit failure")
    failing = sessionmaker(bind=guarded_sessions.kw["bind"], class_=FailCommit)
    with pytest.raises(RuntimeError, match="synthetic"):
        delete_v2_batch(failing, batch_id="b" * 32)
    assert "b" * 32 in deleted_ids()
    with guarded_sessions() as db:
        assert db.get(Batch, "b" * 32) is not None
        assert db.get(FactSnapshot, "a" * 32) is not None
    with pytest.raises(V2Deleted):
        with v2_write_transaction(guarded_sessions, batch_id="b" * 32):
            pass
    assert replay_deletions(guarded_sessions) == 1
    assert replay_deletions(guarded_sessions) == 0
    with guarded_sessions() as db:
        assert db.get(Batch, "b" * 32) is None
        assert db.get(FactSnapshot, "a" * 32) is None


def test_guard_serializes_inflight_writer_with_deletion(guarded_sessions):
    import threading
    from app.privacy import JournalUnavailable
    from app.privacy_v2 import delete_v2_batch, v2_write_transaction, V2Deleted

    create_guarded_batch(guarded_sessions)
    entered, release, deleting, finished = (threading.Event() for _ in range(4))
    errors = []
    def writer():
        try:
            with v2_write_transaction(guarded_sessions, batch_id="b" * 32) as db:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test timeout")
                db.get(Batch, "b" * 32).jd_text = "updated fictional text"
        except BaseException as exc:
            errors.append(exc)
    def remover():
        try:
            deleting.set()
            delete_v2_batch(guarded_sessions, batch_id="b" * 32)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    a = threading.Thread(target=writer)
    b = threading.Thread(target=remover)
    a.start()
    assert entered.wait(5)
    b.start()
    assert deleting.wait(5)
    try:
        assert not finished.wait(0.2)
    finally:
        release.set()
        a.join(5)
        b.join(5)
    assert finished.is_set() and not errors
    with pytest.raises(V2Deleted):
        with v2_write_transaction(guarded_sessions, batch_id="b" * 32):
            pass


def test_old_v1_delete_never_deletes_v2(guarded_sessions):
    from app.privacy import JournalUnavailable, delete_batch, deleted_ids
    create_guarded_batch(guarded_sessions)
    with pytest.raises(JournalUnavailable):
        delete_batch(guarded_sessions, "b" * 32)
    assert "b" * 32 not in deleted_ids()


def test_v2_journal_archive_cannot_restore_older_intent_state(guarded_sessions, tmp_path):
    from app.privacy import JournalUnavailable
    from app.privacy_v2 import delete_v2_batch
    from scripts.journal_tools import export_journal, import_journal
    create_guarded_batch(guarded_sessions)
    old = tmp_path / "old-v2-journal.tar.gz"
    export_journal(old)
    delete_v2_batch(guarded_sessions, batch_id="b" * 32)
    with pytest.raises(JournalUnavailable):
        import_journal(old, guarded_sessions)


def test_fact_writes_refuse_unguarded_sessions(guarded_sessions):
    from app.facts_v2 import FactStorageError, create_fact_snapshot, create_material_revision
    with guarded_sessions() as session:
        with pytest.raises(FactStorageError):
            create_material_revision(session, scope_id="local", source_type="text", extracted_text="fictional")
        with pytest.raises(FactStorageError):
            create_fact_snapshot(session, scope_id="local", claims=[])


def test_batch_deletion_removes_owned_revisions_preserves_explicit_independent(guarded_sessions):
    from app.facts_v2 import create_material_revision, create_fact_snapshot
    from app.privacy_v2 import bind_batch_snapshot, delete_v2_batch, v2_write_transaction
    batch_id = "b" * 32
    with v2_write_transaction(guarded_sessions, batch_id=batch_id) as db:
        owned = create_material_revision(db, scope_id="local", source_type="text", extracted_text="fictional owned")
        later = create_material_revision(db, scope_id="local", source_type="text", extracted_text="new revision", material_id=owned.material_id)
        independent = create_material_revision(db, scope_id="local", source_type="text", extracted_text="fictional independent", retain_independently=True)
        snapshot = create_fact_snapshot(db, scope_id="local", claims=[]).snapshot
        db.add(Batch(id=batch_id, idempotency_key=batch_id, input_hash="f" * 64,
                     jd_text="fiction", material_text="", schema_version=2))
        db.flush()
        bind_batch_snapshot(db, batch_id=batch_id, snapshot_id=snapshot.id, material_revision_ids=(later.id, independent.id))
        owned_ids, independent_id, snapshot_id = (owned.id, later.id), independent.id, snapshot.id
    delete_v2_batch(guarded_sessions, batch_id=batch_id)
    with guarded_sessions() as db:
        assert db.get(Batch, batch_id) is None and db.get(FactSnapshot, snapshot_id) is None
        assert all(db.get(MaterialRevision, identifier) is None for identifier in owned_ids)
        assert db.get(MaterialRevision, independent_id) is not None


def test_owned_material_cannot_be_attached_to_another_batch(guarded_sessions):
    from app.facts_v2 import create_material_revision, create_fact_snapshot
    from app.privacy_v2 import bind_batch_snapshot, v2_write_transaction
    first, second = "b" * 32, "c" * 32
    with v2_write_transaction(guarded_sessions, batch_id=first) as db:
        source = create_material_revision(db, scope_id="local", source_type="text", extracted_text="fictional")
        source_id = source.id
    with pytest.raises(DeletionPlanError):
        with v2_write_transaction(guarded_sessions, batch_id=second) as db:
            snap = create_fact_snapshot(db, scope_id="local", claims=[]).snapshot
            db.add(Batch(id=second, idempotency_key=second, input_hash="f" * 64,
                         jd_text="fiction", material_text="", schema_version=2))
            db.flush()
            bind_batch_snapshot(db, batch_id=second, snapshot_id=snap.id, material_revision_ids=(source_id,))


def test_unknown_legacy_ownership_fails_closed(guarded_sessions):
    from app.privacy_v2 import delete_v2_batch, v2_write_transaction
    create_guarded_batch(guarded_sessions)
    with v2_write_transaction(guarded_sessions, batch_id="b" * 32) as db:
        db.add(MaterialRevision(id="c" * 32, material_id="d" * 32, revision=1, scope_id="local",
                                source_type="manual", extracted_text="fiction", content_hash="e" * 64))
        db.get(Batch, "b" * 32).material_revision_ids = ["c" * 32]
    with pytest.raises(DeletionPlanError):
        delete_v2_batch(guarded_sessions, batch_id="b" * 32)
    with guarded_sessions() as db:
        assert db.get(Batch, "b" * 32) and db.get(MaterialRevision, "c" * 32)


def test_old_business_backup_replay_removes_batch_and_owned_material(guarded_sessions):
    from app.facts_v2 import create_fact_snapshot, create_material_revision
    from app.privacy import replay_deletions
    from app.privacy_v2 import bind_batch_snapshot, delete_v2_batch, v2_write_transaction
    batch_id = "b" * 32
    with v2_write_transaction(guarded_sessions, batch_id=batch_id) as db:
        source = create_material_revision(db, scope_id="local", source_type="text", extracted_text="fictional only")
        snapshot = create_fact_snapshot(db, scope_id="local", claims=[]).snapshot
        db.add(Batch(id=batch_id, idempotency_key=batch_id, input_hash="f" * 64, jd_text="fiction", material_text="", schema_version=2))
        db.flush()
        bind_batch_snapshot(db, batch_id=batch_id, snapshot_id=snapshot.id, material_revision_ids=(source.id,))
        material_id, material_revision_id, snapshot_id, digest = source.material_id, source.id, snapshot.id, source.content_hash
    delete_v2_batch(guarded_sessions, batch_id=batch_id)
    # Simulate the old business backup being restored while API/worker are off;
    # the independent deletion journal remains newer than the backup.
    with guarded_sessions() as db:
        db.add(MaterialRevision(id=material_revision_id, material_id=material_id, revision=1,
                                scope_id="local", source_type="text", extracted_text="fictional only", content_hash=digest,
                                retention="batch", owner_batch_id=batch_id))
        db.add(FactSnapshot(id=snapshot_id, scope_id="local", claims=[], source_copies=[]))
        db.add(Batch(id=batch_id, idempotency_key=batch_id, input_hash="f" * 64, jd_text="fiction", material_text="",
                     schema_version=2, fact_snapshot_id=snapshot_id, material_revision_ids=[material_revision_id]))
        db.commit()
    assert replay_deletions(guarded_sessions) == 1
    with guarded_sessions() as db:
        assert db.get(Batch, batch_id) is None
        assert db.get(MaterialRevision, material_revision_id) is None
        assert db.get(FactSnapshot, snapshot_id) is None


def create_shared_material(guarded_sessions):
    from app.content_v2 import Claim, ClaimEvidence, text_hash
    from app.facts_v2 import create_fact_snapshot, create_material_revision
    from app.privacy_v2 import bind_batch_snapshot, v2_write_transaction
    with v2_write_transaction(guarded_sessions) as db:
        source = create_material_revision(db, scope_id="local", source_type="text", extracted_text="fictional material", retain_independently=True)
        material_id, revision_id = source.material_id, source.id
    for key in ("b", "c"):
        batch_id = key * 32
        with v2_write_transaction(guarded_sessions, batch_id=batch_id) as db:
            own = create_material_revision(db, scope_id="local", source_type="text", extracted_text="batch private")
            evidence = ClaimEvidence(source_revision_id=revision_id, start=0, end=7, quote_hash=text_hash("fiction"))
            claim = Claim(claim_id="candidate", fact_type="skill", value="fiction", evidence=(evidence,))
            snap = create_fact_snapshot(db, scope_id="local", claims=[claim]).snapshot
            db.add(Batch(id=batch_id, idempotency_key=batch_id, input_hash="f" * 64, jd_text="fiction", material_text="", schema_version=2))
            db.flush()
            bind_batch_snapshot(db, batch_id=batch_id, snapshot_id=snap.id, material_revision_ids=(revision_id, own.id))
    return material_id, revision_id


@pytest.mark.parametrize("mode", ["cascade", "preserve_existing"])
def test_independent_material_deletion_uses_frozen_dependencies(guarded_sessions, mode):
    from app.facts_v2 import FactStorageError, create_material_revision
    from app.privacy_v2 import V2Deleted, delete_independent_material, delete_v2_batch, require_material_guard, v2_write_transaction
    material_id, revision_id = create_shared_material(guarded_sessions)
    delete_independent_material(guarded_sessions, material_id=material_id, mode=mode)
    with guarded_sessions() as db:
        assert db.get(MaterialRevision, revision_id) is None
        assert (db.get(Batch, "b" * 32) is None) == (mode == "cascade")
        assert (db.get(Batch, "c" * 32) is None) == (mode == "cascade")
        if mode == "cascade":
            assert db.query(FactSnapshot).count() == 0
            assert db.query(MaterialRevision).count() == 0
    with v2_write_transaction(guarded_sessions) as db:
        with pytest.raises(V2Deleted):
            require_material_guard(db, material_id=material_id, revision_id=revision_id)
        with pytest.raises(FactStorageError):
            create_material_revision(db, scope_id="local", source_type="text", extracted_text="forbidden", material_id=material_id, retain_independently=True)
    if mode == "preserve_existing":
        delete_v2_batch(guarded_sessions, batch_id="b" * 32)
        with guarded_sessions() as db:
            assert db.get(Batch, "b" * 32) is None
            assert db.get(Batch, "c" * 32) is not None
    else:
        with pytest.raises(V2Deleted):
            with v2_write_transaction(guarded_sessions, batch_id="b" * 32):
                pass


def test_material_commit_failure_blocks_reuse_then_replays(guarded_sessions):
    from sqlalchemy.orm import Session, sessionmaker
    from app.facts_v2 import FactStorageError, create_material_revision
    from app.privacy import replay_deletions
    from app.privacy_v2 import delete_independent_material, v2_write_transaction
    material_id, revision_id = create_shared_material(guarded_sessions)
    class FailCommit(Session):
        def commit(self):
            raise RuntimeError("synthetic failure")
    failing = sessionmaker(bind=guarded_sessions.kw["bind"], class_=FailCommit)
    with pytest.raises(RuntimeError):
        delete_independent_material(failing, material_id=material_id, mode="preserve_existing")
    with v2_write_transaction(guarded_sessions) as db:
        assert db.get(MaterialRevision, revision_id) is not None
        with pytest.raises(FactStorageError):
            create_material_revision(db, scope_id="local", source_type="text", extracted_text="forbidden", material_id=material_id, retain_independently=True)
    assert replay_deletions(guarded_sessions) == 0
    with guarded_sessions() as db:
        assert db.get(MaterialRevision, revision_id) is None
        assert db.get(Batch, "b" * 32) is not None


@pytest.mark.parametrize("mode", ["cascade", "preserve_existing"])
def test_material_intent_replays_after_restoring_old_business_backup(guarded_sessions, tmp_path, mode):
    import sqlite3
    from app.privacy import replay_deletions
    from app.privacy_v2 import delete_independent_material
    material_id, revision_id = create_shared_material(guarded_sessions)
    database_path = guarded_sessions.kw["bind"].url.database
    backup_path = tmp_path / "older-fictional-business.sqlite"
    with sqlite3.connect(database_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    delete_independent_material(guarded_sessions, material_id=material_id, mode=mode)
    # Simulate stopped workers with an independently retained, newer journal.
    guarded_sessions.kw["bind"].dispose()
    with sqlite3.connect(backup_path) as backup, sqlite3.connect(database_path) as target:
        backup.backup(target)
    assert replay_deletions(guarded_sessions) == (2 if mode == "cascade" else 0)
    with guarded_sessions() as db:
        assert db.get(MaterialRevision, revision_id) is None
        assert (db.get(Batch, "b" * 32) is None) == (mode == "cascade")
        assert (db.get(Batch, "c" * 32) is None) == (mode == "cascade")


def test_material_journal_blocks_reference_when_delete_commit_fails(guarded_sessions):
    from sqlalchemy.orm import Session, sessionmaker
    from app.facts_v2 import FactStorageError, create_fact_snapshot
    from app.content_v2 import Claim, ClaimEvidence, text_hash
    from app.privacy_v2 import V2Deleted, bind_batch_snapshot, delete_independent_material, v2_write_transaction
    material_id, revision_id = create_shared_material(guarded_sessions)
    class FailCommit(Session):
        def commit(self):
            raise RuntimeError("synthetic failure")
    failing = sessionmaker(bind=guarded_sessions.kw["bind"], class_=FailCommit)
    with pytest.raises(RuntimeError):
        delete_independent_material(failing, material_id=material_id, mode="preserve_existing")
    with v2_write_transaction(guarded_sessions, batch_id="d" * 32) as db:
        claim = Claim(claim_id="x", fact_type="action", value="fiction",
                      evidence=(ClaimEvidence(source_revision_id=revision_id, start=0, end=7, quote_hash=text_hash("fiction")),))
        with pytest.raises(FactStorageError):
            create_fact_snapshot(db, scope_id="local", claims=[claim])
        snapshot = create_fact_snapshot(db, scope_id="local", claims=[]).snapshot
        db.add(Batch(id="d" * 32, idempotency_key="d" * 32, input_hash="f" * 64,
                     jd_text="fiction", material_text="", schema_version=2))
        db.flush()
        with pytest.raises(V2Deleted):
            bind_batch_snapshot(db, batch_id="d" * 32, snapshot_id=snapshot.id, material_revision_ids=(revision_id,))
        db.rollback()


def test_real_service_batch_delete_cleans_drafts_guides_history_and_material(guarded_sessions, monkeypatch):
    from sqlalchemy import select
    from app.fact_candidates_v2 import extract_candidates
    from app.models import Guide, GuideRevision, Resume, ResumeRevision, TaskExecution
    from app.privacy_v2 import delete_v2_batch
    from app.service_v2 import create_v2_batch
    from app.worker_v2 import process_batch_v2_once, process_guide_v2_once
    monkeypatch.setenv("COACH_PROVIDER", "local")
    text = "虚构设备台账"
    candidates = extract_candidates(text)["candidates"]
    result = create_v2_batch(guarded_sessions, jd="虚构岗位负责工程记录整理、资料核对及相关协作工作。", material=text,
                             candidates=candidates, header={}, resume_locale="zh-CN", guide_locale="zh-CN",
                             idempotency_key="fictional-full-delete")
    batch_id = result["id"]
    assert process_batch_v2_once(guarded_sessions)
    assert process_guide_v2_once(guarded_sessions)
    assert process_guide_v2_once(guarded_sessions)
    with guarded_sessions() as db:
        batch = db.get(Batch, batch_id)
        fact_id, material_ids = batch.fact_snapshot_id, list(batch.material_revision_ids)
        assert db.scalars(select(TaskExecution).where(TaskExecution.batch_id == batch_id)).all()
        assert db.scalars(select(GuideRevision).where(GuideRevision.batch_id == batch_id)).all()
    delete_v2_batch(guarded_sessions, batch_id=batch_id)
    with guarded_sessions() as db:
        assert db.get(Batch, batch_id) is None and db.get(FactSnapshot, fact_id) is None
        assert all(db.get(MaterialRevision, identifier) is None for identifier in material_ids)
        for model in (Resume, ResumeRevision, Guide, GuideRevision, TaskExecution):
            assert not db.scalars(select(model)).all()


def test_material_deletion_preview_lists_frozen_impact_without_side_effects(guarded_sessions):
    from app.privacy_v2 import preview_independent_material_deletion
    material_id, revision_id = create_shared_material(guarded_sessions)
    preview = preview_independent_material_deletion(guarded_sessions, material_id=material_id, mode="preserve_existing")
    assert {key: value for key, value in preview.items() if key != "impact_token"} == {"material_id": material_id, "mode": "preserve_existing", "revision_count": 1,
                       "affected_batch_ids": ["b" * 32, "c" * 32]}
    assert len(preview["impact_token"]) == 64
    with guarded_sessions() as db:
        assert db.get(MaterialRevision, revision_id) is not None
        assert db.get(Batch, "b" * 32) is not None and db.get(Batch, "c" * 32) is not None


def test_journal_guard_serializes_a_separate_writer_process(guarded_sessions):
    import os
    import select as select_module
    import subprocess
    import sys
    import threading
    from app.privacy_v2 import delete_v2_batch
    create_guarded_batch(guarded_sessions)
    url = guarded_sessions.kw["bind"].url.render_as_string(hide_password=False)
    code = '''import sys
from sqlalchemy.orm import sessionmaker
from app.db import make_engine
from app.privacy_v2 import v2_write_transaction
factory = sessionmaker(bind=make_engine(sys.argv[1]))
with v2_write_transaction(factory, batch_id="b" * 32):
    print("locked", flush=True)
    sys.stdin.read(1)
'''
    child = subprocess.Popen([sys.executable, "-c", code, url], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=os.environ.copy(), text=True)
    finished = threading.Event()
    errors = []
    thread = None
    try:
        assert select_module.select([child.stdout], [], [], 5)[0], "child did not acquire guard"
        assert child.stdout.readline().strip() == "locked"
        def remove():
            try:
                delete_v2_batch(guarded_sessions, batch_id="b" * 32)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=remove)
        thread.start()
        assert not finished.wait(0.2)
        child.stdin.write("x")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        assert finished.wait(5)
        assert not errors
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if thread:
            thread.join(5)


def test_non_batch_material_tombstone_prevents_cross_install_journal_replacement(guarded_sessions, tmp_path, monkeypatch):
    from app.facts_v2 import create_material_revision
    from app.privacy import JournalUnavailable, initialize_journal
    from app.privacy_v2 import delete_independent_material, v2_write_transaction
    from scripts.journal_tools import export_journal, import_journal
    # Prepare a journal from another empty installation.
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "external-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "external-data"))
    initialize_journal(bootstrap=True)
    external = tmp_path / "external-journal.tar.gz"
    export_journal(external)
    # A second installation has no remaining business rows after a material-only
    # deletion, but its tombstone must still forbid journal replacement.
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "current-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "current-data"))
    initialize_journal(bootstrap=True)
    with v2_write_transaction(guarded_sessions) as db:
        source = create_material_revision(db, scope_id="local", source_type="text", extracted_text="fictional", retain_independently=True)
        material_id = source.material_id
    delete_independent_material(guarded_sessions, material_id=material_id)
    with pytest.raises(JournalUnavailable):
        import_journal(external, guarded_sessions)
