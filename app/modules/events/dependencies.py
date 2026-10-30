import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.modules.events import repository
from app.modules.events.models import Event
from app.modules.identity.dependencies import SessionDep, require_organizer
from app.modules.identity.models import OrganizerProfile


def require_event_owner(
    event_id: uuid.UUID,
    organizer: Annotated[OrganizerProfile, Depends(require_organizer)],
    session: SessionDep,
) -> Event:
    """The event named in the path, if it belongs to the logged-in organizer.

    Someone else's event answers 404, not 403, so the API never reveals that it exists.
    """
    event = repository.get_event(session, event_id)
    if event is None or event.organizer_id != organizer.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event
