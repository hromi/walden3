from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class Link(BaseModel):
    rel: str
    ref: str
    note: str | None = None

class BaseRevision(BaseModel):
    schema_: str = Field(alias="schema")
    version: int
    created_at: str = Field(default_factory=now_iso)
    previous: str | None = None
    links: list[Link] = Field(default_factory=list)
    model_config = {"populate_by_name": True}

class PersonRevision(BaseRevision):
    schema_: Literal["walden/person-memory/1"] = Field("walden/person-memory/1", alias="schema")
    person_ref: str
    memory: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

class RoomRevision(BaseRevision):
    schema_: Literal["walden/room-memory/1"] = Field("walden/room-memory/1", alias="schema")
    room_ref: str
    memory: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

class ParticipantRef(BaseModel):
    person: str
    memory_at_time: str | None = None
    role: str = "participant"

class EpisodeRevision(BaseRevision):
    schema_: Literal["walden/episode/1"] = Field("walden/episode/1", alias="schema")
    version: int = 1
    previous: str | None = None
    episode_ref: str | None = None
    when: dict[str, Any]
    where: dict[str, Any] = Field(default_factory=dict)
    what: dict[str, Any] = Field(default_factory=dict)
    who: list[ParticipantRef] = Field(default_factory=list)
    room: str | None = None
    room_memory_at_time: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    raw_transcript: list[dict[str, Any]] | None = None

Revision = PersonRevision | RoomRevision | EpisodeRevision

class EntityHead(BaseModel):
    object_id: str
    kind: Literal["person", "room"]
    key_hash: str
    head_blob_id: str
    revision: int
    updated_at: str = Field(default_factory=now_iso)

class MatrixEventRecord(BaseModel):
    event_id: str
    room_id: str
    sender: str
    timestamp_ms: int
    body: str
    role: Literal["human", "assistant", "system"] = "human"
    sender_name: str = ""  # Matrix display name; never the raw user ID

class Consolidation(BaseModel):
    episode_summary: str
    episode_topics: list[str] = Field(default_factory=list)
    # Fact operations (see core/facts.py). "about" is a Matrix user ID, "walden" or "room";
    # "said_by" is a Matrix user ID or "walden". Resolved from model aliases by the Consolidator.
    operations: list[dict[str, Any]] = Field(default_factory=list)
