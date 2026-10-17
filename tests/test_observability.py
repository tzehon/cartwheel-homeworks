import pytest
from fastapi.testclient import TestClient

from server import app as server_app


@pytest.fixture
def client(world, tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "_SESSIONS", {})
    monkeypatch.setattr(
        server_app, "SESSIONS_DB", tmp_path / "sessions.db"
    )

    def unexpected_agent(*args, **kwargs):
        pytest.fail("Rejected requests must not reach the agent")

    monkeypatch.setattr(server_app, "build_agent", unexpected_agent)
    return TestClient(server_app.app)


def test_session_rejects_role_mismatch(client):
    response = client.post(
        "/sessions",
        json={"user_id": 1, "role": "support"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "role does not match user"
    assert server_app._SESSIONS == {}


def test_token_cannot_authorize_another_session(client):
    first = client.post(
        "/sessions", json={"user_id": 1, "role": "shopper"}
    )
    second = client.post(
        "/sessions", json={"user_id": 1, "role": "shopper"}
    )
    assert first.status_code == second.status_code == 200

    response = client.post(
        f"/sessions/{second.json()['session_id']}/messages",
        headers={"Authorization": f"Bearer {first.json()['token']}"},
        json={"message": "Show my recent orders."},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "token is for another session"
