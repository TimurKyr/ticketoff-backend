import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.common.base import Base


class AuditLog(Base):
    """Append-only record of a significant action: who, what, on which entity, when.

    Never edited or deleted (ERD decision 6), so there are no update/delete endpoints.
    """

    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL for system actions (e.g. a background job releasing expired seat holds).
    actor_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), index=True)
    action_type = Column(String, nullable=False)  # e.g. "event.published"
    entity_type = Column(String, nullable=False)  # e.g. "event"
    entity_id = Column(UUID(as_uuid=True))
    details = Column(JSONB)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    actor = relationship("User")
    event = relationship("Event")
