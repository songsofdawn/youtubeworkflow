from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

from .models import SubtitleCue


ASS_GENERATOR_VERSION = "1.3"
VERTICAL_ASS_GENERATOR_VERSION = "1.2"


def orientation_font_multiplier(width: int, height: int) -> float:
    """Keep portrait text stable and enlarge landscape text by aspect ratio."""
    if width <= height:
        return 1.0
    aspect_ratio = width / max(1, height)
    if aspect_ratio >= 2.0:
        return 1.75
    if aspect_ratio >= 16 / 9:
        return 1.6
    return 1.5


def ass_generator_version(width: int, height: int) -> str:
    # Portrait output is byte-for-byte compatible with v1.2, so its existing
    # checkpoint remains reusable. Landscape output must be rebuilt once.
    return (
        ASS_GENERATOR_VERSION
        if orientation_font_multiplier(width, height) > 1.0
        else VERTICAL_ASS_GENERATOR_VERSION
    )


def escape_ass_text(value: str) -> str:
    """Escape subtitle payload so it cannot become an ASS override block."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\\", r"\\")
    normalized = normalized.replace("{", r"\{").replace("}", r"\}")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    return r"\N".join(line for line in lines if line)


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _round_scaled(value: float, scale: float, *, minimum: float = 1.0) -> float:
    return max(minimum, round(value * scale, 2))


def scaled_style(style: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    scale = max(0.1, height / 1080.0)
    font_multiplier = orientation_font_multiplier(width, height)
    return {
        **style,
        "play_res_x": int(width),
        "play_res_y": int(height),
        "orientation_font_multiplier": font_multiplier,
        "chinese_font_size": int(
            round(
                float(style.get("chinese_font_size_1080p", 48))
                * scale
                * font_multiplier
            )
        ),
        "english_font_size": int(
            round(
                float(style.get("english_font_size_1080p", 34))
                * scale
                * font_multiplier
            )
        ),
        "chinese_min_font_size": int(
            round(
                float(style.get("chinese_min_font_size_1080p", 34))
                * scale
                * font_multiplier
            )
        ),
        "english_min_font_size": int(
            round(
                float(style.get("english_min_font_size_1080p", 25))
                * scale
                * font_multiplier
            )
        ),
        "outline": _round_scaled(
            float(style.get("outline_1080p", 2.5)),
            scale * font_multiplier,
            minimum=0.1,
        ),
        "shadow": _round_scaled(
            float(style.get("shadow_1080p", 0.8)),
            scale * font_multiplier,
            minimum=0.0,
        ),
        "margin_v": int(round(float(style.get("margin_v_1080p", 75)) * scale)),
        "margin_lr": int(round(float(style.get("margin_lr_1080p", 80)) * scale)),
    }


def _line_width_units(text: str) -> float:
    units = 0.0
    for character in text:
        if "\u3400" <= character <= "\u9fff":
            units += 1.0
        elif character.isspace():
            units += 0.32
        elif character.isascii() and character.isalnum():
            units += 0.56
        else:
            units += 0.52
    return units


def adaptive_font_size(
    cue: SubtitleCue,
    *,
    base_size: int,
    minimum_size: int,
    safe_width: float,
    combined_line_count: int,
) -> int:
    lines = [line.strip() for line in (cue.lines or tuple(cue.text.splitlines())) if line.strip()]
    if not lines:
        return base_size
    widest = max(_line_width_units(line) for line in lines)
    width_scale = min(1.0, safe_width / max(1.0, widest * base_size))
    density_scale = 0.92 if combined_line_count >= 4 else 0.97 if combined_line_count == 3 else 1.0
    return max(minimum_size, min(base_size, int(round(base_size * width_scale * density_scale))))


def available_font_names() -> set[str]:
    if os.name != "nt":
        return set()
    try:
        import winreg
    except ImportError:
        return set()
    names: set[str] = set()
    registry_paths = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for hive, registry_path in registry_paths:
        try:
            with winreg.OpenKey(hive, registry_path) as key:
                index = 0
                while True:
                    try:
                        display_name, _, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    normalized = re.sub(r"\s+\((?:TrueType|OpenType)\)$", "", display_name, flags=re.I)
                    names.add(normalized.casefold())
                    index += 1
        except OSError:
            continue
    return names


def resolve_fonts(
    style: dict[str, Any],
    installed_fonts: Iterable[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    resolved = dict(style)
    available = {name.casefold() for name in (installed_fonts or available_font_names())}
    warnings: list[str] = []
    if not available and os.name != "nt":
        return resolved, warnings

    def present(name: str) -> bool:
        requested = name.casefold()
        return any(
            requested == installed
            or installed.startswith(requested + " &")
            or requested in {part.strip() for part in installed.split("&")}
            for installed in available
        )

    fallback = str(style.get("fallback_font", "Arial Unicode MS"))
    fallback_exists = present(fallback)
    for key in ("chinese_font", "english_font"):
        requested = str(style.get(key, "")).strip()
        if requested and present(requested):
            continue
        if fallback_exists:
            resolved[key] = fallback
            warnings.append(f"FONT_FALLBACK:{key}:{requested}->{fallback}")
        else:
            warnings.append(f"FONT_NOT_FOUND:{key}:{requested}")
    return resolved, warnings


def layout_warnings(
    english: list[SubtitleCue],
    chinese: list[SubtitleCue],
    style: dict[str, Any],
) -> list[dict[str, Any]]:
    chinese_by_id = {cue.identifier: cue for cue in chinese}
    maximum_english = int(style.get("max_english_lines", 2))
    maximum_chinese = int(style.get("max_chinese_lines", 2))
    maximum_combined = int(style.get("max_combined_lines", 4))
    warnings: list[dict[str, Any]] = []
    for cue in english:
        other = chinese_by_id.get(cue.identifier)
        if other is None:
            continue
        english_lines = len([line for line in cue.lines if line.strip()]) or 1
        chinese_lines = len([line for line in other.lines if line.strip()]) or 1
        if (
            english_lines > maximum_english
            or chinese_lines > maximum_chinese
            or english_lines + chinese_lines > maximum_combined
        ):
            warnings.append(
                {
                    "code": "BILINGUAL_TOO_MANY_LINES",
                    "id": cue.identifier,
                    "english_lines": english_lines,
                    "chinese_lines": chinese_lines,
                    "combined_lines": english_lines + chinese_lines,
                }
            )
    return warnings


def build_bilingual_ass(
    english: list[SubtitleCue],
    chinese: list[SubtitleCue],
    style: dict[str, Any],
    *,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    scaled = scaled_style(style, width, height)
    chinese_by_id = {cue.identifier: cue for cue in chinese}
    english_font = str(scaled.get("english_font", "Arial"))
    chinese_font = str(scaled.get("chinese_font", "Microsoft YaHei"))
    base_font_size = scaled["english_font_size"]
    header = f"""[Script Info]
