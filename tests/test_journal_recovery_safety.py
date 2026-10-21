"""Deletion-state recovery boundaries, using only isolated fictional data."""
from io import BytesIO
import json
import os
from pathlib import Path
import tarfile

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import Base, make_engine
from app.privacy import (
    JournalUnavailable, delete_batch, deleted_ids, installation_id,
    journal_path,
)
from app.service import create_batch
from scripts.journal_tools import export_journal, import_journal


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'fictional-recovery.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def fictional_batch(sessions, key):
    return create_batch(sessions, jd="虚构机械岗位，仅供删除记录恢复测试。", material="", confirmed=False, idempotency_key=key).id


@pytest.mark.parametrize("surviving_business", [False, True])
def test_missing_journal_cannot_import_old_same_installation_snapshot(sessions, tmp_path, surviving_business):
    snapshot = tmp_path / "before-delete.tar.gz"
    export_journal(snapshot)
    identifier = fictional_batch(sessions, "fictional-deleted-before-journal-loss")
    delete_batch(sessions, identifier)
    if surviving_business:
        fictional_batch(sessions, "fictional-surviving-business")
    journal_path().unlink()

    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)
    assert not journal_path().exists()


@pytest.mark.parametrize("surviving_witness", ["identity", "marker"])
def test_missing_journal_with_any_installation_witness_stays_closed(sessions, tmp_path, surviving_witness):
    snapshot = tmp_path / "journal-snapshot.tar.gz"
    export_journal(snapshot)
    journal_path().unlink()
    if surviving_witness == "identity":
        (Path(os.environ["COACH_DATA_DIR"]) / ".installation-id").unlink()
    else:
        (journal_path().parent / "installation.json").unlink()

    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)
    assert not journal_path().exists()


def test_pristine_empty_install_can_enroll_latest_external_journal(sessions, tmp_path, monkeypatch):
    identifier = fictional_batch(sessions, "fictional-transfer-deletion")
    delete_batch(sessions, identifier)
    original_installation = installation_id()
    snapshot = tmp_path / "latest-journal.tar.gz"
    export_journal(snapshot)
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "pristine-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "pristine-data"))

    import_journal(snapshot, sessions)

    assert installation_id() == original_installation
    assert deleted_ids() == {identifier}
    assert (Path(os.environ["COACH_DATA_DIR"]) / ".installation-id").read_text().strip() == original_installation
    for path in (journal_path(), journal_path().parent / "installation.json", Path(os.environ["COACH_DATA_DIR"]) / ".installation-id"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_business_without_installation_witness_cannot_enroll_journal(sessions, tmp_path, monkeypatch):
    snapshot = tmp_path / "journal-snapshot.tar.gz"
    export_journal(snapshot)
    fictional_batch(sessions, "fictional-unowned-business")
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "unknown-journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "unknown-data"))
    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)
    assert not journal_path().exists()


def rewrite_journal_archive(archive_path, transform):
    with tarfile.open(archive_path, "r:gz") as source:
        entries = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(archive_path, "w:gz") as target:
        for member, contents in entries:
            if member.name == "deletions.jsonl":
                member = transform(member)
            target.addfile(member, BytesIO(contents) if member.isfile() else None)


@pytest.mark.parametrize("name", ["../deletions.jsonl", "/deletions.jsonl", "data/deletions.jsonl", "./deletions.jsonl"])
def test_journal_archive_rejects_noncanonical_paths_without_mutation(sessions, tmp_path, name):
    snapshot = tmp_path / "journal-snapshot.tar.gz"
    export_journal(snapshot)
    before = journal_path().read_bytes()
    def rename(member):
        member.name = name
        return member
    rewrite_journal_archive(snapshot, rename)
    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)
    assert journal_path().read_bytes() == before


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_journal_archive_rejects_nonregular_members_without_mutation(sessions, tmp_path, member_type):
    snapshot = tmp_path / "journal-snapshot.tar.gz"
    export_journal(snapshot)
    before = journal_path().read_bytes()
    def change_type(member):
        member.type = member_type
        member.size = 0
        member.linkname = "installation.json"
        return member
    rewrite_journal_archive(snapshot, change_type)
    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)
    assert journal_path().read_bytes() == before


def test_journal_archive_duplicate_member_is_rejected(sessions, tmp_path):
    snapshot = tmp_path / "journal-snapshot.tar.gz"
    export_journal(snapshot)
    with tarfile.open(snapshot, "r:gz") as source:
        contents = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(snapshot, "w:gz") as target:
        for member, value in contents + [contents[0]]:
            target.addfile(member, BytesIO(value))
    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)


def test_journal_checksum_failure_preserves_all_current_files(sessions, tmp_path):
    snapshot = tmp_path / "journal-snapshot.tar.gz"
    export_journal(snapshot)
    protected = [journal_path(), journal_path().parent / "installation.json", Path(os.environ["COACH_DATA_DIR"]) / ".installation-id"]
    before = {path: path.read_bytes() for path in protected}
    with tarfile.open(snapshot, "r:gz") as source:
        entries = [(member.name, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(snapshot, "w:gz") as target:
        for name, value in entries:
            if name == "deletions.jsonl":
                value = (json.dumps({"version": 1, "batch_id": "a" * 32}) + "\n").encode()
            member = tarfile.TarInfo(name)
            member.size = len(value)
            target.addfile(member, BytesIO(value))
    with pytest.raises(JournalUnavailable):
        import_journal(snapshot, sessions)
    assert {path: path.read_bytes() for path in protected} == before
