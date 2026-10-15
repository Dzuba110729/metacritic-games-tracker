from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_healthz(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert (tmp_path / "test.db").exists()