Title: English / 中文
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {scaled["play_res_x"]}
PlayResY: {scaled["play_res_y"]}
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bilingual,{english_font},{base_font_size},{scaled.get("primary_color", "&H00FFFFFF")},&H000000FF,{scaled.get("outline_color", "&H00000000")},{scaled.get("shadow_color", "&H80000000")},0,0,0,0,100,100,0,0,1,{scaled["outline"]},{scaled["shadow"]},{int(scaled.get("alignment", 2))},{scaled["margin_lr"]},{scaled["margin_lr"]},{scaled["margin_v"]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    adjustments: list[dict[str, Any]] = []
    for cue in english:
        translation = chinese_by_id[cue.identifier]
        english_text = escape_ass_text(cue.text)
        chinese_text = escape_ass_text(translation.text)
        english_line_count = len([line for line in cue.lines if line.strip()]) or 1
        chinese_line_count = len([line for line in translation.lines if line.strip()]) or 1
        combined_line_count = english_line_count + chinese_line_count
        safe_width = (
            scaled["play_res_x"]
            - 2 * scaled["margin_lr"]
        ) * float(scaled.get("max_line_width_ratio", 0.88))
        english_size = adaptive_font_size(
            cue,
            base_size=scaled["english_font_size"],
            minimum_size=scaled["english_min_font_size"],
            safe_width=safe_width,
            combined_line_count=combined_line_count,
        )
        chinese_size = adaptive_font_size(
            translation,
            base_size=scaled["chinese_font_size"],
            minimum_size=scaled["chinese_min_font_size"],
            safe_width=safe_width,
            combined_line_count=combined_line_count,
        )
        chinese_size = min(
            scaled["chinese_font_size"],
            max(chinese_size, english_size + 3),
        )
        if (
            english_size != scaled["english_font_size"]
            or chinese_size != scaled["chinese_font_size"]
        ):
            adjustments.append(
                {
                    "id": cue.identifier,
                    "english_font_size": english_size,
                    "chinese_font_size": chinese_size,
                    "combined_line_count": combined_line_count,
                }
            )
        chinese_bold = 1 if bool(scaled.get("chinese_bold", False)) else 0
        payload = (
            rf"{{\fn{english_font}\fs{english_size}\b0}}{english_text}"
            r"\N"
            rf"{{\fn{chinese_font}\fs{chinese_size}\b{chinese_bold}}}{chinese_text}"
        )
        events.append(
            "Dialogue: 0,"
            f"{ass_timestamp(cue.start)},{ass_timestamp(cue.end)},"
            f"Bilingual,,0,0,0,,{payload}"
        )
    scaled["adaptive_font_size_summary"] = {
        "adjusted_segment_count": len(adjustments),
        "english_min_used": min(
            (item["english_font_size"] for item in adjustments),
            default=scaled["english_font_size"],
        ),
        "english_max_used": scaled["english_font_size"],
        "chinese_min_used": min(
            (item["chinese_font_size"] for item in adjustments),
            default=scaled["chinese_font_size"],
        ),
        "chinese_max_used": scaled["chinese_font_size"],
    }
    warnings = layout_warnings(english, chinese, style)
    return header + "\n".join(events) + ("\n" if events else ""), scaled, warnings
