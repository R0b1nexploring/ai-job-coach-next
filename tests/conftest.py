"""All tests use independent deletion intents, never the developer's data."""
import pytest


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path / "data"))
    from app.privacy import initialize_journal
    initialize_journal(bootstrap=True)
