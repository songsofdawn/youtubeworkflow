from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class WordEvent:
    text: str
    start: float
    end: float
    first_seen_at: float
    source_cue_id: int
    probability: float | None = None
    source_segment_id: int | None = None

    def __post_init__(self) -> None:
        if self.source_segment_id is None:
            self.source_segment_id = self.source_cue_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RawCue:
    id: int
    start: float
    end: float
    text: str
    words: list[WordEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value


@dataclass
class SubtitleSegment:
    id: int
    start: float
    end: float
    text: str
    source_cue_ids: list[int] = field(default_factory=list)
    words: list[WordEvent] = field(default_factory=list)
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)
    source: str = ""
    source_segment_ids: list[int] = field(default_factory=list)
    qc_flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_segment_ids:
            self.source_segment_ids = list(self.source_cue_ids)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TranslationSegment:
    id: int
    start: float
    end: float
    source_text: str
    translation: str = ""
    translation_raw: str = ""
    qc_flags: list[str] = field(default_factory=list)
    estimated_tts_duration: float = 0.0
    duration_ratio: float = 0.0
    repaired: bool = False
    manually_reviewed: bool = False

    @property
    def source_duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_duration"] = self.source_duration
        value["chinese_char_count"] = sum(
            1 for character in self.translation if "\u3400" <= character <= "\u9fff"
        )
        value["needs_shortening"] = "TTS_TOO_LONG" in self.qc_flags
        return value
