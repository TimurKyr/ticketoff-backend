import uuid

from app.modules.identity.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_roundtrip() -> None:
    hashed = hash_password("secret-pass-123")
    assert hashed != "secret-pass-123"
    assert verify_password("secret-pass-123", hashed)
    assert not verify_password("wrong", hashed)


def test_token_roundtrip() -> None:
    user_id = uuid.uuid4()
    assert decode_access_token(create_access_token(user_id)) == user_id


def test_garbage_token_returns_none() -> None:
    assert decode_access_token("not-a-token") is None
