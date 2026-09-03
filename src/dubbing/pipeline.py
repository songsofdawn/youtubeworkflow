from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..stage3.subtitle_writer import read_srt
from ..stage3.dubbing_script import (
    canonical_text_from_payload,
    load_canonical_script,
    normalize_script_text,
    script_text_hash,
)
from ..stage4.input_resolver import resolve_source_video
from ..stage4.media_probe import probe_media
from ..stage4.stage4_manifest import (
    hash_json,
    sha256_file,
    utc_now,
)
from .config import resolve_model_path
from .demucs import DemucsSeparator, run_checked, valid_wav
from .manifest import ManifestSaveError, load_manifest, save_manifest, save_segment_metadata
from .loudness import normalize_loudness
from .mixer import build_chinese_voice_track, mix_background
from .runtime import (
    DubbingPreflightError,
    build_dubbing_subprocess_env,
    ensure_dubbing_runtime,
)
from .speech_timing import schedule_speech_regions, trim_wav_silence
from .timing import TimingPlan, adapt_segment, plan_duration, wav_duration
from .voxcpm import VoxCPM2Synthesizer


class DubbingError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class DubbingResult:
    status: str
    manifest_path: Path
    dubbed_audio_path: Path | None = None
    needs_review: bool = False
    warnings: list[str] = field(default_factory=list)


ProgressCallback = Callable[[str, int, int, int], None]


def _canonical_mode_enabled(video_dir: Path | str) -> bool:
    root = Path(video_dir).resolve()
    manifest_path = root / "stage3_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return bool(manifest.get("translation_for_dubbing"))


def select_chinese_subtitle(video_dir: Path | str) -> Path:
    root = Path(video_dir).resolve()
    priorities = ["subtitles/zh.reviewed.srt"]
    if _canonical_mode_enabled(root):
        priorities.append("subtitles/zh.dubbing.srt")
    priorities.append("subtitles/zh.clean.srt")
    for relative in priorities:
        candidate = root / relative
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    raise DubbingError(
        "CHINESE_DUBBING_SUBTITLE_NOT_FOUND",
        "中文配音只读取现有的 zh.dubbing.srt、zh.reviewed.srt 或 zh.clean.srt。"
        "当前任务没有这些字幕文件；请先完成中文字幕翻译阶段。",
    )


def subtitle_segments(path: Path | str) -> list[dict[str, Any]]:
    rows = [segment for segment in read_srt(path) if segment.text.strip()]
    return [
        {
            "index": index,
            "start": float(segment.start),
            "end": float(segment.end),
            "text": segment.text.strip(),
        }
        for index, segment in enumerate(rows, 1)
    ]


def canonical_dubbing_segments(video_dir: Path | str) -> list[dict[str, Any]]:
    """Load exact TTS text from canonical_zh.json when Stage3 V2 is present."""

    root = Path(video_dir).resolve()
    canonical_path = root / "stage3" / "translation" / "canonical_zh.json"
    if not _canonical_mode_enabled(root) or not canonical_path.is_file():
        return []
    payload = load_canonical_script(canonical_path)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(payload.get("utterances") or [], 1):
        text = str(row.get("zh_text") or "").strip()
        if not text:
            continue
        result.append(
            {
                "index": index,
                "start": float(row.get("start") or 0.0),
                "end": float(row.get("end") or 0.0),
                "text": text,
            }
        )
    return result


def reference_prompt_text(
    video_dir: Path | str,
    *,
    start: float,
    end: float,
) -> tuple[str, str]:
    """Return the English transcript corresponding to the reference audio window."""

    root = Path(video_dir).resolve()
    for relative in ("subtitles/en.dubbing.srt", "subtitles/en.selected.srt"):
        candidate = root / relative
        if not candidate.is_file():
            continue
        cues = read_srt(candidate)
        contained = [
            cue.text.strip()
            for cue in cues
            if cue.text.strip()
            and float(cue.start) >= start - 0.03
            and float(cue.end) <= end + 0.03
        ]
        # Prompt text must describe the prompt waveform exactly enough for
        # continuation conditioning.  Do not guess with partially-overlapping
        # cues (common with a manually cropped reference); reference-only
        # conditioning is safer than pairing audio with the wrong transcript.
        if contained:
            return " ".join(contained), relative
    return "", ""


