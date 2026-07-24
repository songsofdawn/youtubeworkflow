from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .bilingual_ass import (
    ASS_GENERATOR_VERSION,
    build_bilingual_ass,
    resolve_fonts,
)
from .ffmpeg_runner import (
    FFmpegRunner,
    build_hardsub_command,
    build_softsub_command,
    readable_command,
    resolve_video_encoder,
    select_audio_mode,
    temporary_output_path,
)
from .input_resolver import resolve_inputs
from .media_probe import probe_media, tool_version
from .models import PipelineOptions, PipelineResult, Stage4Error
from .quality_control import evaluate_render, write_render_qc
from .stage4_manifest import (
    atomic_write_json,
    atomic_write_text,
    empty_manifest,
    hash_json,
    load_manifest,
    output_matches_checkpoint,
    sha256_file,
    utc_now,
)
from .subtitle_validator import validate_subtitles


class Stage4Pipeline:
    def __init__(
        self,
        project_root: Path | str,
        config: dict[str, Any],
        *,
        ffmpeg_path: Path | str | None = None,
        ffprobe_path: Path | str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config
        self.ffmpeg_path = Path(ffmpeg_path or self.project_root / "tools" / "bin" / "ffmpeg.exe")
        self.ffprobe_path = Path(ffprobe_path or self.project_root / "tools" / "bin" / "ffprobe.exe")

    def _check_tools(self) -> None:
        missing = [
            str(path)
            for path in (self.ffmpeg_path, self.ffprobe_path)
            if not path.is_file()
        ]
        if missing:
            raise Stage4Error("MEDIA_TOOL_NOT_FOUND", "缺少项目本地媒体工具。", details={"paths": missing})

    @staticmethod
    def _make_directories(video_dir: Path) -> dict[str, Path]:
        root = video_dir / "stage4"
        paths = {
            "root": root,
            "subtitles": root / "subtitles",
            "video": root / "video",
            "qc": root / "qc",
            "logs": root / "logs",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths

    @staticmethod
    def _render_requested(mode: str, target: str) -> bool:
        return mode == target or mode == "both"

    def _probe_and_qc(
        self,
        source_probe: dict[str, Any],
        output_path: Path,
        *,
        mode: str,
        command_returncode: int | None,
        audio_transcoded: bool,
        duration_tolerance: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        output_probe = probe_media(self.ffprobe_path, output_path)
        report = evaluate_render(
            source_probe,
            output_probe,
            mode=mode,
            output_path=output_path,
            duration_tolerance=duration_tolerance,
            ffmpeg_returncode=command_returncode,
            audio_transcoded=audio_transcoded,
            temporary_cleaned=True,
        )
        return output_probe, report

    def _render_output(
        self,
        *,
        kind: str,
        destination: Path,
        command_builder: Any,
        fingerprint: str,
        force: bool,
        resume: bool,
        checkpoint: dict[str, Any] | None,
        source_probe: dict[str, Any],
        duration_tolerance: float,
        audio_transcoded: bool,
        runner: FFmpegRunner,
        keep_temp: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
        if (
            resume
            and not force
            and output_matches_checkpoint(
                checkpoint,
                fingerprint=fingerprint,
                output_path=destination,
            )
        ):
            output_probe, report = self._probe_and_qc(
                source_probe,
                destination,
                mode=kind,
                command_returncode=0,
                audio_transcoded=audio_transcoded,
                duration_tolerance=duration_tolerance,
            )
            return output_probe, report, dict(checkpoint or {}), True

        temporary = temporary_output_path(destination)
        temporary.unlink(missing_ok=True)
        command = command_builder(temporary)
        print(f"[stage4] {kind}: {readable_command(command)}", flush=True)
        result = runner.run(command)
        if not result.success:
            if not keep_temp:
                temporary.unlink(missing_ok=True)
            raise Stage4Error(
                f"{kind.upper()}_FFMPEG_FAILED",
                f"{kind} FFmpeg 执行失败。",
                details={
                    "returncode": result.returncode,
                    "stderr": result.stderr.strip()[-2000:],
                },
            )
        try:
            output_probe, report = self._probe_and_qc(
                source_probe,
                temporary,
                mode=kind,
                command_returncode=result.returncode,
                audio_transcoded=audio_transcoded,
                duration_tolerance=duration_tolerance,
            )
            if report["qc_status"] != "QC_PASSED":
                raise Stage4Error(
                    f"{kind.upper()}_QC_FAILED",
                    f"{kind} 输出未通过自动质量检查。",
                    details={"failed_checks": report["failed_checks"]},
                )
            os.replace(temporary, destination)
            output_probe["path"] = str(destination.resolve())
            report["output_path"] = str(destination.resolve())
        finally:
            if not keep_temp:
                temporary.unlink(missing_ok=True)
        new_checkpoint = {
            "fingerprint": fingerprint,
            "output_hash": sha256_file(destination),
            "qc_status": report["qc_status"],
            "completed_at": utc_now(),
        }
        return output_probe, report, new_checkpoint, False

    def run(
        self,
        video_dir: Path | str,
        options: PipelineOptions | None = None,
    ) -> PipelineResult:
        options = options or PipelineOptions()
        root = Path(video_dir).resolve()
        if not root.is_dir():
            raise Stage4Error("VIDEO_DIR_NOT_FOUND", f"视频任务目录不存在：{root}")
        paths = self._make_directories(root)
        manifest_path = paths["root"] / "stage4_manifest.json"
        previous = load_manifest(manifest_path)
        manifest = empty_manifest(root, options.mode)
        manifest["checkpoints"] = previous.get("checkpoints", {})
        started = time.monotonic()
        plan: dict[str, Any] = {"mode": options.mode, "dry_run": options.dry_run, "commands": {}}
        reports: dict[str, Any] = load_manifest(paths["qc"] / "render_qc.json")
        probes_after: dict[str, Any] = load_manifest(paths["qc"] / "media_probe_after.json")
        warnings: list[str] = []
        source_hash = english_hash = chinese_hash = ""

        try:
            self._check_tools()
            resolved = resolve_inputs(
                root,
                self.config,
                require_reviewed=options.require_reviewed,
            )
            source_hash = sha256_file(resolved.source_video)
            english_hash = sha256_file(resolved.english_subtitle)
            chinese_hash = sha256_file(resolved.chinese_subtitle)
            source_probe = probe_media(self.ffprobe_path, resolved.source_video)
            atomic_write_json(paths["qc"] / "media_probe_before.json", source_probe)
            chinese_selection_report_path = (
                paths["subtitles"] / "chinese_selection_report.json"
            )
            atomic_write_json(
                chinese_selection_report_path,
                resolved.chinese_selection_report,
            )
            if resolved.chinese_subtitle_auto_selected:
                warnings.append(
                    "AUTO_SELECTED_CHINESE_SUBTITLE:"
                    f"{resolved.chinese_subtitle.name}:"
                    f"{resolved.chinese_subtitle_selection_score:.3f}"
                )

            manifest.update(
                {
                    "source_video_path": str(resolved.source_video),
                    "source_video_hash": source_hash,
                    "source_video_probe": source_probe,
                    "source_video_selection_reason": resolved.source_video_reason,
                    "source_video_candidates": [str(path) for path in resolved.source_video_candidates],
                    "english_subtitle_path": str(resolved.english_subtitle),
                    "english_subtitle_hash": english_hash,
                    "chinese_subtitle_path": str(resolved.chinese_subtitle),
                    "chinese_subtitle_hash": chinese_hash,
                    "chinese_subtitle_reviewed": resolved.chinese_subtitle_reviewed,
                    "chinese_subtitle_auto_selected": resolved.chinese_subtitle_auto_selected,
                    "chinese_subtitle_selection_reason": (
                        resolved.chinese_subtitle_selection_reason
                    ),
                    "chinese_subtitle_selection_score": (
                        resolved.chinese_subtitle_selection_score
                    ),
                    "chinese_selection_report_path": str(chinese_selection_report_path),
                    "original_audio_codec": [
                        item.get("codec") for item in source_probe.get("audio_streams", [])
                    ],
                    "original_duration": source_probe.get("duration", 0),
                    "ffmpeg_version": tool_version(self.ffmpeg_path),
                    "ffprobe_version": tool_version(self.ffprobe_path),
                }
            )
            validation = validate_subtitles(
                resolved.english_subtitle,
                resolved.chinese_subtitle,
                tolerance_ms=int(
                    self.config.get("input", {}).get("subtitle_time_tolerance_ms", 20)
                ),
                video_duration=float(source_probe.get("duration") or 0),
            )
            atomic_write_json(paths["subtitles"] / "subtitle_report.json", validation.report)
            if not validation.passed:
                atomic_write_json(paths["qc"] / "subtitle_qc.json", validation.report)
                raise Stage4Error(
                    str(validation.report["validation_status"]),
                    "中英字幕一致性校验失败，未生成正式 ASS。",
                    details=validation.report,
                )

            style_config, font_warnings = resolve_fonts(
                dict(self.config.get("subtitle_style", {}))
            )
            warnings.extend(font_warnings)
            ass_text, scaled_style, layout_issues = build_bilingual_ass(
                validation.english,
                validation.chinese,
                style_config,
                width=int(source_probe["display_width"]),
                height=int(source_probe["display_height"]),
            )
            subtitle_qc = {
                **validation.report,
                "layout_warnings": layout_issues,
                "font_warnings": font_warnings,
                "scaled_style": scaled_style,
                "qc_status": "REVIEW_REQUIRED" if layout_issues else "QC_PASSED",
            }
            atomic_write_json(paths["qc"] / "subtitle_qc.json", subtitle_qc)
            warnings.extend(
                f"{item['code']}:{item['id']}" for item in layout_issues
            )
            if layout_issues and not options.dry_run:
                atomic_write_text(paths["subtitles"] / "bilingual_preview.ass", ass_text)
            if layout_issues and options.strict_subtitle_layout:
                raise Stage4Error(
                    "BILINGUAL_TOO_MANY_LINES",
                    "严格字幕排版模式检测到超过行数限制的片段。",
                    details={"warnings": layout_issues},
                )

            style_hash = hash_json(self.config.get("subtitle_style", {}))
            config_hash = hash_json(self.config)
            ass_fingerprint = hash_json(
                {
                    "english": english_hash,
                    "chinese": chinese_hash,
                    "style": style_hash,
                    "display": [
                        source_probe["display_width"],
                        source_probe["display_height"],
                    ],
                    "generator": ASS_GENERATOR_VERSION,
                }
            )
            ass_path = paths["subtitles"] / "bilingual.ass"
            ass_force = options.force or options.force_ass
            ass_checkpoint = manifest["checkpoints"].get("ass", {})
            ass_reused = (
                options.resume
                and not ass_force
                and output_matches_checkpoint(
                    ass_checkpoint,
                    fingerprint=ass_fingerprint,
                    output_path=ass_path,
                )
            )
            if not options.dry_run:
                if not ass_reused:
                    atomic_write_text(ass_path, ass_text)
                    manifest["checkpoints"]["ass"] = {
                        "fingerprint": ass_fingerprint,
                        "output_hash": sha256_file(ass_path),
                        "qc_status": "QC_PASSED",
                        "completed_at": utc_now(),
                    }
                manifest.update(
                    {
                        "bilingual_ass_path": str(ass_path),
                        "bilingual_ass_hash": sha256_file(ass_path),
                    }
                )
            plan["ass"] = {
                "path": str(ass_path),
                "reused": ass_reused,
                "generator_version": ASS_GENERATOR_VERSION,
                "scaled_style": scaled_style,
            }
            manifest.update(
                {
                    "subtitle_segment_count": len(validation.english),
                    "subtitle_style_config_hash": style_hash,
                    "config_hash": config_hash,
                }
            )

            render_config = dict(self.config.get("render", {}))
            preserve_existing = bool(render_config.get("preserve_existing_subtitle_tracks", True))
            soft_path = paths["video"] / "final_bilingual_softsub.mkv"
            hard_path = paths["video"] / "final_bilingual_hardsub.mp4"
            if self._render_requested(options.mode, "hardsub"):
                audio_mode, audio_transcoded, audio_warnings = select_audio_mode(
                    source_probe,
                    require_audio_copy=options.require_audio_copy,
                )
            else:
                audio_mode, audio_transcoded, audio_warnings = "copy", False, []
            warnings.extend(audio_warnings)
            selected_encoder = ""
            if self._render_requested(options.mode, "hardsub"):
                selected_encoder = resolve_video_encoder(options.video_encoder, self.ffmpeg_path)

            soft_plan = build_softsub_command(
                self.ffmpeg_path,
                resolved.source_video,
                ass_path,
                soft_path,
                existing_subtitle_count=int(source_probe.get("subtitle_stream_count", 0)),
                preserve_existing_subtitles=preserve_existing,
                preserve_metadata=bool(render_config.get("preserve_metadata", True)),
                preserve_chapters=bool(render_config.get("preserve_chapters", True)),
            )
            hard_plan = (
                build_hardsub_command(
                    self.ffmpeg_path,
                    resolved.source_video,
                    ass_path,
                    hard_path,
                    video_encoder=selected_encoder,
                    audio_mode=audio_mode,
                    render_config=render_config,
                )
                if selected_encoder
                else []
            )
            if self._render_requested(options.mode, "softsub"):
                plan["commands"]["softsub"] = soft_plan
            if self._render_requested(options.mode, "hardsub"):
                plan["commands"]["hardsub"] = hard_plan
            plan.update(
                {
                    "inputs": resolved.to_dict(),
                    "video_encoder": selected_encoder,
                    "audio_mode": audio_mode,
                    "warnings": warnings,
                }
            )

            if options.dry_run:
                manifest.update(
                    {
                        "video_encoder": selected_encoder,
                        "audio_mode": audio_mode,
                        "audio_transcoded": audio_transcoded,
                        "status": "DRY_RUN_COMPLETED",
                        "qc_status": "REVIEW_REQUIRED"
                        if (
                            (
                                not resolved.chinese_subtitle_reviewed
                                and not resolved.chinese_subtitle_auto_selected
                            )
                            or layout_issues
                        )
                        else "QC_PASSED",
                        "warnings": warnings,
                    }
                )
                manifest["finished_at"] = utc_now()
                manifest["processing_seconds"] = round(time.monotonic() - started, 3)
                manifest["plan"] = plan
                dry_run_path = paths["qc"] / "dry_run_plan.json"
                atomic_write_json(dry_run_path, manifest)
                return PipelineResult(
                    status="DRY_RUN_COMPLETED",
                    manifest_path=dry_run_path,
                    plan=plan,
                    warnings=warnings,
                )

            runner = FFmpegRunner(paths["logs"] / "ffmpeg_commands.log", cwd=self.project_root)
            duration_tolerance = float(render_config.get("duration_tolerance_seconds", 0.5))
            ass_hash = sha256_file(ass_path)
            soft_fingerprint = hash_json(
                {
                    "source": source_hash,
                    "ass": ass_hash,
                    "config": config_hash,
                    "kind": "softsub",
                }
            )
            hard_fingerprint = (
                hash_json(
                    {
                        "source": source_hash,
                        "ass": ass_hash,
                        "config": config_hash,
                        "encoder": selected_encoder,
                        "audio_mode": audio_mode,
                        "kind": "hardsub",
                    }
                )
                if selected_encoder
                else ""
            )
            soft_checkpoint = manifest["checkpoints"].get("softsub")
            soft_is_current = output_matches_checkpoint(
                soft_checkpoint,
                fingerprint=soft_fingerprint,
                output_path=soft_path,
            )
            if not self._render_requested(options.mode, "softsub"):
                if soft_is_current:
                    soft_probe, soft_report = self._probe_and_qc(
                        source_probe,
                        soft_path,
                        mode="softsub",
                        command_returncode=0,
                        audio_transcoded=False,
                        duration_tolerance=duration_tolerance,
                    )
                    probes_after["softsub"] = soft_probe
                    reports["softsub"] = soft_report
                    manifest["softsub_output_path"] = str(soft_path)
                    manifest["softsub_output_hash"] = sha256_file(soft_path)
                else:
                    probes_after.pop("softsub", None)
                    reports.pop("softsub", None)
            if not self._render_requested(options.mode, "hardsub"):
                hard_checkpoint = manifest["checkpoints"].get("hardsub")
                previous_encoder = str(
                    (hard_checkpoint or {}).get("video_encoder")
                    or previous.get("video_encoder")
                    or ""
                )
                previous_audio_mode = str(
                    (hard_checkpoint or {}).get("audio_mode")
                    or previous.get("audio_mode")
                    or ""
                )
                preserved_hard_fingerprint = (
                    hash_json(
                        {
                            "source": source_hash,
                            "ass": ass_hash,
                            "config": config_hash,
                            "encoder": previous_encoder,
                            "audio_mode": previous_audio_mode,
                            "kind": "hardsub",
                        }
                    )
                    if previous_encoder and previous_audio_mode
                    else ""
                )
                hard_is_current = bool(preserved_hard_fingerprint) and output_matches_checkpoint(
                    hard_checkpoint,
                    fingerprint=preserved_hard_fingerprint,
                    output_path=hard_path,
                )
                if hard_is_current:
                    preserved_transcoded = bool(
                        (hard_checkpoint or {}).get(
                            "audio_transcoded",
                            previous.get("audio_transcoded", False),
                        )
                    )
                    hard_probe, hard_report = self._probe_and_qc(
                        source_probe,
                        hard_path,
                        mode="hardsub",
                        command_returncode=0,
                        audio_transcoded=preserved_transcoded,
                        duration_tolerance=duration_tolerance,
                    )
                    probes_after["hardsub"] = hard_probe
                    reports["hardsub"] = hard_report
                    manifest["hardsub_output_path"] = str(hard_path)
                    manifest["hardsub_output_hash"] = sha256_file(hard_path)
                else:
                    probes_after.pop("hardsub", None)
                    reports.pop("hardsub", None)
            if self._render_requested(options.mode, "softsub"):
                soft_probe, soft_report, checkpoint, reused = self._render_output(
                    kind="softsub",
                    destination=soft_path,
                    command_builder=lambda output: build_softsub_command(
                        self.ffmpeg_path,
                        resolved.source_video,
                        ass_path,
                        output,
                        existing_subtitle_count=int(
                            source_probe.get("subtitle_stream_count", 0)
                        ),
                        preserve_existing_subtitles=preserve_existing,
                        preserve_metadata=bool(render_config.get("preserve_metadata", True)),
                        preserve_chapters=bool(render_config.get("preserve_chapters", True)),
                    ),
                    fingerprint=soft_fingerprint,
                    force=options.force or options.force_softsub,
                    resume=options.resume,
                    checkpoint=manifest["checkpoints"].get("softsub"),
                    source_probe=source_probe,
                    duration_tolerance=duration_tolerance,
                    audio_transcoded=False,
                    runner=runner,
                    keep_temp=options.keep_temp,
                )
                soft_report["reused"] = reused
                probes_after["softsub"] = soft_probe
                reports["softsub"] = soft_report
                manifest["checkpoints"]["softsub"] = checkpoint
                manifest["softsub_output_path"] = str(soft_path)
                manifest["softsub_output_hash"] = sha256_file(soft_path)

            if self._render_requested(options.mode, "hardsub"):
                hard_probe, hard_report, checkpoint, reused = self._render_output(
                    kind="hardsub",
                    destination=hard_path,
                    command_builder=lambda output: build_hardsub_command(
                        self.ffmpeg_path,
                        resolved.source_video,
                        ass_path,
                        output,
                        video_encoder=selected_encoder,
                        audio_mode=audio_mode,
                        render_config=render_config,
                    ),
                    fingerprint=hard_fingerprint,
                    force=options.force or options.force_hardsub,
                    resume=options.resume,
                    checkpoint=manifest["checkpoints"].get("hardsub"),
                    source_probe=source_probe,
                    duration_tolerance=duration_tolerance,
                    audio_transcoded=audio_transcoded,
                    runner=runner,
                    keep_temp=options.keep_temp,
                )
                hard_report["reused"] = reused
                probes_after["hardsub"] = hard_probe
                reports["hardsub"] = hard_report
                checkpoint.update(
                    {
                        "video_encoder": selected_encoder,
                        "audio_mode": audio_mode,
                        "audio_transcoded": audio_transcoded,
                    }
                )
                manifest["checkpoints"]["hardsub"] = checkpoint
                manifest["hardsub_output_path"] = str(hard_path)
                manifest["hardsub_output_hash"] = sha256_file(hard_path)

            if probes_after:
                atomic_write_json(paths["qc"] / "media_probe_after.json", probes_after)
                write_render_qc(
                    paths["qc"] / "render_qc.json",
                    paths["qc"] / "render_qc.txt",
                    reports,
                )
            last_probe = next(reversed(probes_after.values()), source_probe)
            hard_checkpoint = manifest["checkpoints"].get("hardsub", {})
            effective_encoder = selected_encoder or str(
                hard_checkpoint.get("video_encoder") or previous.get("video_encoder") or ""
            )
            effective_audio_mode = (
                audio_mode
                if self._render_requested(options.mode, "hardsub")
                else str(hard_checkpoint.get("audio_mode") or "copy")
            )
            effective_audio_transcoded = (
                audio_transcoded
                if self._render_requested(options.mode, "hardsub")
                else bool(hard_checkpoint.get("audio_transcoded", False))
            )
            manifest.update(
                {
                    "video_encoder": effective_encoder,
                    "audio_mode": effective_audio_mode,
                    "audio_transcoded": effective_audio_transcoded,
                    "output_audio_codec": [
                        item.get("codec") for item in last_probe.get("audio_streams", [])
                    ],
                    "output_duration": last_probe.get("duration", 0),
                    "warnings": warnings,
                }
            )
            if (
                sha256_file(resolved.source_video) != source_hash
                or sha256_file(resolved.english_subtitle) != english_hash
                or sha256_file(resolved.chinese_subtitle) != chinese_hash
            ):
                raise Stage4Error(
                    "SOURCE_FILE_MODIFIED",
                    "阶段四运行期间检测到原始视频或字幕被修改。",
                )
            technical_passed = all(
                report.get("qc_status") == "QC_PASSED" for report in reports.values()
            )
            review_required = (
                not resolved.chinese_subtitle_reviewed
                and not resolved.chinese_subtitle_auto_selected
            ) or bool(layout_issues)
            if technical_passed and review_required:
                manifest["status"] = "REVIEW_REQUIRED"
                manifest["qc_status"] = "REVIEW_REQUIRED"
            elif technical_passed:
                manifest["status"] = "STAGE4_COMPLETED"
                manifest["qc_status"] = "QC_PASSED"
            else:
                manifest["status"] = "FAILED"
                manifest["qc_status"] = "FAILED"
            if options.mode == "ass":
                manifest["status"] = (
                    "REVIEW_REQUIRED"
                    if (
                        (
                            not resolved.chinese_subtitle_reviewed
                            and not resolved.chinese_subtitle_auto_selected
                        )
                        or layout_issues
                    )
                    else "STAGE4_COMPLETED"
                )
                manifest["qc_status"] = (
                    "REVIEW_REQUIRED"
                    if manifest["status"] == "REVIEW_REQUIRED"
                    else "QC_PASSED"
                )
            return self._finish(manifest, manifest_path, started, plan)
        except Stage4Error as exc:
            manifest["status"] = "FAILED"
            manifest["qc_status"] = "FAILED"
            manifest["warnings"] = warnings
            manifest.setdefault("errors", []).append(exc.to_dict())
            self._finish(manifest, manifest_path, started, plan)
            raise

    @staticmethod
    def _finish(
        manifest: dict[str, Any],
        manifest_path: Path,
        started: float,
        plan: dict[str, Any],
    ) -> PipelineResult:
        manifest["finished_at"] = utc_now()
        manifest["processing_seconds"] = round(time.monotonic() - started, 3)
        atomic_write_json(manifest_path, manifest)
        return PipelineResult(
            status=str(manifest.get("status") or ""),
            manifest_path=manifest_path,
            plan=plan,
            warnings=list(manifest.get("warnings") or []),
            errors=list(manifest.get("errors") or []),
        )
