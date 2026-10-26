import uuid

from fastapi.testclient import TestClient


def _creds() -> tuple[str, str]:
    # unique email per test so reruns never collide with rows already in the DB
    return f"user-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"


def _register(client: TestClient, email: str, password: str):
    return client.post("/auth/register", json={"email": email, "password": password})


def _login(client: TestClient, email: str, password: str):
    return client.post("/auth/login", data={"username": email, "password": password})


def test_register_then_login(client: TestClient) -> None:
    email, password = _creds()
    response = _register(client, email, password)
    assert response.status_code == 201
    assert response.json()["email"] == email
    assert "password" not in response.json()

    login = _login(client, email, password)
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"


def test_duplicate_email_is_409_case_insensitive(client: TestClient) -> None:
    email, password = _creds()
    assert _register(client, email, password).status_code == 201
    assert _register(client, email.upper(), password).status_code == 409


def test_wrong_password_is_401(client: TestClient) -> None:
    email, password = _creds()
    _register(client, email, password)
    assert _login(client, email, "wrong-password-123").status_code == 401


def test_unknown_email_is_401(client: TestClient) -> None:
    assert _login(client, "nobody-here@example.com", "whatever-123").status_code == 401


def test_short_password_is_422(client: TestClient) -> None:
    response = _register(client, "short@example.com", "short")
    assert response.status_code == 422


def test_me_returns_current_user(client: TestClient) -> None:
    email, password = _creds()
    _register(client, email, password)
    token = _login(client, email, password).json()["access_token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == email


def test_me_without_token_is_401(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401


def test_me_with_garbage_token_is_401(client: TestClient) -> None:
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
