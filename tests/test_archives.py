from io import BytesIO
import json
from pathlib import Path
import tarfile

import pytest

from scripts.archive_tools import InvalidArchive, pack, verify


def fixture_archive(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "database.dump").write_bytes(b"fictional-pg-dump")
    data = tmp_path / "payload"
    data.mkdir()
    (data / "export.html").write_text("fictional export")
    (data / ".worker-heartbeat.json").write_text("fictional heartbeat")
    archive = tmp_path / "backup.tar.gz"
    manifest = pack(staging, data, archive, "0003", "a" * 32)
    return archive, manifest


def rewrite(archive, replacement=None, extra=None):
    with tarfile.open(archive, "r:gz") as source:
        entries = [(member.name, source.extractfile(member).read()) for member in source.getmembers()]
    with tarfile.open(archive, "w:gz") as target:
        for name, content in entries:
            if replacement and name == replacement[0]:
                content = replacement[1]
            info = tarfile.TarInfo(name)
            info.size = len(content)
            target.addfile(info, BytesIO(content))
        if extra:
            info = tarfile.TarInfo(extra)
            info.size = 1
            target.addfile(info, BytesIO(b"x"))


def test_backup_inventory_is_complete_and_excludes_heartbeat(tmp_path):
    archive, manifest = fixture_archive(tmp_path)
    assert set(manifest["files"]) == {"database.dump", "data/export.html"}
    assert verify(archive, tmp_path / "verified") == manifest
    assert (tmp_path / "verified/data/export.html").read_text() == "fictional export"
    assert archive.stat().st_mode & 0o777 == 0o600


def test_checksum_mismatch_never_extracts_anything(tmp_path):
    archive, _ = fixture_archive(tmp_path)
    rewrite(archive, ("data/export.html", b"tampered"))
    destination = tmp_path / "verified"
    with pytest.raises(InvalidArchive):
        verify(archive, destination)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "data/../outside", "data/export.html", "extra-secret"])
def test_unsafe_duplicate_or_unlisted_paths_rejected(tmp_path, name):
    archive, _ = fixture_archive(tmp_path)
    rewrite(archive, extra=name)
    with pytest.raises(InvalidArchive):
        verify(archive)


def test_missing_inventory_member_rejected(tmp_path):
    archive, manifest = fixture_archive(tmp_path)
    manifest["files"]["data/missing.html"] = {"bytes": 1, "sha256": "0" * 64}
    rewrite(archive, ("manifest.json", json.dumps(manifest).encode()))
    with pytest.raises(InvalidArchive):
        verify(archive)


def test_backup_cannot_use_a_different_installation_journal(tmp_path):
    archive, _ = fixture_archive(tmp_path)
    destination = tmp_path / "verified"
    with pytest.raises(InvalidArchive):
        verify(archive, destination, "b" * 32)
    assert not destination.exists()
