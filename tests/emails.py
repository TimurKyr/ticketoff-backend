"""A provider for tests: records every email instead of sending it."""

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.modules.notifications import service as notifications
from app.modules.notifications.providers import EmailMessage


class RecordingEmailProvider:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to == address]


def confirm_email(api: TestClient, address: str) -> None:
    """Open the verification link emailed to `address` at registration, as the user would.

    Buying tickets and publishing an event need a confirmed email, so API tests that
    register a user through /auth/register call this right after. The email comes from the
    recording provider the autouse `emails` fixture installs.
    """
    recorder = notifications.get_provider()
    assert isinstance(recorder, RecordingEmailProvider)
    [link] = [w for m in recorder.to(address) for w in m.text.split() if "/verify-email?" in w]
    [token] = parse_qs(urlparse(link).query)["token"]
    response = api.post("/auth/verify-email", json={"token": token})
    assert response.status_code == 200, response.text
