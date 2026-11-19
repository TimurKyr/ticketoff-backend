"""A provider for tests: records every email instead of sending it."""

from app.modules.notifications.providers import EmailMessage


class RecordingEmailProvider:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to == address]
