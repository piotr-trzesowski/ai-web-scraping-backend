from datetime import timezone
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.event_models import EventSource, StoredEvent
from app.event_schemas import Event, EventPage
from app.timeframe import window

app = FastAPI(title="Events API", version="0.1.0")
Database = Annotated[Session, Depends(get_session)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/events", response_model=EventPage)
def list_events(
    session: Database,
    months: Annotated[int, Query(ge=1, le=3)] = 3,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventPage:
    start, end = window(months)
    filters = (
        StoredEvent.starts_at >= start.astimezone(timezone.utc),
        StoredEvent.starts_at < end.astimezone(timezone.utc),
        StoredEvent.sources.any(EventSource.source_name == "web-search"),
    )
    total = session.scalar(select(func.count()).select_from(StoredEvent).where(*filters))
    rows = session.scalars(
        select(StoredEvent)
        .where(*filters)
        .order_by(StoredEvent.starts_at, StoredEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return EventPage(
        items=[event_response(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


def event_response(row: StoredEvent) -> Event:
    values = {name: getattr(row, name) for name in Event.model_fields}
    # SQLite drops offsets; persisted dates are always normalized to UTC.
    if values["starts_at"].tzinfo is None:
        values["starts_at"] = values["starts_at"].replace(tzinfo=timezone.utc)
    return Event.model_validate(values)


@app.get("/api/v1/events/{event_id}", response_model=Event)
def get_event(event_id: UUID, session: Database) -> Event:
    event = session.get(StoredEvent, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    start = event.starts_at.replace(tzinfo=event.starts_at.tzinfo or timezone.utc)
    lower, upper = window(3)
    if (
        not lower.astimezone(timezone.utc) <= start < upper.astimezone(timezone.utc)
        or session.scalar(
            select(EventSource.id).where(
                EventSource.event_id == event_id,
                EventSource.source_name == "web-search",
            )
        ) is None
    ):
        raise HTTPException(404, "Event not found")
    return event_response(event)