def validate_canonical_dubbing_script(
    video_dir: Path | str,
    subtitle_path: Path | str,
) -> dict[str, Any]:
    """Ensure the TTS input text is the same text Stage 3 declared canonical."""

    root = Path(video_dir).resolve()
    canonical_path = root / "stage3" / "translation" / "canonical_zh.json"
    if not _canonical_mode_enabled(root) or not canonical_path.is_file():
        return {
            "enabled": False,
            "status": "LEGACY_SUBTITLE_SOURCE",
            "canonical_path": "",
        }
    try:
        payload = load_canonical_script(canonical_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DubbingError(
            "CANONICAL_DUBBING_SCRIPT_INVALID",
            f"中配单一脚本无法读取：{canonical_path}",
            details={"error": str(exc)},
        ) from exc

    subtitle_rows = read_srt(subtitle_path)
    canonical_rows = list(payload.get("utterances") or [])
    canonical_text = canonical_text_from_payload(payload)
    subtitle_text = "".join(item.text for item in subtitle_rows)
    mismatch_ids: list[int] = []
    timeline_mismatch_ids: list[int] = []
    for index, row in enumerate(canonical_rows, 1):
        if index > len(subtitle_rows):
            mismatch_ids.append(index)
            continue
        cue = subtitle_rows[index - 1]
        if normalize_script_text(cue.text) != normalize_script_text(str(row.get("zh_text") or "")):
            mismatch_ids.append(index)
        if (
            abs(float(cue.start) - float(row.get("start") or 0.0)) > 0.002
            or abs(float(cue.end) - float(row.get("end") or 0.0)) > 0.002
        ):
            timeline_mismatch_ids.append(index)

    full_match = normalize_script_text(subtitle_text) == normalize_script_text(canonical_text)
    if len(subtitle_rows) != len(canonical_rows):
        full_match = False
    if not full_match or mismatch_ids or timeline_mismatch_ids:
        raise DubbingError(
            "DUB_TEXT_SUBTITLE_MISMATCH",
            "中文字幕与中配 canonical_zh.json 不一致。为避免观众看到的文字和听到的文字不同，已停止配音。",
            details={
                "subtitle_path": str(Path(subtitle_path).resolve()),
                "canonical_path": str(canonical_path),
                "subtitle_count": len(subtitle_rows),
                "canonical_count": len(canonical_rows),
                "text_mismatch_ids": mismatch_ids,
                "timeline_mismatch_ids": timeline_mismatch_ids,
                "subtitle_text_hash": script_text_hash(subtitle_text),
                "canonical_text_hash": script_text_hash(canonical_text),
            },
        )
    return {
        "enabled": True,
        "status": "PASSED",
        "canonical_path": str(canonical_path),
        "canonical_hash": sha256_file(canonical_path),
        "canonical_text_hash": script_text_hash(canonical_text),
        "utterance_count": len(canonical_rows),
    }


def choose_reference_window(
    segments: list[dict[str, Any]],
    *,
    media_duration: float,
    settings: dict[str, Any],
) -> tuple[float, float]:
    target = float(settings.get("duration_seconds") or 8.0)
    minimum = float(settings.get("minimum_seconds") or 5.0)
    maximum = float(settings.get("maximum_seconds") or 10.0)
    skip_intro = float(settings.get("skip_intro_seconds") or 3.0)
    maximum_gap = float(settings.get("maximum_continuity_gap_seconds") or 0.6)
    target = max(minimum, min(target, maximum))
    usable = [row for row in segments if float(row["end"]) > skip_intro]
    candidates: list[tuple[float, float, float]] = []
    for offset, row in enumerate(usable):
        start = max(skip_intro, float(row["start"]))
        end = float(row["end"])
        duration = end - start
        if minimum <= duration <= maximum:
            candidates.append((abs(duration - target), start, min(end, media_duration)))
        for following in usable[offset + 1 :]:
            if float(following["start"]) - end > maximum_gap:
                break
            proposed_end = max(end, float(following["end"]))
            proposed_duration = proposed_end - start
            if proposed_duration > maximum:
                break
            end = proposed_end
            if proposed_duration >= minimum:
                candidates.append(
                    (
                        abs(proposed_duration - target),
                        start,
                        min(end, media_duration),
                    )
                )
    candidates = [item for item in candidates if item[2] - item[1] >= min(minimum, media_duration)]
    if candidates:
        _, start, end = min(candidates, key=lambda item: (item[0], item[1]))
        return round(start, 3), round(end, 3)
    start = max(skip_intro, float(usable[0]["start"]) if usable else skip_intro)
    if media_duration > 0 and start >= media_duration:
        start = max(0.0, media_duration - target)
    end = min(media_duration, start + target) if media_duration > 0 else start + target
    if end - start < 1.0:
        raise DubbingError(
            "REFERENCE_WINDOW_NOT_FOUND",
            "无法从有效视频时长中选择 reference.wav 片段。",
        )
    return round(start, 3), round(end, 3)


class DubbingPipeline:
    def __init__(
        self,
        project_root: Path | str,
        config: dict[str, Any],
        *,
        ffmpeg_path: Path | str | None = None,
        ffprobe_path: Path | str | None = None,
        python_executable: Path | str | None = None,
        progress: ProgressCallback | None = None,
        synthesizer_factory: Callable[..., Any] = VoxCPM2Synthesizer,
        separator_factory: Callable[..., Any] = DemucsSeparator,
        command_runner: Callable[..., None] = run_checked,
        runtime_preflight: Callable[[Path, Path], Any] = ensure_dubbing_runtime,
        loudness_normalizer: Callable[..., dict[str, Any]] = normalize_loudness,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = dict(config)
        self.ffmpeg_path = Path(
            ffmpeg_path or self.project_root / "tools" / "bin" / "ffmpeg.exe"
        ).resolve()
        self.ffprobe_path = Path(
            ffprobe_path or self.project_root / "tools" / "bin" / "ffprobe.exe"
        ).resolve()
        self.python_executable = Path(python_executable or os.sys.executable).resolve()
        self.progress = progress
        self.synthesizer_factory = synthesizer_factory
        self.separator_factory = separator_factory
        self.command_runner = command_runner
        self.runtime_preflight = runtime_preflight
        self.loudness_normalizer = loudness_normalizer
        self.subprocess_env = build_dubbing_subprocess_env(self.project_root)

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, Any]:
        return load_manifest(path)

    def _save_manifest(self, path: Path, manifest: dict[str, Any]) -> None:
        save_manifest(path, manifest, log=self._log)

    def _run_command(
        self,
        command: list[Path | str],
        *,
        cwd: Path | str | None = None,
        log: Callable[[str], None] | None = None,
        **_: Any,
    ) -> None:
        self.command_runner(
            command,
            cwd=cwd,
            log=log,
            env=self.subprocess_env,
        )

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        return path.resolve().relative_to(root.resolve()).as_posix()

    def _notify(self, step: str, progress: int, current: int = 0, total: int = 0) -> None:
        value = max(0, min(int(progress), 100))
        if self.progress:
            self.progress(step, current, total, value)
        marker = {
            "step": step,
            "current": int(current),
            "total": int(total),
            "progress": value,
        }
        print("[DUBBING_PROGRESS] " + json.dumps(marker, ensure_ascii=False), flush=True)

    @staticmethod
    def _log(message: str) -> None:
        print(message, flush=True)

    def _crop_reference(
        self,
        vocals: Path,
        reference: Path,
        *,
        start: float,
        end: float,
    ) -> None:
        temporary = reference.with_name(f".{reference.stem}-{os.getpid()}.tmp.wav")
        temporary.unlink(missing_ok=True)
        try:
            self._run_command(
                [
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-i",
                    vocals,
                    "-t",
                    f"{end - start:.3f}",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    temporary,
                ],
                cwd=reference.parent,
                log=self._log,
            )
            if not valid_wav(temporary):
                raise DubbingError(
                    "REFERENCE_AUDIO_FAILED",
                    "reference.wav 生成失败或输出为空。",
                )
            os.replace(temporary, reference)
        finally:
            temporary.unlink(missing_ok=True)

    def _prepare_reference(
        self,
        manifest: dict[str, Any],
        work_dir: Path,
        vocals: Path,
        segments: list[dict[str, Any]],
        media_duration: float,
        *,
        mode: str,
        manual_start: float | None,
        manual_end: float | None,
        force: bool,
    ) -> tuple[Path, dict[str, Any]]:
        reference = work_dir / "reference.wav"
        reference_settings = dict(self.config.get("reference") or {})
        normalized_mode = str(mode or "auto").strip().casefold()
        if normalized_mode == "manual":
            if manual_start is None or manual_end is None:
                raise DubbingError(
                    "REFERENCE_RANGE_REQUIRED",
                    "手动参考声音模式必须同时提供开始时间和结束时间。",
                )
            start, end = float(manual_start), float(manual_end)
            if start < 0 or end <= start or end > media_duration + 0.05:
                raise DubbingError(
                    "REFERENCE_RANGE_INVALID",
                    "手动 reference.wav 时间范围无效或超出视频时长。",
                    details={"start": start, "end": end, "duration": media_duration},
                )
        elif normalized_mode == "auto":
            start, end = choose_reference_window(
                segments,
                media_duration=media_duration,
                settings=reference_settings,
            )
        else:
            raise DubbingError("REFERENCE_MODE_INVALID", f"不支持的参考声音模式：{mode}")
        use_prompt_transcript = bool(reference_settings.get("use_prompt_transcript", True))
        prompt_text, prompt_source = (
            reference_prompt_text(work_dir.parent, start=start, end=end)
            if use_prompt_transcript
            else ("", "")
        )
        prompt_text_hash = script_text_hash(prompt_text) if prompt_text else ""
        vocals_hash = sha256_file(vocals)
        fingerprint = hash_json(
            {
                "version": 2,
                "vocals_hash": vocals_hash,
                "mode": normalized_mode,
                "start": round(start, 3),
                "end": round(end, 3),
                "prompt_text_hash": prompt_text_hash,
            }
        )
        previous = manifest.get("reference") if isinstance(manifest.get("reference"), dict) else {}
        reusable = (
            not force
            and valid_wav(reference)
            and previous.get("fingerprint") == fingerprint
            and previous.get("output_hash") == sha256_file(reference)
        )
        if not reusable:
            self._crop_reference(vocals, reference, start=start, end=end)
        record = {
            "mode": normalized_mode,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "path": "reference.wav",
            "fingerprint": fingerprint,
            "output_hash": sha256_file(reference),
            "reused": reusable,
            "prompt_transcript_enabled": use_prompt_transcript,
            "prompt_text": prompt_text,
            "prompt_text_hash": prompt_text_hash,
            "prompt_source": prompt_source,
        }
        self._log(f"[DUBBING] Reference audio: {start:.1f}s - {end:.1f}s")
        if use_prompt_transcript:
            if prompt_text:
                self._log(
                    "[DUBBING] Reference transcript attached for VoxCPM2 prompt conditioning "
                    f"({prompt_source})."
                )
            else:
                self._log(
                    "[DUBBING] WARNING: reference.wav 没有找到对应英文文本；"
                    "本次将仅使用 reference voice conditioning。"
                )
        return reference, record

    def _check_disk(self, work_dir: Path) -> None:
        minimum_gb = max(0.0, float(self.config.get("minimum_free_gb") or 0))
        free = shutil.disk_usage(work_dir).free
        if free < minimum_gb * 1024**3:
            raise DubbingError(
                "INSUFFICIENT_DISK_SPACE",
                f"中配工作盘剩余空间不足：至少需要 {minimum_gb:g} GB。",
                details={"free_bytes": free},
            )

    @staticmethod
    def _duration_retry_candidates(
        rows: list[dict[str, Any]],
        *,
        timing_settings: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return only first-pass duration overflows eligible for one TTS retry."""

        if not bool(timing_settings.get("duration_retry_enabled", True)):
            return []
        try:
            max_times = int(timing_settings.get("duration_retry_max_times", 1))
        except (TypeError, ValueError):
            max_times = 1
        if max_times < 1:
            return []
        candidates: list[dict[str, Any]] = []
        for row in rows:
            timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
            reasons = set(timing.get("reasons") or row.get("schedule_reasons") or [])
            if "REGION_DURATION_OVERFLOW" not in reasons:
                continue
            retry = row.get("duration_retry")
            if isinstance(retry, dict) and retry.get("attempted"):
                continue
            candidates.append(row)
        return candidates

    @staticmethod
    def _selected_duration_retry_source(
        row: dict[str, Any],
        *,
        work_dir: Path,
        force: bool,
    ) -> tuple[Path, dict[str, Any]] | None:
        if force:
            return None
        retry = row.get("previous", {}).get("duration_retry")
        if not isinstance(retry, dict) or not retry.get("selected"):
            return None
        relative = str(retry.get("candidate_wav") or "").strip()
        candidate = work_dir / relative
        expected_hash = str(retry.get("candidate_wav_hash") or "")
        if (
            not relative
            or not candidate.is_file()
            or not valid_wav(candidate)
            or not expected_hash
            or sha256_file(candidate) != expected_hash
        ):
            return None
        return candidate, dict(retry)

    def _retry_overlong_segments(
        self,
        rows: list[dict[str, Any]],
        *,
        synthesizer: Any,
        reference: Path,
        work_dir: Path,
        timing_settings: dict[str, Any],
        checkpoint: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Retry each overlong segment once and keep it only when it is shorter.

        VoxCPM2 does not currently expose a target-duration argument. The target
        is therefore used as an acceptance budget: a second same-text synthesis
        is selected only if its silence-trimmed speech is shorter. The normal
        bounded speed adaptation remains the final timing control.
        """

        candidates = self._duration_retry_candidates(
            rows,
            timing_settings=timing_settings,
        )
        if not candidates:
            return rows, 0, 0

        retry_root = work_dir / "segments" / "duration_retry"
        retry_root.mkdir(parents=True, exist_ok=True)
        retry_trim_root = retry_root / "trimmed"
        retry_trim_root.mkdir(parents=True, exist_ok=True)
        trim_enabled = bool(timing_settings.get("trim_silence_enabled", True))
        threshold_db = float(timing_settings.get("silence_threshold_db", -45.0))
        relative_db = float(timing_settings.get("silence_relative_db", -35.0))
        padding_ms = float(timing_settings.get("silence_padding_ms", 40.0))
        max_stretch = max(
            1.0, float(timing_settings.get("max_stretch_ratio", 1.30))
        )
        updated = [dict(row) for row in rows]
        selected_count = 0
        attempted_count = 0
        candidate_indexes = {int(row["index"]) for row in candidates}
        for offset, row in enumerate(updated, 1):
            if int(row["index"]) not in candidate_indexes:
                continue
            index = int(row["index"])
            timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
            natural_duration = max(
                0.05,
                float(row.get("spoken_duration") or row.get("generated_duration") or 0.0),
            )
            required_speed = max(
                1.0,
                float(
                    timing.get(
                        "required_speed_factor",
                        row.get("schedule_required_speed", max_stretch),
                    )
                ),
            )
            target_duration = max(0.05, natural_duration * max_stretch / required_speed)
            retry_path = retry_root / f"{index:06d}.wav"
            retry_trimmed_path = retry_trim_root / f"{index:06d}.wav"
            fingerprint = hash_json(
                {
                    "version": 1,
                    "input_hash": row.get("input_hash", ""),
                    "target_duration": round(target_duration, 3),
                    "max_stretch_ratio": round(max_stretch, 4),
                    "trim_enabled": trim_enabled,
                    "silence_threshold_db": threshold_db,
                    "silence_relative_db": relative_db,
                    "silence_padding_ms": padding_ms,
                }
            )
            self._notify(
                "超时片段重生成",
                79,
                offset,
                len(candidates),
            )
            attempted_count += 1
            record: dict[str, Any] = {
                "version": 1,
                "attempted": True,
                "attempts": 1,
                "fingerprint": fingerprint,
                "target_duration": round(target_duration, 3),
                "original_spoken_duration": round(natural_duration, 3),
                "completed_at": utc_now(),
                "selected": False,
            }
            try:
                synthesizer.generate(str(row["text"]), reference, retry_path)
                retry_raw_duration = wav_duration(retry_path)
                retry_trim = trim_wav_silence(
                    retry_path,
                    retry_trimmed_path,
                    enabled=trim_enabled,
                    threshold_db=threshold_db,
                    relative_db=relative_db,
                    padding_ms=padding_ms,
                )
                retry_spoken_duration = wav_duration(retry_trimmed_path)
                retry_hash = sha256_file(retry_path)
                selected = retry_spoken_duration + 0.01 < natural_duration
                record.update(
                    candidate_wav=self._relative(retry_path, work_dir),
                    candidate_wav_hash=retry_hash,
                    retry_generated_duration=round(retry_raw_duration, 3),
                    retry_spoken_duration=round(retry_spoken_duration, 3),
                    target_met=retry_spoken_duration <= target_duration + 0.02,
                    selected=selected,
                    reason=(
                        "shorter"
                        if selected and retry_spoken_duration > target_duration + 0.02
                        else "target_met"
                        if selected
                        else "not_shorter"
                    ),
                )
                if selected:
                    row.update(
                        wav=record["candidate_wav"],
                        wav_hash=retry_hash,
                        generated_duration=retry_raw_duration,
                        duration_retry=record,
                    )
                    selected_count += 1
                else:
                    row["duration_retry"] = record
                self._log(
                    "[DUBBING] Duration retry segment "
                    f"{index}: target={target_duration:.2f}s, "
                    f"retry={retry_spoken_duration:.2f}s, "
                    f"{'selected' if selected else 'kept original'}"
                )
            except Exception as exc:
                record.update(status="FAILED", error=str(exc))
                row["duration_retry"] = record
                self._log(
                    f"[DUBBING] WARNING: duration retry for segment {index} failed; "
                    f"keeping original audio ({exc})"
                )
            if checkpoint is not None:
                checkpoint(updated)
        return updated, selected_count, attempted_count

    def _apply_regional_timing(
        self,
        rows: list[dict[str, Any]],
        *,
        previous_segments: dict[int, dict[str, Any]],
        work_dir: Path,
        media_duration: float,
        timing_settings: dict[str, Any],
        force: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        trimmed_dir = work_dir / "segments" / "trimmed"
        scheduled_dir = work_dir / "segments" / "scheduled"
        trimmed_dir.mkdir(parents=True, exist_ok=True)
        scheduled_dir.mkdir(parents=True, exist_ok=True)
        trim_enabled = bool(timing_settings.get("trim_silence_enabled", True))
        threshold_db = float(timing_settings.get("silence_threshold_db", -45.0))
        relative_db = float(timing_settings.get("silence_relative_db", -35.0))
        padding_ms = float(timing_settings.get("silence_padding_ms", 40.0))
        prepared: list[dict[str, Any]] = []
        total_removed = 0.0

        self._notify("裁剪配音首尾静音", 75, 0, len(rows))
        for offset, row in enumerate(rows, 1):
            index = int(row["index"])
            raw_path = work_dir / str(row["wav"])
            trimmed_path = trimmed_dir / f"{index:06d}.wav"
            trim_fingerprint = hash_json(
                {
                    "version": 1,
                    "wav_hash": row["wav_hash"],
                    "enabled": trim_enabled,
                    "threshold_db": threshold_db,
                    "relative_db": relative_db,
                    "padding_ms": padding_ms,
                }
            )
            previous = previous_segments.get(index, {})
            trim_reused = (
                not force
                and previous.get("trim_fingerprint") == trim_fingerprint
                and valid_wav(trimmed_path)
                and previous.get("trimmed_wav_hash") == sha256_file(trimmed_path)
            )
            if trim_reused:
                trim_record = (
                    dict(previous.get("silence_trim"))
                    if isinstance(previous.get("silence_trim"), dict)
                    else {}
                )
            else:
                trim_record = trim_wav_silence(
                    raw_path,
                    trimmed_path,
                    enabled=trim_enabled,
                    threshold_db=threshold_db,
                    relative_db=relative_db,
                    padding_ms=padding_ms,
                ).to_dict()
            spoken_duration = wav_duration(trimmed_path)
            source_duration = float(row.get("generated_duration") or spoken_duration)
            total_removed += max(0.0, source_duration - spoken_duration)
            item = dict(row)
            item.update(
                trim_fingerprint=trim_fingerprint,
                trimmed_wav=self._relative(trimmed_path, work_dir),
                trimmed_wav_hash=sha256_file(trimmed_path),
                spoken_duration=spoken_duration,
                silence_trim=trim_record,
                trim_reused=trim_reused,
            )
            prepared.append(item)
            self._notify("裁剪配音首尾静音", 75, offset, len(rows))

        self._notify("连续语音区域调度", 78, 0, len(prepared))
        scheduled, timing_qc = schedule_speech_regions(
            prepared,
            media_duration=media_duration,
            region_max_gap=float(timing_settings.get("region_max_gap_ms", 500.0))
            / 1000,
            internal_gap=float(timing_settings.get("region_internal_gap_ms", 40.0))
            / 1000,
            boundary_gap=float(timing_settings.get("region_boundary_gap_ms", 50.0))
            / 1000,
            max_extension=float(timing_settings.get("max_extension_ms", 1000.0))
            / 1000,
            max_stretch_ratio=float(timing_settings.get("max_stretch_ratio", 1.30)),
            max_alignment_shift=float(
                timing_settings.get("max_alignment_shift_ms", 1500.0)
            )
            / 1000,
            overlap_tolerance=float(
                timing_settings.get("overlap_tolerance_ms", 20.0)
            )
            / 1000,
        )
        tolerance = float(timing_settings.get("overlap_tolerance_ms", 20.0)) / 1000
        final_rows: list[dict[str, Any]] = []
        for offset, item in enumerate(scheduled, 1):
            index = int(item["index"])
            trimmed_path = work_dir / str(item["trimmed_wav"])
            speed = max(1.0, float(item["schedule_speed_factor"]))
            scheduled_path = scheduled_dir / f"{index:06d}.wav"
            schedule_fingerprint = hash_json(
                {
                    "version": 1,
                    "trimmed_wav_hash": item["trimmed_wav_hash"],
                    "speed_factor": round(speed, 8),
                    "scheduled_start": round(float(item["scheduled_start"]), 6),
                }
            )
            previous = previous_segments.get(index, {})
            scheduled_reused = (
                speed > 1.000001
                and not force
                and previous.get("schedule_fingerprint") == schedule_fingerprint
                and valid_wav(scheduled_path)
                and previous.get("final_wav_hash") == sha256_file(scheduled_path)
            )
            if speed > 1.000001:
                plan = TimingPlan(
                    start=float(item["scheduled_start"]),
                    subtitle_end=float(item["end"]),
                    available_end=float(item["scheduled_start"])
                    + float(item["spoken_duration"]) / speed,
                    available_duration=float(item["spoken_duration"]) / speed,
                    generated_duration=float(item["spoken_duration"]),
                    ratio=speed,
                    speed_factor=speed,
                    final_duration=float(item["spoken_duration"]) / speed,
                    needs_review=bool(item["schedule_needs_review"]),
                    reason=(
                        "region_review"
                        if item["schedule_needs_review"]
                        else "region_scheduled"
                    ),
                )
                final_path = (
                    scheduled_path
                    if scheduled_reused
                    else adapt_segment(
                        trimmed_path,
                        scheduled_path,
                        plan,
                        ffmpeg_path=self.ffmpeg_path,
                        log=self._log,
                        command_runner=self._run_command,
                    )
                )
            else:
                final_path = trimmed_path
                scheduled_reused = bool(item.get("trim_reused"))
            actual_duration = wav_duration(final_path)
            result = dict(item)
            result.update(
                status="done",
                original_start=float(item["start"]),
                timing={
                    "reason": (
                        "region_review"
                        if item["schedule_needs_review"]
                        else "region_scheduled"
                    ),
                    "region": int(item["schedule_region"]),
                    "generated_duration": float(item["generated_duration"]),
                    "spoken_duration": float(item["spoken_duration"]),
                    "required_speed_factor": float(item["schedule_required_speed"]),
                    "speed_factor": speed,
                    "scheduled_start": float(item["scheduled_start"]),
                    "schedule_shift": float(item["schedule_shift"]),
                    "needs_review": bool(item["schedule_needs_review"]),
                    "reasons": list(item["schedule_reasons"]),
                },
                timing_fingerprint=schedule_fingerprint,
                schedule_fingerprint=schedule_fingerprint,
                final_wav=self._relative(final_path, work_dir),
                final_wav_hash=sha256_file(final_path),
                final_duration=actual_duration,
                needs_review=bool(item["schedule_needs_review"]),
                overlap=False,
                adapted_reused=scheduled_reused,
            )
            final_rows.append(result)

        warning_rows: list[str] = []
        for index, row in enumerate(final_rows):
            end = float(row["scheduled_start"]) + float(row["final_duration"])
            actual_overlap = bool(
                index + 1 < len(final_rows)
                and end
                > float(final_rows[index + 1]["scheduled_start"]) + tolerance
            )
            media_overflow = end > media_duration + tolerance
            reasons = list(row.get("schedule_reasons") or [])
            if actual_overlap:
                reasons.append("ACTUAL_VOICE_OVERLAP")
            if media_overflow:
                reasons.append("MEDIA_END_OVERFLOW")
            if not bool((row.get("silence_trim") or {}).get("speech_detected", True)):
                reasons.append("NO_SPEECH_DETECTED")
            row["overlap"] = actual_overlap
            row["needs_review"] = bool(reasons)
            row["timing"]["needs_review"] = bool(reasons)
            row["timing"]["reasons"] = sorted(set(reasons))

        review_rows = [row for row in final_rows if row["needs_review"]]
        review_regions = sorted({int(row["schedule_region"]) for row in review_rows})
        for region in review_regions:
            reasons = sorted(
                {
                    reason
                    for row in review_rows
                    if int(row["schedule_region"]) == region
                    for reason in row["timing"]["reasons"]
                }
            )
            message = (
                f"Region {region} requires fallback/review: {', '.join(reasons)}."
            )
            warning_rows.append(message)
            self._log(f"[DUBBING] WARNING: {message}")

        timing_qc.update(
            status="REVIEW_REQUIRED" if review_rows else "PASS_AUTO_ADAPTED",
            review_region_count=len(review_regions),
            review_segment_count=len(review_rows),
            no_voice_overlap=not any(bool(row["overlap"]) for row in final_rows),
            total_trimmed_silence_seconds=round(total_removed, 3),
            trim_enabled=trim_enabled,
        )
        return final_rows, timing_qc, warning_rows

    def run(
        self,
        video_dir: Path | str,
        *,
        reference_mode: str = "auto",
        reference_start: float | None = None,
        reference_end: float | None = None,
        force_separation: bool = False,
        force_tts: bool = False,
    ) -> DubbingResult:
        total_started = time.monotonic()
        root = Path(video_dir).resolve()
        if not root.is_dir():
            raise DubbingError("VIDEO_DIR_NOT_FOUND", f"视频任务目录不存在：{root}")
        try:
            self.runtime_preflight(self.project_root, self.python_executable)
        except DubbingPreflightError as exc:
            if exc.details:
                self._log(
                    "[DUBBING] Preflight detail: "
                    + json.dumps(exc.details, ensure_ascii=False)
                )
            raise DubbingError(exc.code, exc.message, details=exc.details) from exc
        if not self.ffmpeg_path.is_file():
            raise DubbingError("FFMPEG_NOT_FOUND", f"FFmpeg 不存在：{self.ffmpeg_path}")
        if not self.ffprobe_path.is_file():
            raise DubbingError("FFPROBE_NOT_FOUND", f"FFprobe 不存在：{self.ffprobe_path}")
        work_dir = root / "dubbing"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "segments" / "adapted").mkdir(parents=True, exist_ok=True)
        metadata_dir = work_dir / "segments" / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = work_dir / "manifest.json"
        loaded_manifest = self._load_manifest(manifest_path)
        existing_segments = loaded_manifest.get("segments")
        if not isinstance(existing_segments, list):
            existing_segments = []
        previous_segments: dict[int, dict[str, Any]] = {}
        for item in existing_segments:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index") or 0)
            except (TypeError, ValueError):
                continue
            if index > 0:
                previous_segments[index] = item
        manifest = {
            "version": 1,
            "tts_backend": "voxcpm2",
            "status": "RUNNING",
            "started_at": loaded_manifest.get("started_at") or utc_now(),
            "updated_at": utc_now(),
            "warnings": (
                list(loaded_manifest.get("warnings") or [])
                if isinstance(loaded_manifest.get("warnings"), list)
                else []
            ),
            "errors": (
                list(loaded_manifest.get("errors") or [])
                if isinstance(loaded_manifest.get("errors"), list)
                else []
            ),
            "error_history": (
                list(loaded_manifest.get("error_history") or [])
                if isinstance(loaded_manifest.get("error_history"), list)
                else []
            ),
            "reference": (
                loaded_manifest.get("reference")
                if isinstance(loaded_manifest.get("reference"), dict)
                else {}
            ),
            "checkpoints": (
                loaded_manifest.get("checkpoints")
                if isinstance(loaded_manifest.get("checkpoints"), dict)
                else {}
            ),
            "audio_qc": (
                loaded_manifest.get("audio_qc")
                if isinstance(loaded_manifest.get("audio_qc"), dict)
                else {}
            ),
            "performance": {
                "model_load_seconds": 0.0,
                "model_reused": False,
                "tts_total_seconds": 0.0,
                "tts_segment_count": 0,
                "tts_cached_segment_count": 0,
                "tts_average_segment_seconds": 0.0,
                "demucs_seconds": 0.0,
                "mix_seconds": 0.0,
                "loudness_seconds": 0.0,
                "total_dubbing_seconds": 0.0,
            },
            "segments": list(previous_segments.values()),
        }
        performance = manifest["performance"]
        self._save_manifest(manifest_path, manifest)

        try:
            self._notify("准备音频", 2)
            self._check_disk(work_dir)
            subtitle_path = select_chinese_subtitle(root)
            script_consistency = validate_canonical_dubbing_script(root, subtitle_path)
            segments = (
                canonical_dubbing_segments(root)
                if script_consistency.get("enabled")
                else subtitle_segments(subtitle_path)
            )
            if not segments:
                raise DubbingError(
                    "CHINESE_DUBBING_SUBTITLE_EMPTY",
                    f"中文字幕为空或没有有效条目：{subtitle_path}",
                )
            source_video, source_reason, _ = resolve_source_video(root)
            source_probe = probe_media(self.ffprobe_path, source_video)
            media_duration = float(source_probe.get("duration") or 0)
            if media_duration <= 0:
                raise DubbingError("MEDIA_DURATION_INVALID", "无法读取原视频有效时长。")
            if int(source_probe.get("audio_stream_count") or 0) < 1:
                raise DubbingError("SOURCE_AUDIO_NOT_FOUND", "原视频没有可用于中配的人声音轨。")

            manifest.update(
                {
                    "source_video_path": str(source_video),
                    "source_video_selection_reason": source_reason,
                    "source_video_hash": sha256_file(source_video),
                    "subtitle_path": str(subtitle_path),
                    "subtitle_hash": sha256_file(subtitle_path),
                    "script_consistency": script_consistency,
                    "media_duration": media_duration,
                }
            )
            self._save_manifest(manifest_path, manifest)

            self._notify("分离人声", 8)
            separator = self.separator_factory(
                ffmpeg_path=self.ffmpeg_path,
                python_executable=self.python_executable,
                config=dict(self.config.get("demucs") or {}),
                log=self._log,
                command_runner=self.command_runner,
                subprocess_env=self.subprocess_env,
            )
            demucs_started = time.monotonic()
            separation = separator.prepare(
                source_video,
                work_dir,
                force=force_separation,
            )
            performance["demucs_seconds"] = round(
                time.monotonic() - demucs_started, 3
            )
            manifest["separation"] = {
                "reused": bool(separation.get("reused")),
                "checkpoint": separation.get("checkpoint") or {},
            }
            self._save_manifest(manifest_path, manifest)

            self._notify("准备参考声音", 24)
            reference, reference_record = self._prepare_reference(
                manifest,
                work_dir,
                Path(separation["vocals"]),
                segments,
                media_duration,
                mode=reference_mode,
                manual_start=reference_start,
                manual_end=reference_end,
                force=force_separation,
            )
            manifest["reference"] = reference_record
            self._save_manifest(manifest_path, manifest)

            model_path = resolve_model_path(self.project_root, self.config)
            tts_settings = dict(self.config.get("tts") or {})
            timing_settings = dict(self.config.get("timing") or {})
            regional_timing_enabled = bool(
                timing_settings.get("regional_scheduling_enabled", False)
            )
            reference_hash = sha256_file(reference)
            model_files = [
                path
                for path in [
                    *model_path.glob("*.safetensors"),
                    model_path / "pytorch_model.bin",
                    model_path / "audiovae.pth",
                ]
                if path.is_file()
            ]
            model_identifier = hash_json(
                {
                    "path": str(model_path),
                    "config_mtime": (
                        (model_path / "config.json").stat().st_mtime_ns
                        if (model_path / "config.json").is_file()
                        else 0
                    ),
                    "weights": [
                        [path.name, path.stat().st_size, path.stat().st_mtime_ns]
                        for path in sorted(model_files)
                    ],
                }
            )
            reference_prompt_hash = str(reference_record.get("prompt_text_hash") or "")
            tts_context_fingerprint = hash_json(
                {
                    "version": 2,
                    "subtitle_hash": manifest["subtitle_hash"],
                    "reference_hash": reference_hash,
                    "reference_prompt_hash": reference_prompt_hash,
                    "model": model_identifier,
                    "tts": tts_settings,
                }
            )
            manifest["tts_context"] = {
                "fingerprint": tts_context_fingerprint,
                "model": model_identifier,
                "reference_hash": reference_hash,
                "reference_prompt_hash": reference_prompt_hash,
                "prompt_conditioning": bool(reference_record.get("prompt_text")),
            }
            self._save_manifest(manifest_path, manifest)

            candidates: list[dict[str, Any]] = []
            pending_count = 0
            for row in segments:
                input_hash = hash_json(
                    {
                        "version": 1,
                        "text": row["text"],
                        "start": round(float(row["start"]), 3),
                        "end": round(float(row["end"]), 3),
                        "reference_hash": reference_hash,
                        "reference_prompt_hash": reference_prompt_hash,
                        "model": model_identifier,
                        "tts": tts_settings,
                    }
                )
                raw_path = work_dir / "segments" / f"{int(row['index']):06d}.wav"
                previous = previous_segments.get(int(row["index"]), {})
                candidates.append(
                    {
                        **row,
                        "input_hash": input_hash,
                        "raw_path": raw_path,
                        "metadata_path": metadata_dir / f"{int(row['index']):06d}.json",
                        "previous": previous,
                    }
                )

            loaded_reference = (
                loaded_manifest.get("reference")
                if isinstance(loaded_manifest.get("reference"), dict)
                else {}
            )
            loaded_context = (
                loaded_manifest.get("tts_context")
                if isinstance(loaded_manifest.get("tts_context"), dict)
                else {}
            )
            global_resume_match = bool(
                str(loaded_manifest.get("status") or "").upper()
                in {"RUNNING", "FAILED"}
                and loaded_manifest.get("subtitle_hash") == manifest["subtitle_hash"]
                and loaded_reference.get("output_hash") == reference_hash
            )
            context_witness = any(
                item["previous"].get("input_hash") == item["input_hash"]
                for item in candidates
                if item["previous"]
            )
            orphan_recovery_allowed = bool(
                global_resume_match
                and (
                    loaded_context.get("fingerprint") == tts_context_fingerprint
                    or context_witness
                )
            )

            prepared: list[dict[str, Any]] = []
            recovered_from_disk = 0
            for item in candidates:
                raw_path = Path(item["raw_path"])
                previous = item["previous"]
                metadata = load_manifest(item["metadata_path"])
                raw_valid = valid_wav(raw_path)
                raw_hash = sha256_file(raw_path) if raw_valid else ""
                raw_duration = wav_duration(raw_path) if raw_valid else 0.0
                manifest_reusable = (
                    not force_tts
                    and previous.get("input_hash") == item["input_hash"]
                    and raw_valid
                    and raw_duration > 0
                    and previous.get("wav_hash") == raw_hash
                )
                metadata_reusable = (
                    not force_tts
                    and metadata.get("input_hash") == item["input_hash"]
                    and raw_valid
                    and raw_duration > 0
                    and metadata.get("wav_hash") == raw_hash
                )
                orphan_reusable = (
                    not force_tts
                    and not previous
                    and not metadata
                    and orphan_recovery_allowed
                    and raw_valid
                    and raw_duration > 0
                )
                raw_reusable = bool(
                    manifest_reusable or metadata_reusable or orphan_reusable
                )
                if not raw_reusable:
                    pending_count += 1
                elif metadata_reusable:
                    item["recovered_from"] = "segment_metadata"
                elif orphan_reusable:
                    item["recovered_from"] = "validated_disk_scan"
                    recovered_from_disk += 1
                else:
                    item["recovered_from"] = "manifest"
                item.update(
                    raw_reusable=raw_reusable,
                    raw_hash=raw_hash,
                    raw_duration=raw_duration,
                )
                prepared.append(item)

            if recovered_from_disk:
                self._log(
                    "[DUBBING] Resume scan recovered "
                    f"{recovered_from_disk} valid segment WAV file(s) newer than manifest.json."
                )

            synthesizer: Any | None = None

            def ensure_synthesizer() -> Any:
                nonlocal synthesizer
                if synthesizer is not None:
                    return synthesizer
                self._notify("加载 VoxCPM2", 28)
                model_load_started = time.monotonic()
                synthesizer = self.synthesizer_factory(
                    model_path,
                    device=str(self.config.get("device") or "cuda"),
                    allow_cpu=bool(self.config.get("allow_cpu", False)),
                    settings=tts_settings,
                    log=self._log,
                )
                performance["model_load_seconds"] = round(
                    float(
                        getattr(
                            synthesizer,
                            "model_load_seconds",
                            time.monotonic() - model_load_started,
                        )
                    ),
                    3,
                )
                performance["model_reused"] = bool(
                    getattr(synthesizer, "model_reused", False)
                )
                prompt_setter = getattr(synthesizer, "set_reference_prompt_text", None)
                if callable(prompt_setter):
                    prompt_setter(str(reference_record.get("prompt_text") or ""))
                reset_peak = getattr(synthesizer, "reset_peak_vram_stats", None)
                if callable(reset_peak):
                    try:
                        reset_peak()
                    except Exception:
                        pass
                return synthesizer

            if pending_count:
                ensure_synthesizer()
            else:
                self._log("[DUBBING] All segment TTS caches are valid; VoxCPM2 load skipped.")

            performance["tts_segment_count"] = int(pending_count)
            performance["tts_cached_segment_count"] = int(
                len(prepared) - pending_count
            )

            completed_rows: list[dict[str, Any]] = []
            warnings: list[str] = []
            try:
                total = len(prepared)
                for offset, item in enumerate(prepared, 1):
                    index = int(item["index"])
                    raw_path = Path(item["raw_path"])
                    progress = 30 + round(offset / max(1, total) * 43)
                    self._notify(f"中文配音：{offset} / {total}", progress, offset, total)
                    self._log(f"[DUBBING] TTS {offset}/{total}")
                    entry = {
                        "index": index,
                        "start": float(item["start"]),
                        "end": float(item["end"]),
                        "text": str(item["text"]),
                        "input_hash": str(item["input_hash"]),
                        "wav": self._relative(raw_path, work_dir),
                        "status": "pending",
                    }
                    previous_retry = item["previous"].get("duration_retry")
                    if (
                        not force_tts
                        and isinstance(previous_retry, dict)
                        and previous_retry.get("attempted")
                    ):
                        entry["duration_retry"] = dict(previous_retry)
                    if not item["raw_reusable"]:
                        assert synthesizer is not None
                        try:
                            segment_started = time.monotonic()
                            synthesizer.generate(str(item["text"]), reference, raw_path)
                            performance["tts_total_seconds"] = round(
                                float(performance["tts_total_seconds"])
                                + time.monotonic()
                                - segment_started,
                                3,
                            )
                            performance["tts_average_segment_seconds"] = round(
                                float(performance["tts_total_seconds"])
                                / max(1, int(performance["tts_segment_count"])),
                                3,
                            )
                        except Exception as exc:
                            entry.update(status="failed", error=str(exc))
                            current = [*completed_rows, entry]
                            manifest.update(
                                status="FAILED",
                                updated_at=utc_now(),
                                segments=current,
                            )
                            self._save_manifest(manifest_path, manifest)
                            raise DubbingError(
                                "TTS_SEGMENT_FAILED",
                                f"第 {index} 句中文配音生成失败；已保存前面完成的片段，重试会从此句继续。",
                                details={"index": index, "error": str(exc)},
                            ) from exc
                        generated_duration = wav_duration(raw_path)
                        wav_hash = sha256_file(raw_path)
                        segment_metadata = {
                            "version": 1,
                            "index": index,
                            "input_hash": str(item["input_hash"]),
                            "wav": self._relative(raw_path, work_dir),
                            "wav_hash": wav_hash,
                            "generated_duration": generated_duration,
                            "completed_at": utc_now(),
                        }
                        save_segment_metadata(item["metadata_path"], segment_metadata)
                        entry.update(
                            status="generated",
                            wav_hash=wav_hash,
                            generated_duration=generated_duration,
                            generated_at=utc_now(),
                        )
                        manifest.update(
                            updated_at=utc_now(),
                            segments=[*completed_rows, entry],
                        )
                        self._save_manifest(manifest_path, manifest)
                    else:
                        if item.get("recovered_from") == "validated_disk_scan":
                            save_segment_metadata(
                                item["metadata_path"],
                                {
                                    "version": 1,
                                    "index": index,
                                    "input_hash": str(item["input_hash"]),
                                    "wav": self._relative(raw_path, work_dir),
                                    "wav_hash": str(item["raw_hash"]),
                                    "generated_duration": float(item["raw_duration"]),
                                    "completed_at": utc_now(),
                                    "recovered_from": "validated_disk_scan",
                                },
                            )
                        entry.update(
                            status="generated",
                            wav_hash=str(item["raw_hash"]),
                            generated_duration=float(item["raw_duration"]),
                            reused=True,
                            recovered_from=str(item.get("recovered_from") or "manifest"),
                        )

                    retry_source = self._selected_duration_retry_source(
                        item,
                        work_dir=work_dir,
                        force=force_tts,
                    )
                    if retry_source is not None:
                        retry_path, retry_record = retry_source
                        entry.update(
                            wav=self._relative(retry_path, work_dir),
                            wav_hash=str(retry_record["candidate_wav_hash"]),
                            generated_duration=float(
                                retry_record["retry_generated_duration"]
                            ),
                            duration_retry=retry_record,
                        )

                    if regional_timing_enabled:
                        effective_path = work_dir / str(entry["wav"])
                        entry.update(
                            status="generated",
                            final_wav=self._relative(effective_path, work_dir),
                            final_wav_hash=str(entry["wav_hash"]),
                            final_duration=float(entry["generated_duration"]),
                            needs_review=False,
                            overlap=False,
                        )
                        completed_rows.append(entry)
                        manifest.update(
                            updated_at=utc_now(),
                            segments=completed_rows,
                        )
                        self._save_manifest(manifest_path, manifest)
                        continue

                    self._notify(f"时长处理：{offset} / {total}", progress, offset, total)

                    next_start = (
                        float(prepared[offset]["start"]) if offset < total else None
                    )
                    plan = plan_duration(
                        start=float(item["start"]),
                        end=float(item["end"]),
                        next_start=next_start,
                        media_duration=media_duration,
                        generated_duration=float(entry["generated_duration"]),
                        min_gap=float(timing_settings.get("min_gap_ms", 200)) / 1000,
                        max_extension=float(
                            timing_settings.get("max_extension_ms", 1000)
                        )
                        / 1000,
                        direct_accept_ratio=float(
                            timing_settings.get("direct_accept_ratio", 1.10)
                        ),
                        max_stretch_ratio=float(
                            timing_settings.get("max_stretch_ratio", 1.30)
                        ),
                    )
                    adapted_path = work_dir / "segments" / "adapted" / f"{index:06d}.wav"
                    timing_fingerprint = hash_json(
                        {
                            "wav_hash": entry["wav_hash"],
                            "speed_factor": round(plan.speed_factor, 8),
                        }
                    )
                    previous = item["previous"]
                    adapted_reusable = (
                        plan.speed_factor > 1.000001
                        and not force_tts
                        and previous.get("timing_fingerprint") == timing_fingerprint
                        and valid_wav(adapted_path)
                        and previous.get("final_wav_hash") == sha256_file(adapted_path)
                    )
                    if plan.speed_factor > 1.000001:
                        final_path = (
                            adapted_path
                            if adapted_reusable
                            else adapt_segment(
                                raw_path,
                                adapted_path,
                                plan,
                                ffmpeg_path=self.ffmpeg_path,
                                log=self._log,
                        command_runner=self._run_command,
                            )
                        )
                    else:
                        final_path = raw_path
                    actual_final_duration = wav_duration(final_path)
                    overlap = bool(
                        next_start is not None
                        and float(item["start"]) + actual_final_duration
                        > next_start - float(timing_settings.get("min_gap_ms", 200)) / 1000
                    )
                    needs_review = bool(plan.needs_review or overlap)
                    if needs_review:
                        warning = (
                            f"Segment {index} exceeds target duration significantly; marked for review."
                        )
                        warnings.append(warning)
                        self._log(f"[DUBBING] WARNING: {warning}")
                    entry.update(
                        status="done",
                        timing=plan.to_dict(),
                        timing_fingerprint=timing_fingerprint,
                        final_wav=self._relative(final_path, work_dir),
                        final_wav_hash=sha256_file(final_path),
                        final_duration=actual_final_duration,
                        needs_review=needs_review,
                        overlap=overlap,
                        adapted_reused=adapted_reusable,
                    )
                    completed_rows.append(entry)
                    manifest.update(
                        updated_at=utc_now(),
                        segments=completed_rows,
                        warnings=sorted(set(list(manifest.get("warnings") or []) + warnings)),
                    )
                    self._save_manifest(manifest_path, manifest)
            finally:
                if synthesizer is not None:
                    peak_vram = getattr(synthesizer, "peak_vram_mb", None)
                    if peak_vram is not None:
                        try:
                            performance["peak_vram_mb"] = round(float(peak_vram), 1)
                        except (TypeError, ValueError):
                            pass
                    close = getattr(synthesizer, "close", None)
                    if callable(close):
                        close()
                    synthesizer = None

            if regional_timing_enabled:
                self._notify("准备连续语音区域调度", 74, 0, len(completed_rows))
                completed_rows, timing_qc, timing_warnings = self._apply_regional_timing(
                    completed_rows,
                    previous_segments=previous_segments,
                    work_dir=work_dir,
                    media_duration=media_duration,
                    timing_settings=timing_settings,
                    force=force_tts,
                )
                retry_candidates = self._duration_retry_candidates(
                    completed_rows,
                    timing_settings=timing_settings,
                )
                retry_selected = 0
                retry_attempted = 0
                if retry_candidates:
                    try:
                        ensure_synthesizer()
                    except (DubbingError, OSError, RuntimeError) as exc:
                        self._log(
                            "[DUBBING] WARNING: cannot load VoxCPM2 for duration retry; "
                            f"keeping the original audio for review ({exc})"
                        )
                    else:
                        def save_duration_retry_checkpoint(
                            retry_rows: list[dict[str, Any]],
                        ) -> None:
                            manifest.update(
                                updated_at=utc_now(),
                                segments=retry_rows,
                            )
                            self._save_manifest(manifest_path, manifest)

                        try:
                            (
                                completed_rows,
                                retry_selected,
                                retry_attempted,
                            ) = self._retry_overlong_segments(
                                completed_rows,
                                synthesizer=synthesizer,
                                reference=reference,
                                work_dir=work_dir,
                                timing_settings=timing_settings,
                                checkpoint=save_duration_retry_checkpoint,
                            )
                        finally:
                            if synthesizer is not None:
                                close = getattr(synthesizer, "close", None)
                                if callable(close):
                                    close()
                                synthesizer = None
                if retry_attempted:
                    manifest.update(
                        updated_at=utc_now(),
                        segments=completed_rows,
                    )
                    self._save_manifest(manifest_path, manifest)
                if retry_selected:
                    self._notify(
                        "重新调度超时片段",
                        79,
                        retry_selected,
                        len(retry_candidates),
                    )
                    completed_rows, timing_qc, timing_warnings = self._apply_regional_timing(
                        completed_rows,
                        previous_segments=previous_segments,
                        work_dir=work_dir,
                        media_duration=media_duration,
                        timing_settings=timing_settings,
                        force=force_tts,
                    )
                warnings = timing_warnings
                preserved_warnings = [
                    value
                    for value in list(manifest.get("warnings") or [])
                    if "exceeds target duration significantly" not in str(value)
                    and "requires fallback/review" not in str(value)
                ]
                manifest.update(
                    updated_at=utc_now(),
                    segments=completed_rows,
                    timing_qc=timing_qc,
                    warnings=sorted(set(preserved_warnings + warnings)),
                )
                self._save_manifest(manifest_path, manifest)

            self._notify("按字幕时间轴拼接", 80)
            self._log("[DUBBING] Building Chinese voice track...")
            voice_path = work_dir / "chinese_voice.wav"
            timeline_fingerprint = hash_json(
                {
                    "version": 2,
                    "media_duration": round(media_duration, 3),
                    "segments": [
                        [
                            row["index"],
                            row.get("scheduled_start", row["start"]),
                            row["final_wav_hash"],
                        ]
                        for row in completed_rows
                    ],
                }
            )
            checkpoints = manifest.setdefault("checkpoints", {})
            timeline_checkpoint = (
                checkpoints.get("timeline")
                if isinstance(checkpoints.get("timeline"), dict)
                else {}
            )
            timeline_reused = (
                not force_tts
                and valid_wav(voice_path)
                and timeline_checkpoint.get("fingerprint") == timeline_fingerprint
                and timeline_checkpoint.get("output_hash") == sha256_file(voice_path)
            )
            if not timeline_reused:
                build_chinese_voice_track(
                    [
                        {
                            **row,
                            "final_wav": work_dir / str(row["final_wav"]),
                        }
                        for row in completed_rows
                    ],
                    voice_path,
                    work_dir=work_dir,
                    media_duration=media_duration,
                    ffmpeg_path=self.ffmpeg_path,
                    sample_rate=int(
                        (self.config.get("mix") or {}).get("sample_rate", 48000)
                    ),
                    log=self._log,
                    command_runner=self._run_command,
                )
            checkpoints["timeline"] = {
                "fingerprint": timeline_fingerprint,
                "output_hash": sha256_file(voice_path),
                "path": "chinese_voice.wav",
                "reused": timeline_reused,
                "completed_at": utc_now(),
            }
            self._save_manifest(manifest_path, manifest)

            mix_settings = dict(self.config.get("mix") or {})
            sample_rate = int(mix_settings.get("sample_rate", 48000))
            loudness_settings = dict(self.config.get("loudness") or {})
            # Existing custom configs that predate this section keep their old
            # behavior; the project default config explicitly enables it.
            loudness_enabled = bool(loudness_settings.get("enabled", False))
            voice_target_lufs = float(
                loudness_settings.get("voice_target_lufs", -18.0)
            )
            voice_true_peak_db = float(
                loudness_settings.get("voice_true_peak_db", -2.0)
            )
            final_target_lufs = float(
                loudness_settings.get("final_target_lufs", -14.0)
            )
            final_true_peak_db = float(
                loudness_settings.get("final_true_peak_db", -1.0)
            )
            final_lra = float(loudness_settings.get("final_lra", 11.0))
            audio_qc = manifest.setdefault("audio_qc", {})

            normalized_voice_path = work_dir / "chinese_voice_normalized.wav"
            voice_loudness_fingerprint = hash_json(
                {
                    "version": 1,
                    "voice_hash": sha256_file(voice_path),
                    "enabled": loudness_enabled,
                    "target_lufs": voice_target_lufs,
                    "true_peak_db": voice_true_peak_db,
                    "lra": final_lra,
                    "sample_rate": sample_rate,
                }
            )
            voice_loudness_checkpoint = (
                checkpoints.get("voice_loudness")
                if isinstance(checkpoints.get("voice_loudness"), dict)
                else {}
            )
            if loudness_enabled:
                voice_for_mix = normalized_voice_path
                voice_loudness_reused = (
                    not force_tts
                    and valid_wav(voice_for_mix)
                    and voice_loudness_checkpoint.get("fingerprint")
                    == voice_loudness_fingerprint
                    and voice_loudness_checkpoint.get("output_hash")
                    == sha256_file(voice_for_mix)
                )
                if not voice_loudness_reused:
                    self._notify("统一中文人声响度", 86)
                    self._log(
                        f"[DUBBING] Normalizing Chinese voice to {voice_target_lufs:g} LUFS"
                    )
                    loudness_started = time.monotonic()
                    audio_qc["voice"] = self.loudness_normalizer(
                        voice_path,
                        voice_for_mix,
                        ffmpeg_path=self.ffmpeg_path,
                        target_lufs=voice_target_lufs,
                        true_peak_db=voice_true_peak_db,
                        lra=final_lra,
                        sample_rate=sample_rate,
                        log=self._log,
                        env=self.subprocess_env,
                    )
                    performance["loudness_seconds"] = round(
                        float(performance["loudness_seconds"])
                        + time.monotonic()
                        - loudness_started,
                        3,
                    )
            else:
                voice_for_mix = voice_path
                voice_loudness_reused = True
                audio_qc["voice"] = {"enabled": False}
            checkpoints["voice_loudness"] = {
                "fingerprint": voice_loudness_fingerprint,
                "output_hash": sha256_file(voice_for_mix),
                "path": self._relative(voice_for_mix, work_dir),
                "reused": voice_loudness_reused,
                "completed_at": utc_now(),
            }
            self._save_manifest(manifest_path, manifest)

            self._notify("混合背景音", 92)
            duck_db = float(mix_settings.get("background_duck_db", 6.0))
            duck_attack_ms = float(mix_settings.get("duck_attack_ms", 40.0))
            duck_release_ms = float(mix_settings.get("duck_release_ms", 250.0))
            self._log(
                f"[DUBBING] Applying background ducking: -{max(0.0, duck_db):g} dB"
                if duck_db > 0
                else "[DUBBING] Background ducking disabled."
            )
            self._log("[DUBBING] Mixing background audio...")
            mixed_path = work_dir / "mixed_audio.wav"
            dubbed_path = work_dir / "dubbed_audio.wav"
            mix_fingerprint = hash_json(
                {
                    "version": 2,
                    "background_hash": sha256_file(Path(separation["background"])),
                    "voice_hash": sha256_file(voice_for_mix),
                    "voice_loudness_fingerprint": voice_loudness_fingerprint,
                    "background_duck_db": duck_db,
                    "duck_attack_ms": duck_attack_ms,
                    "duck_release_ms": duck_release_ms,
                    "sample_rate": sample_rate,
                    "limiter": float(mix_settings.get("limiter", 0.95)),
                    "speech_intervals": [
                        [
                            int(row["index"]),
                            round(
                                float(row.get("scheduled_start", row["start"])),
                                6,
                            ),
                            round(float(row["final_duration"]), 6),
                        ]
                        for row in completed_rows
                    ],
                }
            )
            mix_checkpoint = (
                checkpoints.get("mix") if isinstance(checkpoints.get("mix"), dict) else {}
            )
            mix_reused = (
                not force_tts
                and valid_wav(mixed_path)
                and mix_checkpoint.get("fingerprint") == mix_fingerprint
                and mix_checkpoint.get("output_hash") == sha256_file(mixed_path)
            )
            if not mix_reused:
                mix_started = time.monotonic()
                mix_background(
                    Path(separation["background"]),
                    voice_for_mix,
                    mixed_path,
                    ffmpeg_path=self.ffmpeg_path,
                    duck_db=duck_db,
                    speech_intervals=completed_rows,
                    attack_ms=duck_attack_ms,
                    release_ms=duck_release_ms,
                    sample_rate=sample_rate,
                    limiter=float(mix_settings.get("limiter", 0.95)),
                    media_duration=media_duration,
                    log=self._log,
                    command_runner=self._run_command,
                )
                performance["mix_seconds"] = round(
                    time.monotonic() - mix_started, 3
                )
            checkpoints["mix"] = {
                "fingerprint": mix_fingerprint,
                "output_hash": sha256_file(mixed_path),
                "path": "mixed_audio.wav",
                "reused": mix_reused,
                "completed_at": utc_now(),
            }

            final_loudness_fingerprint = hash_json(
                {
                    "version": 1,
                    "mixed_audio_hash": sha256_file(mixed_path),
                    "mix_fingerprint": mix_fingerprint,
                    "enabled": loudness_enabled,
                    "target_lufs": final_target_lufs,
                    "true_peak_db": final_true_peak_db,
                    "lra": final_lra,
                    "sample_rate": sample_rate,
                }
            )
            final_loudness_checkpoint = (
                checkpoints.get("final_loudness")
                if isinstance(checkpoints.get("final_loudness"), dict)
                else {}
            )
            final_loudness_reused = (
                not force_tts
                and valid_wav(dubbed_path)
                and final_loudness_checkpoint.get("fingerprint")
                == final_loudness_fingerprint
                and final_loudness_checkpoint.get("output_hash")
                == sha256_file(dubbed_path)
            )
            if not final_loudness_reused:
                if loudness_enabled:
                    self._notify("统一最终成片响度", 96)
                    self._log(
                        "[DUBBING] Final loudness target: "
                        f"{final_target_lufs:g} LUFS / {final_true_peak_db:g} dBTP"
                    )
                    loudness_started = time.monotonic()
                    audio_qc["final_mix"] = self.loudness_normalizer(
                        mixed_path,
                        dubbed_path,
                        ffmpeg_path=self.ffmpeg_path,
                        target_lufs=final_target_lufs,
                        true_peak_db=final_true_peak_db,
                        lra=final_lra,
                        sample_rate=sample_rate,
                        log=self._log,
                        env=self.subprocess_env,
                    )
                    performance["loudness_seconds"] = round(
                        float(performance["loudness_seconds"])
                        + time.monotonic()
                        - loudness_started,
                        3,
                    )
                else:
                    temporary_copy = dubbed_path.with_name(
                        f".{dubbed_path.stem}-{os.getpid()}.tmp.wav"
                    )
                    temporary_copy.unlink(missing_ok=True)
                    try:
                        shutil.copy2(mixed_path, temporary_copy)
                        os.replace(temporary_copy, dubbed_path)
                    finally:
                        temporary_copy.unlink(missing_ok=True)
                    audio_qc["final_mix"] = {"enabled": False}
            checkpoints["final_loudness"] = {
                "fingerprint": final_loudness_fingerprint,
                "output_hash": sha256_file(dubbed_path),
                "path": "dubbed_audio.wav",
                "reused": final_loudness_reused,
                "completed_at": utc_now(),
            }
            needs_review = any(bool(row.get("needs_review")) for row in completed_rows)
            status = "COMPLETED_WITH_REVIEW" if needs_review else "COMPLETED"
            prior_errors = list(manifest.get("errors") or [])
            manifest.update(
                {
                    "status": status,
                    "needs_review": needs_review,
                    "segment_count": len(completed_rows),
                    "chinese_voice_path": str(voice_path),
                    "chinese_voice_normalized_path": str(voice_for_mix),
                    "mixed_audio_path": str(mixed_path),
                    "dubbed_audio_path": str(dubbed_path),
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                    "segments": completed_rows,
                    "warnings": sorted(
                        set(list(manifest.get("warnings") or []) + warnings)
                    ),
                    "errors": [],
                    "error_history": (
                        list(manifest.get("error_history") or []) + prior_errors
                    )[-20:],
                }
            )
            performance["total_dubbing_seconds"] = round(
                time.monotonic() - total_started, 3
            )
            self._save_manifest(manifest_path, manifest)
            self._notify("中文配音完成", 100, len(completed_rows), len(completed_rows))
            self._log("[DUBBING] Completed.")
            return DubbingResult(
                status=status,
                manifest_path=manifest_path,
                dubbed_audio_path=dubbed_path,
                needs_review=needs_review,
                warnings=list(manifest["warnings"]),
            )
        except ManifestSaveError as exc:
            raise DubbingError(
                "DUBBING_MANIFEST_SAVE_FAILED",
                str(exc),
                details={"path": str(exc.path), "attempts": exc.attempts},
            ) from exc
        except DubbingError as exc:
            performance["total_dubbing_seconds"] = round(
                time.monotonic() - total_started, 3
            )
            manifest.update(status="FAILED", updated_at=utc_now())
            errors = list(manifest.get("errors") or [])
            errors.append(exc.to_dict())
            manifest["errors"] = errors[-20:]
            try:
                self._save_manifest(manifest_path, manifest)
            except ManifestSaveError as save_exc:
                raise DubbingError(
                    "DUBBING_MANIFEST_SAVE_FAILED",
                    str(save_exc),
                    details={
                        "path": str(save_exc.path),
                        "attempts": save_exc.attempts,
                    },
                ) from save_exc
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            wrapped = DubbingError("DUBBING_FAILED", str(exc))
            performance["total_dubbing_seconds"] = round(
                time.monotonic() - total_started, 3
            )
            manifest.update(status="FAILED", updated_at=utc_now())
            errors = list(manifest.get("errors") or [])
            errors.append(wrapped.to_dict())
            manifest["errors"] = errors[-20:]
            try:
                self._save_manifest(manifest_path, manifest)
            except ManifestSaveError as save_exc:
                raise DubbingError(
                    "DUBBING_MANIFEST_SAVE_FAILED",
                    str(save_exc),
                    details={
                        "path": str(save_exc.path),
                        "attempts": save_exc.attempts,
                    },
                ) from save_exc
            raise wrapped from exc


__all__ = [
    "DubbingError",
    "DubbingPipeline",
    "DubbingResult",
    "choose_reference_window",
    "select_chinese_subtitle",
    "canonical_dubbing_segments",
    "validate_canonical_dubbing_script",
    "subtitle_segments",
]
