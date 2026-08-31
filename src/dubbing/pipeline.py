from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..stage3.subtitle_writer import read_srt
from ..stage4.input_resolver import resolve_source_video
from ..stage4.media_probe import probe_media
from ..stage4.stage4_manifest import (
    atomic_write_json,
    hash_json,
    sha256_file,
    utc_now,
)
from .config import resolve_model_path
from .demucs import DemucsSeparator, run_checked, valid_wav
from .mixer import build_chinese_voice_track, mix_background
from .timing import adapt_segment, plan_duration, wav_duration
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


def select_chinese_subtitle(video_dir: Path | str) -> Path:
    root = Path(video_dir).resolve()
    for relative in ("subtitles/zh.reviewed.srt", "subtitles/zh.clean.srt"):
        candidate = root / relative
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    raise DubbingError(
        "CHINESE_DUBBING_SUBTITLE_NOT_FOUND",
        "中文配音只读取现有的 zh.reviewed.srt 或 zh.clean.srt。"
        "当前任务两者都不存在；请先完成中文字幕翻译阶段。",
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
        for following in usable[offset + 1 :]:
            if float(following["start"]) - end > maximum_gap:
                break
            end = max(end, float(following["end"]))
            duration = end - start
            if duration >= minimum:
                clipped_end = min(end, start + target, start + maximum, media_duration)
                candidates.append((abs((clipped_end - start) - target), start, clipped_end))
                break
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

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

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
            self.command_runner(
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
        vocals_hash = sha256_file(vocals)
        fingerprint = hash_json(
            {
                "version": 1,
                "vocals_hash": vocals_hash,
                "mode": normalized_mode,
                "start": round(start, 3),
                "end": round(end, 3),
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
        }
        self._log(f"[DUBBING] Reference audio: {start:.1f}s - {end:.1f}s")
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
        root = Path(video_dir).resolve()
        if not root.is_dir():
            raise DubbingError("VIDEO_DIR_NOT_FOUND", f"视频任务目录不存在：{root}")
        if not self.ffmpeg_path.is_file():
            raise DubbingError("FFMPEG_NOT_FOUND", f"FFmpeg 不存在：{self.ffmpeg_path}")
        if not self.ffprobe_path.is_file():
            raise DubbingError("FFPROBE_NOT_FOUND", f"FFprobe 不存在：{self.ffprobe_path}")
        work_dir = root / "dubbing"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "segments" / "adapted").mkdir(parents=True, exist_ok=True)
        manifest_path = work_dir / "manifest.json"
        manifest = self._load_manifest(manifest_path)
        existing_segments = manifest.get("segments")
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
            "started_at": manifest.get("started_at") or utc_now(),
            "updated_at": utc_now(),
            "warnings": (
                list(manifest.get("warnings") or [])
                if isinstance(manifest.get("warnings"), list)
                else []
            ),
            "errors": (
                list(manifest.get("errors") or [])
                if isinstance(manifest.get("errors"), list)
                else []
            ),
            "error_history": (
                list(manifest.get("error_history") or [])
                if isinstance(manifest.get("error_history"), list)
                else []
            ),
            "reference": (
                manifest.get("reference")
                if isinstance(manifest.get("reference"), dict)
                else {}
            ),
            "checkpoints": (
                manifest.get("checkpoints")
                if isinstance(manifest.get("checkpoints"), dict)
                else {}
            ),
            "segments": list(previous_segments.values()),
        }
        atomic_write_json(manifest_path, manifest)

        try:
            self._notify("准备音频", 2)
            self._check_disk(work_dir)
            subtitle_path = select_chinese_subtitle(root)
            segments = subtitle_segments(subtitle_path)
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
                    "media_duration": media_duration,
                }
            )
            atomic_write_json(manifest_path, manifest)

            self._notify("分离人声", 8)
            separator = self.separator_factory(
                ffmpeg_path=self.ffmpeg_path,
                python_executable=self.python_executable,
                config=dict(self.config.get("demucs") or {}),
                log=self._log,
                command_runner=self.command_runner,
            )
            separation = separator.prepare(
                source_video,
                work_dir,
                force=force_separation,
            )
            manifest["separation"] = {
                "reused": bool(separation.get("reused")),
                "checkpoint": separation.get("checkpoint") or {},
            }
            atomic_write_json(manifest_path, manifest)

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
            atomic_write_json(manifest_path, manifest)

            model_path = resolve_model_path(self.project_root, self.config)
            tts_settings = dict(self.config.get("tts") or {})
            timing_settings = dict(self.config.get("timing") or {})
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
            prepared: list[dict[str, Any]] = []
            pending_count = 0
            for row in segments:
                input_hash = hash_json(
                    {
                        "version": 1,
                        "text": row["text"],
                        "start": round(float(row["start"]), 3),
                        "end": round(float(row["end"]), 3),
                        "reference_hash": reference_hash,
                        "model": model_identifier,
                        "tts": tts_settings,
                    }
                )
                raw_path = work_dir / "segments" / f"{int(row['index']):06d}.wav"
                previous = previous_segments.get(int(row["index"]), {})
                raw_reusable = (
                    not force_tts
                    and previous.get("input_hash") == input_hash
                    and valid_wav(raw_path)
                    and previous.get("wav_hash") == sha256_file(raw_path)
                )
                if not raw_reusable:
                    pending_count += 1
                prepared.append(
                    {
                        **row,
                        "input_hash": input_hash,
                        "raw_path": raw_path,
                        "raw_reusable": raw_reusable,
                        "previous": previous,
                    }
                )

            synthesizer: Any | None = None
            if pending_count:
                self._notify("加载 VoxCPM2", 28)
                synthesizer = self.synthesizer_factory(
                    model_path,
                    device=str(self.config.get("device") or "cuda"),
                    allow_cpu=bool(self.config.get("allow_cpu", False)),
                    settings=tts_settings,
                    log=self._log,
                )
            else:
                self._log("[DUBBING] All segment TTS caches are valid; VoxCPM2 load skipped.")

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
                    if not item["raw_reusable"]:
                        assert synthesizer is not None
                        try:
                            synthesizer.generate(str(item["text"]), reference, raw_path)
                        except Exception as exc:
                            entry.update(status="failed", error=str(exc))
                            current = [*completed_rows, entry]
                            manifest.update(
                                status="FAILED",
                                updated_at=utc_now(),
                                segments=current,
                            )
                            atomic_write_json(manifest_path, manifest)
                            raise DubbingError(
                                "TTS_SEGMENT_FAILED",
                                f"第 {index} 句中文配音生成失败；已保存前面完成的片段，重试会从此句继续。",
                                details={"index": index, "error": str(exc)},
                            ) from exc
                        entry.update(
                            status="generated",
                            wav_hash=sha256_file(raw_path),
                            generated_duration=wav_duration(raw_path),
                            generated_at=utc_now(),
                        )
                        manifest.update(
                            updated_at=utc_now(),
                            segments=[*completed_rows, entry],
                        )
                        atomic_write_json(manifest_path, manifest)
                    else:
                        entry.update(
                            status="generated",
                            wav_hash=sha256_file(raw_path),
                            generated_duration=wav_duration(raw_path),
                            reused=True,
                        )

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
                                command_runner=self.command_runner,
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
                        self._log(f"[DUBBING] {warning}")
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
                    atomic_write_json(manifest_path, manifest)
            finally:
                if synthesizer is not None:
                    close = getattr(synthesizer, "close", None)
                    if callable(close):
                        close()

            self._notify("按字幕时间轴拼接", 80)
            self._log("[DUBBING] Building Chinese voice track...")
            voice_path = work_dir / "chinese_voice.wav"
            timeline_fingerprint = hash_json(
                {
                    "version": 1,
                    "media_duration": round(media_duration, 3),
                    "segments": [
                        [row["index"], row["start"], row["final_wav_hash"]]
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
                    command_runner=self.command_runner,
                )
            checkpoints["timeline"] = {
                "fingerprint": timeline_fingerprint,
                "output_hash": sha256_file(voice_path),
                "path": "chinese_voice.wav",
                "reused": timeline_reused,
                "completed_at": utc_now(),
            }
            atomic_write_json(manifest_path, manifest)

            self._notify("混合背景音", 92)
            self._log("[DUBBING] Mixing background audio...")
            dubbed_path = work_dir / "dubbed_audio.wav"
            mix_settings = dict(self.config.get("mix") or {})
            mix_fingerprint = hash_json(
                {
                    "version": 1,
                    "background_hash": sha256_file(Path(separation["background"])),
                    "voice_hash": sha256_file(voice_path),
                    "settings": mix_settings,
                }
            )
            mix_checkpoint = (
                checkpoints.get("mix") if isinstance(checkpoints.get("mix"), dict) else {}
            )
            mix_reused = (
                not force_tts
                and valid_wav(dubbed_path)
                and mix_checkpoint.get("fingerprint") == mix_fingerprint
                and mix_checkpoint.get("output_hash") == sha256_file(dubbed_path)
            )
            if not mix_reused:
                mix_background(
                    Path(separation["background"]),
                    voice_path,
                    dubbed_path,
                    ffmpeg_path=self.ffmpeg_path,
                    duck_db=float(mix_settings.get("background_duck_db", 6.0)),
                    sample_rate=int(mix_settings.get("sample_rate", 48000)),
                    limiter=float(mix_settings.get("limiter", 0.95)),
                    media_duration=media_duration,
                    log=self._log,
                    command_runner=self.command_runner,
                )
            checkpoints["mix"] = {
                "fingerprint": mix_fingerprint,
                "output_hash": sha256_file(dubbed_path),
                "path": "dubbed_audio.wav",
                "reused": mix_reused,
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
            atomic_write_json(manifest_path, manifest)
            self._notify("中文配音完成", 100, len(completed_rows), len(completed_rows))
            self._log("[DUBBING] Completed.")
            return DubbingResult(
                status=status,
                manifest_path=manifest_path,
                dubbed_audio_path=dubbed_path,
                needs_review=needs_review,
                warnings=list(manifest["warnings"]),
            )
        except DubbingError as exc:
            manifest.update(status="FAILED", updated_at=utc_now())
            errors = list(manifest.get("errors") or [])
            errors.append(exc.to_dict())
            manifest["errors"] = errors[-20:]
            atomic_write_json(manifest_path, manifest)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            wrapped = DubbingError("DUBBING_FAILED", str(exc))
            manifest.update(status="FAILED", updated_at=utc_now())
            errors = list(manifest.get("errors") or [])
            errors.append(wrapped.to_dict())
            manifest["errors"] = errors[-20:]
            atomic_write_json(manifest_path, manifest)
            raise wrapped from exc


__all__ = [
    "DubbingError",
    "DubbingPipeline",
    "DubbingResult",
    "choose_reference_window",
    "select_chinese_subtitle",
    "subtitle_segments",
]
