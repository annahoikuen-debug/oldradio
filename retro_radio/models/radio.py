from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

class PlaylistItemType(str, Enum):
    TALK = "talk"
    SONG = "song"

@dataclass
class PlaylistItem:
    id: str
    type: PlaylistItemType
    title: str
    # For talk segments
    content: Optional[str] = None
    estimated_duration: Optional[float] = None
    audio_url: Optional[str] = None
    # For songs
    artist: Optional[str] = None
    preview_url: Optional[str] = None
    artwork_url: Optional[str] = None
    is_fallback: bool = False
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not isinstance(self.type, PlaylistItemType):
            try:
                self.type = PlaylistItemType(str(self.type).strip().lower())
            except ValueError:
                raise ValueError(f"Invalid playlist item type: {self.type!r}") from None
        if not self.id:
            raise ValueError("PlaylistItem.id must not be empty")
        if not self.title:
            raise ValueError("PlaylistItem.title must not be empty")
        if self.type is PlaylistItemType.TALK and self.content is None:
            raise ValueError("TALK playlist items require content")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "title": self.title,
            "content": self.content,
            "estimated_duration": self.estimated_duration,
            "audio_url": self.audio_url,
            "artist": self.artist,
            "preview_url": self.preview_url,
            "artwork_url": self.artwork_url,
            "is_fallback": self.is_fallback,
            "metadata": self.metadata or {}
        }

@dataclass
class ScriptSegment:
    id: str
    title: str
    content: str
    estimated_duration: float
    order: int
    metadata: Optional[Dict[str, Any]] = None
    source_language: str = "ja"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "estimated_duration": self.estimated_duration,
            "order": self.order,
            "metadata": self.metadata or {},
            "source_language": self.source_language
        }

@dataclass
class ProgramSchedule:
    id: str
    title: str
    start_time: str
    duration: int
    description: str
    is_historical: bool = False
    source: str = "modern"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_time": self.start_time,
            "duration": self.duration,
            "description": self.description,
            "is_historical": self.is_historical,
            "source": self.source
        }

@dataclass
class ProgramGuide:
    date: str
    weekday: str
    schedules: List[ProgramSchedule]
    today_highlight: str
    special_events: Optional[List[str]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "weekday": self.weekday,
            "schedules": [s.to_dict() for s in self.schedules],
            "today_highlight": self.today_highlight,
            "special_events": self.special_events or []
        }
