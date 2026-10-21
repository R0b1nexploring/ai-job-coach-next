"""Backup extraction cannot alter independent deletion state or file ownership."""
from io import BytesIO
import os
from pathlib import Path
import tarfile

import pytest

from app.privacy import initialize_journal, journal_path
from scripts.archive_tools import InvalidArchive, pack, restore_data, verify


def make_archive(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "database.dump").write_bytes(b"fictional database dump")
    data = tmp_path / "payload"
    data.mkdir()
    (data / "export.html").write_text("fictional export")
    archive = tmp_path / "backup.tar.gz"
    pack(staging, data, archive, "0003", "a" * 32)
    return archive


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_business_archive_rejects_nonregular_members_before_extraction(tmp_path, member_type):
    archive = make_archive(tmp_path)
    with tarfile.open(archive, "r:gz") as source:
        entries = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(archive, "w:gz") as target:
        for member, value in entries:
            if member.name == "data/export.html":
                member.type = member_type
                member.size = 0
                member.linkname = "database.dump"
            target.addfile(member, BytesIO(value) if member.isfile() else None)
    destination = tmp_path / "verified"
    with pytest.raises(InvalidArchive):
        verify(archive, destination)
    assert not destination.exists()


def test_extraction_ignores_archive_ownership_and_privileged_modes(tmp_path):
    archive = make_archive(tmp_path)
    with tarfile.open(archive, "r:gz") as source:
        entries = [(member, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(archive, "w:gz") as target:
        for member, value in entries:
            member.uid = member.gid = 65534
            member.uname = member.gname = "fictional-owner"
            member.mode = 0o6777
            target.addfile(member, BytesIO(value))
    destination = tmp_path / "verified"
    verify(archive, destination)
    for path in destination.rglob("*"):
        if path.is_file():
            assert path.stat().st_uid == os.geteuid()
            assert path.stat().st_mode & 0o7777 == 0o600


def test_relative_data_directory_excludes_independent_journal_subtree(tmp_path, monkeypatch):
    relative_root = tmp_path / "relative-case"
    relative_root.mkdir()
    monkeypatch.chdir(relative_root)
    monkeypatch.setenv("COACH_DATA_DIR", "./data")
    monkeypatch.setenv("COACH_JOURNAL_DIR", "./data/deletion-journal")
    initialize_journal(bootstrap=True)
    (Path("data") / "fictional-export.html").write_text("fictional export")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "database.dump").write_bytes(b"fictional database dump")
    archive = tmp_path / "backup.tar.gz"

    manifest = pack(staging, Path("data"), archive, "0003", "a" * 32)

    assert "data/fictional-export.html" in manifest["files"]
    assert not any(name.startswith("data/deletion-journal/") for name in manifest["files"])
    verify(archive)


@pytest.mark.parametrize("journal_relative", ["deletion-journal", "private/journal"])
def test_restore_cannot_copy_any_archived_path_over_live_journal(tmp_path, monkeypatch, journal_relative):
    data = tmp_path / "live-data"
    monkeypatch.setenv("COACH_DATA_DIR", str(data))
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(data / journal_relative))
    initialize_journal(bootstrap=True)
    before = journal_path().read_bytes()
    sentinel = data / "fictional-export.html"
    sentinel.write_text("current fictional export")
    source = tmp_path / "verified"
    malicious = source / "data" / journal_relative / "deletions.jsonl"
    malicious.parent.mkdir(parents=True)
    malicious.write_text("fictional stale journal contents")

    with pytest.raises(InvalidArchive):
        restore_data(source, data)

    assert journal_path().read_bytes() == before
    assert sentinel.read_text() == "current fictional export"
