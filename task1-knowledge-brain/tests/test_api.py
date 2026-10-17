from pathlib import Path

from fastapi.testclient import TestClient

from kb import api


def test_health_and_status_on_empty_db(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(api.settings, "db_path", tmp_path / "kb.db")
    with TestClient(api.app) as client:
        assert client.get("/health").json() == {"status": "ok", "articles": 0}
        assert client.get("/ingestion/status").json() == {"pages_by_status": {}, "last_run": None}
