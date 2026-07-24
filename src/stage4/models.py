from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class Stage4Error(RuntimeError):
    """An expected stage-four failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class SubtitleCue:
    identifier: str
    start: float
    end: float
    text: str
    lines: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedInputs:
    video_dir: Path
    source_video: Path
    source_video_reason: str
    source_video_candidates: tuple[Path, ...]
    english_subtitle: Path
    chinese_subtitle: Path
    chinese_subtitle_reviewed: bool
    chinese_subtitle_auto_selected: bool = False
    chinese_subtitle_selection_reason: str = ""
    chinese_subtitle_selection_score: float | None = None
    chinese_selection_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_dir": str(self.video_dir),
            "source_video": str(self.source_video),
            "source_video_reason": self.source_video_reason,
            "source_video_candidates": [str(path) for path in self.source_video_candidates],
            "english_subtitle": str(self.english_subtitle),
            "chinese_subtitle": str(self.chinese_subtitle),
            "chinese_subtitle_reviewed": self.chinese_subtitle_reviewed,
            "chinese_subtitle_auto_selected": self.chinese_subtitle_auto_selected,
            "chinese_subtitle_selection_reason": self.chinese_subtitle_selection_reason,
            "chinese_subtitle_selection_score": self.chinese_subtitle_selection_score,
        }


@dataclass
class SubtitleValidation:
    english: list[SubtitleCue]
    chinese: list[SubtitleCue]
    report: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.report.get("validation_status") == "PASSED"


@dataclass
class CommandResult:
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    processing_seconds: float

    @property
    def success(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["success"] = self.success
        value["stderr_summary"] = self.stderr.strip()[-2000:]
        return value


@dataclass
class PipelineOptions:
    mode: str = "softsub"
    video_encoder: str = "auto"
    resume: bool = True
    force: bool = False
    force_ass: bool = False
    force_softsub: bool = False
    force_hardsub: bool = False
    strict_subtitle_layout: bool = False
    require_reviewed: bool = False
    require_audio_copy: bool = False
    keep_temp: bool = False
    dry_run: bool = False


@dataclass
class PipelineResult:
    status: str
    manifest_path: Path
    plan: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
