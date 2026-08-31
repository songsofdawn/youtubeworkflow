from __future__ import annotations

import os
import re
import math
from pathlib import Path
from typing import Any, Iterable

from .models import SubtitleCue


ASS_GENERATOR_VERSION = "1.11"


def orientation_font_multiplier(width: int, height: int) -> float:
    """Return the compatibility multiplier for the configured 1080p sizes.

    The style keys are already expressed relative to a 1080-pixel frame
    height.  Applying a second landscape/aspect-ratio multiplier made a
    configured 48px Chinese font become 77px at ordinary 1920x1080 and caused
    otherwise usable single-line subtitles to be cropped.
    """
    del width, height
    return 1.0


def ass_generator_version(width: int, height: int) -> str:
    del width, height
    return ASS_GENERATOR_VERSION


def escape_ass_text(value: str) -> str:
    """Escape subtitle payload so it cannot become an ASS override block."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\\", r"\\")
    normalized = normalized.replace("{", r"\{").replace("}", r"\}")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    return r"\N".join(line for line in lines if line)


def single_line_text(value: str) -> str:
    """Collapse source subtitle wrapping without changing the spoken text."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    joined = " ".join(line for line in lines if line)
    # SRT wrapping should not introduce a visible gap inside ordinary Chinese text.
    return re.sub(
        r"(?<=[\u3400-\u9fff，。！？；：、]) (?=[\u3400-\u9fff，。！？；：、])",
        "",
        joined,
    )


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
        "chinese_absolute_min_font_size": int(
            round(
                float(style.get("chinese_absolute_min_font_size_1080p", 24))
                * scale
                * font_multiplier
            )
        ),
        "english_absolute_min_font_size": int(
            round(
                float(style.get("english_absolute_min_font_size_1080p", 20))
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


_NATURAL_BREAK_CHARACTERS = frozenset(
    " \t,.;:!?，。！？；：、…—-)]}）】》”’"
)


def split_text_to_width(text: str, maximum_units: float) -> list[str]:
    """Split a visible one-line subtitle into width-bounded display pages.

    Natural punctuation/word boundaries are preferred.  A hard character
    boundary is used only for an indivisible token (for example a long URL),
    so every returned page remains renderable without adding a third line.
    """
    remaining = single_line_text(text).strip()
    if not remaining:
        return [""]
    maximum_units = max(0.1, float(maximum_units))
    page_count = max(1, math.ceil(_line_width_units(remaining) / maximum_units))
    pages: list[str] = []
    for page_index in range(page_count - 1):
        pages_left = page_count - page_index
        remaining_units = _line_width_units(remaining)
        target_units = remaining_units / pages_left
        minimum_units = max(
            0.0,
            remaining_units - maximum_units * (pages_left - 1),
        )
        consumed_units = 0.0
        hard_candidates: list[tuple[float, int]] = []
        natural_candidates: list[tuple[float, int]] = []
        all_hard_candidates: list[tuple[float, int]] = []
        for index, character in enumerate(remaining):
            consumed_units += _line_width_units(character)
            if consumed_units > maximum_units:
                break
            candidate = (abs(consumed_units - target_units), index + 1)
            all_hard_candidates.append(candidate)
            if consumed_units + 1e-9 < minimum_units:
                continue
            hard_candidates.append(candidate)
            if character in _NATURAL_BREAK_CHARACTERS:
                natural_candidates.append(candidate)
        # With an almost exact multiple of the page capacity, character-width
        # granularity can leave no boundary inside the narrow ideal interval.
        # Falling back to character one creates an orphan page and leaves the
        # complete sentence overflowing on the last page.  Use the closest
        # real boundary below the capacity instead.
        best_hard = min(
            hard_candidates or all_hard_candidates,
            default=(0.0, 1),
        )
        best_natural = min(natural_candidates, default=None)
        # A punctuation break is useful only when it is reasonably balanced.
        # Otherwise a late comma can leave a one-word/short-CJK orphan page.
        if (
            best_natural is not None
            and best_natural[0] <= max(2.0, target_units * 0.35)
        ):
            boundary = best_natural[1]
        else:
            boundary = best_hard[1]
        page = remaining[:boundary].strip()
        if not page:
            boundary = max(1, boundary)
            page = remaining[:boundary].strip() or remaining[:1]
        pages.append(page)
        remaining = remaining[boundary:].strip()
    if remaining:
        pages.append(remaining)
    return pages or [""]


def _page_for_slot(pages: list[str], slot: int, total_slots: int) -> str:
    """Distribute each language's pages over a shared bilingual time grid."""
    if not pages:
        return ""
    index = min(len(pages) - 1, slot * len(pages) // max(1, total_slots))
    return pages[index]


def adaptive_font_size(
    cue: SubtitleCue,
    *,
    base_size: int,
    minimum_size: int,
    safe_width: float,
    combined_line_count: int,
    text: str | None = None,
) -> int:
    lines = (
        [text.strip()]
        if text is not None and text.strip()
        else [
            line.strip()
            for line in (cue.lines or tuple(cue.text.splitlines()))
            if line.strip()
        ]
    )
    if not lines:
        return base_size
    widest = max(_line_width_units(line) for line in lines)
    width_scale = min(1.0, safe_width / max(1.0, widest * base_size))
    density_scale = 0.92 if combined_line_count >= 4 else 0.97 if combined_line_count == 3 else 1.0
    return max(minimum_size, min(base_size, int(round(base_size * width_scale * density_scale))))


def adaptive_horizontal_scale(
    text: str,
    *,
    font_size: int,
    safe_width: float,
    minimum_percent: int,
) -> int:
    """Use mild horizontal compression only when font-size fitting is insufficient."""
    estimated_width = _line_width_units(text) * font_size
    if estimated_width <= safe_width:
        return 100
    required = int(safe_width / max(1.0, estimated_width) * 100)
    return max(minimum_percent, min(100, required))


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
    single_line = bool(style.get("one_line_per_language", True))
    maximum_english = int(style.get("max_english_lines", 1 if single_line else 2))
    maximum_chinese = int(style.get("max_chinese_lines", 1 if single_line else 2))
    maximum_combined = int(style.get("max_combined_lines", 2 if single_line else 4))
    warnings: list[dict[str, Any]] = []
    for cue in english:
        other = chinese_by_id.get(cue.identifier)
        if other is None:
            continue
        english_lines = 1 if single_line else len([line for line in cue.lines if line.strip()]) or 1
        chinese_lines = 1 if single_line else len([line for line in other.lines if line.strip()]) or 1
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
    single_line = bool(scaled.get("one_line_per_language", True))
    wrap_style = 2 if single_line else 0
    chinese_by_id = {cue.identifier: cue for cue in chinese}
    english_font = str(scaled.get("english_font", "Arial"))
    chinese_font = str(scaled.get("chinese_font", "Microsoft YaHei"))
    base_font_size = scaled["english_font_size"]
    header = f"""[Script Info]
Title: English / 中文
ScriptType: v4.00+
WrapStyle: {wrap_style}
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
    width_warnings: list[dict[str, Any]] = []
    fragmentation_warnings: list[dict[str, Any]] = []
    fragmentation_records: list[dict[str, Any]] = []
    font_tier_counts: dict[str, int] = {}
    for cue in english:
        translation = chinese_by_id[cue.identifier]
        english_display = single_line_text(cue.text) if single_line else cue.text
        chinese_display = single_line_text(translation.text) if single_line else translation.text
        english_line_count = 1 if single_line else len([line for line in cue.lines if line.strip()]) or 1
        chinese_line_count = 1 if single_line else len([line for line in translation.lines if line.strip()]) or 1
        combined_line_count = english_line_count + chinese_line_count
        safe_width = (
            scaled["play_res_x"]
            - 2 * scaled["margin_lr"]
        ) * float(scaled.get("max_line_width_ratio", 0.88))
        minimum_scale = max(
            75,
            min(100, int(scaled.get("minimum_horizontal_scale_percent", 92))),
        )
        minimum_page_duration = max(
            0.1,
            float(scaled.get("minimum_fragment_duration_seconds", 1.0)),
        )
        cue_duration = max(0.0, cue.end - cue.start)
        if single_line and bool(scaled.get("auto_fragment_long_lines", True)):
            # Prefer the configured large display sizes.  Only fall back to the
            # normal readable minima when large-font pagination would change too
            # quickly for the cue duration.  The old absolute 24/20px tier is
            # intentionally excluded from publishable single-line output.
            layout_candidates = (
                (
                    "base",
                    scaled["english_font_size"],
                    scaled["chinese_font_size"],
                ),
                (
                    "readable_minimum",
                    scaled["english_min_font_size"],
                    scaled["chinese_min_font_size"],
                ),
            )
            selected_layout: tuple[str, int, int, list[str], list[str]] | None = None
            for tier, english_floor, chinese_floor in layout_candidates:
                available_width = scaled["play_res_x"] - 2 * scaled["margin_lr"]
                # Leave one glyph of headroom.  Without it, character-width
                # granularity could put the final page about 0.5-1% past the
                # frame even though the theoretical capacity was exact.
                english_capacity = max(0.1, available_width / max(
                    1.0,
                    english_floor * minimum_scale / 100,
                ) - 1.0)
                chinese_capacity = max(0.1, available_width / max(
                    1.0,
                    chinese_floor * minimum_scale / 100,
                ) - 1.0)
                candidate_english_pages = split_text_to_width(
                    english_display, english_capacity
                )
                candidate_chinese_pages = split_text_to_width(
                    chinese_display, chinese_capacity
                )
                candidate_page_count = max(
                    len(candidate_english_pages), len(candidate_chinese_pages)
                )
                selected_layout = (
                    tier,
                    english_floor,
                    chinese_floor,
                    candidate_english_pages,
                    candidate_chinese_pages,
                )
                if (
                    candidate_page_count == 1
                    or cue_duration / candidate_page_count >= minimum_page_duration
                ):
                    break
            assert selected_layout is not None
            (
                font_tier,
                english_font_floor,
                chinese_font_floor,
                english_pages,
                chinese_pages,
            ) = selected_layout
        else:
            font_tier = "readable_minimum" if single_line else "multiline"
            english_font_floor = scaled["english_min_font_size"]
            chinese_font_floor = scaled["chinese_min_font_size"]
            english_pages = [english_display]
            chinese_pages = [chinese_display]
        font_tier_counts[font_tier] = font_tier_counts.get(font_tier, 0) + 1
        page_count = max(len(english_pages), len(chinese_pages))
        if page_count > 1:
            seconds_per_page = cue_duration / page_count
            fragmentation_records.append(
                {
                    "id": cue.identifier,
                    "english_page_count": len(english_pages),
                    "chinese_page_count": len(chinese_pages),
                    "generated_event_count": page_count,
                    "seconds_per_event": round(seconds_per_page, 3),
                    "font_tier": font_tier,
                    "english_font_floor": english_font_floor,
                    "chinese_font_floor": chinese_font_floor,
                }
            )
            if seconds_per_page < minimum_page_duration:
                fragmentation_warnings.append(
                    {
                        "code": "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT",
                        "id": cue.identifier,
                        "generated_event_count": page_count,
                        "seconds_per_event": round(seconds_per_page, 3),
                        "minimum_seconds_per_event": minimum_page_duration,
                    }
                )

        for page_index in range(page_count):
            english_page = _page_for_slot(english_pages, page_index, page_count)
            chinese_page = _page_for_slot(chinese_pages, page_index, page_count)
            english_text = escape_ass_text(english_page)
            chinese_text = escape_ass_text(chinese_page)
            english_size = adaptive_font_size(
                cue,
                base_size=scaled["english_font_size"],
                minimum_size=(
                    english_font_floor
                    if single_line
                    else scaled["english_min_font_size"]
                ),
                safe_width=safe_width,
                combined_line_count=combined_line_count,
                text=english_page if single_line else None,
            )
            chinese_size = adaptive_font_size(
                translation,
                base_size=scaled["chinese_font_size"],
                minimum_size=(
                    chinese_font_floor
                    if single_line
                    else scaled["chinese_min_font_size"]
                ),
                safe_width=safe_width,
                combined_line_count=combined_line_count,
                text=chinese_page if single_line else None,
            )
            if not single_line:
                chinese_size = min(
                    scaled["chinese_font_size"],
                    max(chinese_size, english_size + 3),
                )
            english_scale = adaptive_horizontal_scale(
                english_page,
                font_size=english_size,
                safe_width=safe_width,
                minimum_percent=minimum_scale,
            ) if single_line else 100
            chinese_scale = adaptive_horizontal_scale(
                chinese_page,
                font_size=chinese_size,
                safe_width=safe_width,
                minimum_percent=minimum_scale,
            ) if single_line else 100
            available_width = scaled["play_res_x"] - 2 * scaled["margin_lr"]
            for language, display_text, font_size, horizontal_scale in (
                ("english", english_page, english_size, english_scale),
                ("chinese", chinese_page, chinese_size, chinese_scale),
            ):
                estimated_width = (
                    _line_width_units(display_text)
                    * font_size
                    * horizontal_scale
                    / 100
                )
                if single_line and estimated_width > available_width:
                    width_warnings.append(
                        {
                            "code": "BILINGUAL_LINE_TOO_WIDE",
                            "id": cue.identifier,
                            "page": page_index + 1,
                            "language": language,
                            "estimated_width": round(estimated_width, 1),
                            "available_width": round(available_width, 1),
                            "width_ratio": round(
                                estimated_width / max(1.0, available_width), 3
                            ),
                            "text_length": len(display_text),
                            "font_size": font_size,
                            "horizontal_scale": horizontal_scale,
                        }
                    )
            if (
                english_size != scaled["english_font_size"]
                or chinese_size != scaled["chinese_font_size"]
                or english_scale != 100
                or chinese_scale != 100
            ):
                adjustments.append(
                    {
                        "id": cue.identifier,
                        "page": page_index + 1,
                        "english_font_size": english_size,
                        "chinese_font_size": chinese_size,
                        "english_horizontal_scale": english_scale,
                        "chinese_horizontal_scale": chinese_scale,
                        "combined_line_count": combined_line_count,
                    }
                )
            chinese_bold = 1 if bool(scaled.get("chinese_bold", False)) else 0
            english_payload = (
                rf"{{\fn{english_font}\fs{english_size}\fscx{english_scale}\b0}}{english_text}"
            )
            chinese_payload = (
                rf"{{\fn{chinese_font}\fs{chinese_size}\fscx{chinese_scale}\b{chinese_bold}}}"
                f"{chinese_text}"
            )
            if str(scaled.get("language_order", "english_above_chinese")) == "chinese_above_english":
                payload = chinese_payload + r"\N" + english_payload
            else:
                payload = english_payload + r"\N" + chinese_payload
            event_start = cue.start + cue_duration * page_index / page_count
            event_end = (
                cue.end
                if page_index == page_count - 1
                else cue.start + cue_duration * (page_index + 1) / page_count
            )
            events.append(
                "Dialogue: 0,"
                f"{ass_timestamp(event_start)},{ass_timestamp(event_end)},"
                f"Bilingual,,0,0,0,,{payload}"
            )
    scaled["adaptive_font_size_summary"] = {
        "adjusted_segment_count": len({item["id"] for item in adjustments}),
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
        "single_line_per_language": single_line,
        "horizontally_compressed_segment_count": sum(
            any(
                item["english_horizontal_scale"] != 100
                or item["chinese_horizontal_scale"] != 100
                for item in adjustments
                if item["id"] == identifier
            )
            for identifier in {item["id"] for item in adjustments}
        ),
        "fragmented_segment_count": len(fragmentation_records),
        "generated_event_count": len(events),
        "maximum_fragment_count": max(
            (item["generated_event_count"] for item in fragmentation_records),
            default=1,
        ),
        "fragmentation": fragmentation_records,
        "font_tier_counts": font_tier_counts,
    }
    warnings = (
        layout_warnings(english, chinese, style)
        + fragmentation_warnings
        + width_warnings
    )
    return header + "\n".join(events) + ("\n" if events else ""), scaled, warnings


def build_chinese_ass(
    chinese: list[SubtitleCue],
    style: dict[str, Any],
    *,
    width: int,
    height: int,
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Build the Chinese-only display used by the optional dubbed render.

    It intentionally shares the Stage 4 sizing, pagination and publishable font
    floors so dubbed videos receive the same crop/flash protections.
    """
    scaled = scaled_style(style, width, height)
    chinese_font = str(scaled.get("chinese_font", "Microsoft YaHei"))
    chinese_bold = 1 if bool(scaled.get("chinese_bold", False)) else 0
    header = f"""[Script Info]
Title: 中文
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {scaled["play_res_x"]}
PlayResY: {scaled["play_res_y"]}
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chinese,{chinese_font},{scaled["chinese_font_size"]},{scaled.get("primary_color", "&H00FFFFFF")},&H000000FF,{scaled.get("outline_color", "&H00000000")},{scaled.get("shadow_color", "&H80000000")},{chinese_bold},0,0,0,100,100,0,0,1,{scaled["outline"]},{scaled["shadow"]},{int(scaled.get("alignment", 2))},{scaled["margin_lr"]},{scaled["margin_lr"]},{scaled["margin_v"]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    warnings: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []
    fragmentation: list[dict[str, Any]] = []
    available_width = scaled["play_res_x"] - 2 * scaled["margin_lr"]
    safe_width = available_width * float(scaled.get("max_line_width_ratio", 0.88))
    minimum_scale = max(
        75,
        min(100, int(scaled.get("minimum_horizontal_scale_percent", 92))),
    )
    minimum_page_duration = max(
        0.1,
        float(scaled.get("minimum_fragment_duration_seconds", 1.0)),
    )
    for cue in chinese:
        display = single_line_text(cue.text)
        duration = max(0.0, cue.end - cue.start)
        selected_tier = "base"
        pages = [display]
        floor = scaled["chinese_font_size"]
        for tier, candidate_floor in (
            ("base", scaled["chinese_font_size"]),
            ("readable_minimum", scaled["chinese_min_font_size"]),
        ):
            capacity = max(
                0.1,
                available_width / max(1.0, candidate_floor * minimum_scale / 100)
                - 1.0,
            )
            candidate_pages = split_text_to_width(display, capacity)
            selected_tier, floor, pages = tier, candidate_floor, candidate_pages
            if len(pages) == 1 or duration / len(pages) >= minimum_page_duration:
                break
        page_count = max(1, len(pages))
        if page_count > 1:
            seconds_per_page = duration / page_count
            record = {
                "id": cue.identifier,
                "chinese_page_count": page_count,
                "generated_event_count": page_count,
                "seconds_per_event": round(seconds_per_page, 3),
                "font_tier": selected_tier,
                "chinese_font_floor": floor,
            }
            fragmentation.append(record)
            if seconds_per_page < minimum_page_duration:
                warnings.append(
                    {
                        "code": "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT",
                        "id": cue.identifier,
                        "generated_event_count": page_count,
                        "seconds_per_event": round(seconds_per_page, 3),
                        "minimum_seconds_per_event": minimum_page_duration,
                    }
                )
        for page_index, page in enumerate(pages):
            size = adaptive_font_size(
                cue,
                base_size=scaled["chinese_font_size"],
                minimum_size=floor,
                safe_width=safe_width,
                combined_line_count=1,
                text=page,
            )
            horizontal_scale = adaptive_horizontal_scale(
                page,
                font_size=size,
                safe_width=safe_width,
                minimum_percent=minimum_scale,
            )
            estimated_width = _line_width_units(page) * size * horizontal_scale / 100
            if estimated_width > available_width:
                warnings.append(
                    {
                        "code": "BILINGUAL_LINE_TOO_WIDE",
                        "id": cue.identifier,
                        "page": page_index + 1,
                        "language": "chinese",
                        "estimated_width": round(estimated_width, 1),
                        "available_width": round(available_width, 1),
                        "width_ratio": round(estimated_width / max(1.0, available_width), 3),
                        "text_length": len(page),
                        "font_size": size,
                        "horizontal_scale": horizontal_scale,
                    }
                )
            if size != scaled["chinese_font_size"] or horizontal_scale != 100:
                adjustments.append(
                    {
                        "id": cue.identifier,
                        "page": page_index + 1,
                        "chinese_font_size": size,
                        "chinese_horizontal_scale": horizontal_scale,
                    }
                )
            event_start = cue.start + duration * page_index / page_count
            event_end = (
                cue.end
                if page_index == page_count - 1
                else cue.start + duration * (page_index + 1) / page_count
            )
            payload = (
                rf"{{\fn{chinese_font}\fs{size}\fscx{horizontal_scale}\b{chinese_bold}}}"
                + escape_ass_text(page)
            )
            events.append(
                "Dialogue: 0,"
                f"{ass_timestamp(event_start)},{ass_timestamp(event_end)},"
                f"Chinese,,0,0,0,,{payload}"
            )
    scaled["adaptive_font_size_summary"] = {
        "adjusted_segment_count": len({item["id"] for item in adjustments}),
        "chinese_min_used": min(
            (item["chinese_font_size"] for item in adjustments),
            default=scaled["chinese_font_size"],
        ),
        "chinese_max_used": scaled["chinese_font_size"],
        "single_line_per_language": True,
        "horizontally_compressed_segment_count": len(
            {
                item["id"]
                for item in adjustments
                if item["chinese_horizontal_scale"] != 100
            }
        ),
        "fragmented_segment_count": len(fragmentation),
        "generated_event_count": len(events),
        "maximum_fragment_count": max(
            (item["generated_event_count"] for item in fragmentation),
            default=1,
        ),
        "fragmentation": fragmentation,
    }
    return header + "\n".join(events) + ("\n" if events else ""), scaled, warnings
