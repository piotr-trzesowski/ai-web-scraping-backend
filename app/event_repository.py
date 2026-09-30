import hashlib
import json
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.event_models import EventSource, StoredEvent, utc_now
from app.event_schemas import EventData


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def event_fingerprint(data: EventData) -> str:
    values = [
        " ".join(value.casefold().split())
        for value in (data.title, data.venue, data.city)
    ]
    values.append(data.starts_at.astimezone(timezone.utc).isoformat())
    return digest(json.dumps(values))


def save_event(
    session: Session, data: EventData, source_name: str, source_key: str
) -> tuple[StoredEvent, bool]:
    fingerprint = event_fingerprint(data)
    identity = digest(source_key)
    source = session.scalar(
        select(EventSource).where(
            EventSource.source_name == source_name, EventSource.source_key == identity
        )
    )
    matching = session.scalar(
        select(StoredEvent).where(StoredEvent.fingerprint == fingerprint)
    )
    created = source is None and matching is None
    if source is not None:
        event = source.event
        if matching is not None and matching.id != event.id:
            # A changed source now describes an already stored event.
            # Preserve the original ID rather than silently merging public records.
            raise ValueError("Source update conflicts with another stored event")
    else:
        event = matching if matching is not None else StoredEvent()
        if matching is None:
            session.add(event)
        source = EventSource(source_name=source_name, source_key=identity, event=event)
        session.add(source)

    values = data.model_dump(mode="json")
    values["starts_at"] = data.starts_at.astimezone(timezone.utc)
    # A listing without a poster should not erase a poster from an event page.
    if (
        values["image_url"] is None
        and event.image_url is not None
        and event.source_url != values["source_url"]
    ):
        values["image_url"] = event.image_url
        values["source_url"] = event.source_url
    for name, value in values.items():
        setattr(event, name, value)
    event.fingerprint = fingerprint
    source.last_seen_at = utc_now()
    session.flush()
    return event, created
