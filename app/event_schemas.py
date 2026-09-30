from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl


class EventData(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(min_length=1)
    artist: str | None = None
    venue: str = Field(min_length=1)
    city: str = Field(min_length=1)
    starts_at: AwareDatetime
    genres: list[str] = Field(default_factory=list)
    ticket_url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    source_url: HttpUrl


class Event(EventData):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class EventPage(BaseModel):
    items: list[Event]
    total: int
    limit: int
    offset: int
