import pytest
from sqlalchemy.orm import sessionmaker

from app.db import Base, make_engine
from app.privacy import delete_batch, deleted_ids, initialize_journal, installation_id, JournalUnavailable
from app.service import create_batch
from scripts.journal_tools import export_journal, import_journal


@pytest.fixture
def journal_sessions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'journal-business.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def batch(sessions, key):
    return create_batch(sessions, jd="完全虚构的机械设计岗位，仅用于删除记录验收。", material="", confirmed=False, idempotency_key=key)


def test_independent_journal_cannot_be_rolled_back(journal_sessions, tmp_path):
    old = tmp_path / "old-journal.tar.gz"
    export_journal(old)
    identifier = batch(journal_sessions, "fictional-journal-rollback").id
    delete_batch(journal_sessions, identifier)
    with pytest.raises(JournalUnavailable):
        import_journal(old, journal_sessions)
    assert identifier in deleted_ids()


def test_latest_journal_transfers_identity_to_an_empty_new_install(journal_sessions, tmp_path, monkeypatch):
    identifier = batch(journal_sessions, "fictional-journal-transfer").id
    delete_batch(journal_sessions, identifier)
    original_id = installation_id()
    snapshot = tmp_path / "latest-journal.tar.gz"
    export_journal(snapshot)
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "new-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "new-data"))
    initialize_journal(bootstrap=True)
    assert installation_id() != original_id
    import_journal(snapshot, journal_sessions)
    assert installation_id() == original_id
    assert identifier in deleted_ids()


def test_another_installation_cannot_overwrite_business_ownership(journal_sessions, tmp_path, monkeypatch):
    snapshot = tmp_path / "another-journal.tar.gz"
    export_journal(snapshot)
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "other-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "other-data"))
    initialize_journal(bootstrap=True)
    batch(journal_sessions, "fictional-existing-business")
    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, journal_sessions)
