from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import SubtitleSegment


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_METADATA_PROMPT_VERSION = "stage3-publish-metadata-v2"
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
_PREFIX_PATTERN = re.compile(r"^\s*【\s*(?:中英双语|原声中字|无配音)\s*】\s*")


def normalize_title_prefix(value: Any) -> str:
    """Normalize an optional user-facing title prefix.

    A prefix is presentation text only.  It may be Chinese, English, or any
    other title-safe text; it must never be interpreted as a bilingual marker.
    Collapsing whitespace also keeps newlines out of the generated CLI title.
    """
    return " ".join(str(value or "").split()).strip()


def utf16_code_units(value: str) -> int:
    """Return the length used by Bilibili's web form and API validators."""
    return sum(2 if ord(character) > 0xFFFF else 1 for character in str(value))


def utf8_bytes(value: str) -> int:
    return len(str(value).encode("utf-8"))


def truncate_utf16(
    value: str,
    max_units: int,
    *,
    suffix: str = "…",
) -> str:
    """Truncate without splitting a supplementary Unicode character."""
    text = str(value)
    if max_units <= 0:
        return ""
    if utf16_code_units(text) <= max_units:
        return text

    ending = suffix if utf16_code_units(suffix) <= max_units else ""
    budget = max_units - utf16_code_units(ending)
    used = 0
    kept: list[str] = []
    for character in text:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > budget:
            break
        kept.append(character)
        used += units
    return "".join(kept).rstrip() + ending


def truncate_utf8(
    value: str,
    max_bytes: int,
    *,
    suffix: str = "…",
) -> str:
    """Truncate text to a UTF-8 byte budget without splitting characters."""
    text = str(value)
    if max_bytes <= 0:
        return ""
    if utf8_bytes(text) <= max_bytes:
        return text

    ending = suffix if utf8_bytes(suffix) <= max_bytes else ""
    budget = max_bytes - utf8_bytes(ending)
    used = 0
    kept: list[str] = []
    for character in text:
        size = utf8_bytes(character)
        if used + size > budget:
            break
        kept.append(character)
        used += size
    return "".join(kept).rstrip() + ending


def load_category_mapping(path: Path | str | None = None) -> dict[str, Any]:
    mapping_path = Path(path) if path is not None else PROJECT_ROOT / "config" / "bilibili_categories.json"
    if not mapping_path.is_absolute():
        mapping_path = PROJECT_ROOT / mapping_path
    payload = json.loads(mapping_path.read_text(encoding="utf-8-sig"))
    rows = payload.get("categories")
    if not isinstance(rows, list) or not rows:
        raise ValueError("哔哩哔哩分区映射不能为空")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("哔哩哔哩分区映射条目必须是对象")
        tid = int(row["tid"])
        if tid < 1 or tid in seen:
            raise ValueError(f"哔哩哔哩分区 TID 无效或重复：{tid}")
        seen.add(tid)
        name = str(row.get("name") or "").strip()
        parent_name = str(row.get("parent_name") or "").strip()
        if not name or not parent_name:
            raise ValueError(f"哔哩哔哩分区名称不完整：{tid}")
        normalized.append(
            {
                "tid": tid,
                "name": name,
                "parent_tid": int(row.get("parent_tid") or 0),
                "parent_name": parent_name,
                "path": f"{parent_name} / {name}",
            }
        )
    fallback_tid = int(payload.get("fallback_tid") or 0)
    if fallback_tid not in seen:
        raise ValueError("默认哔哩哔哩分区不在分区映射中")
    return {
        "schema_version": int(payload.get("schema_version") or 1),
        "source": str(payload.get("source") or ""),
        "fallback_tid": fallback_tid,
        "categories": normalized,
        "path": str(mapping_path.resolve()),
    }


def category_for_tid(mapping: dict[str, Any], tid: int) -> dict[str, Any]:
    for row in mapping["categories"]:
        if int(row["tid"]) == int(tid):
            return dict(row)
    raise ValueError(f"未知的哔哩哔哩分区 TID：{tid}")


def compose_localized_title(
    chinese_title: str,
    english_title: str,
    *,
    prefix: str = "",
    fallback_title: str,
    max_length: int = 80,
) -> str:
    """Compose a localized title using Bilibili's UTF-16 code-unit limit.

    Python ``len`` counts supplementary characters such as emoji once, while
    Bilibili's web form and API count each of them as two UTF-16 code units.
    Build every budget from the same counter used by submission validation so
    generated defaults can always be submitted unchanged.  When the combined
    title is too long, keep the complete Chinese title whenever it fits and
    spend the remaining budget on the English title.
    """
    normalized_prefix = normalize_title_prefix(prefix)
    chinese = _PREFIX_PATTERN.sub("", " ".join(str(chinese_title or "").split())).strip(" ｜|+-")
    english = _PREFIX_PATTERN.sub("", " ".join(str(english_title or "").split())).strip(" ｜|+-")
    if not chinese:
        chinese = fallback_title
    if not english or english.casefold() == chinese.casefold():
        return truncate_utf16(normalized_prefix + chinese, max_length)
    separator = "｜"
    full = f"{normalized_prefix}{chinese}{separator}{english}"
    if utf16_code_units(full) <= max_length:
        return full
    prefix_units = utf16_code_units(normalized_prefix)
    chinese_units = utf16_code_units(chinese)
    chinese_budget = max_length - prefix_units
    if chinese_budget <= 0:
        return truncate_utf16(normalized_prefix, max_length)
    if chinese_units > chinese_budget:
        # The Chinese title alone exceeds the platform limit.  In this
        # impossible-to-preserve case, omit English instead of sacrificing
        # additional Chinese content for a bilingual separator.
        chinese_short = truncate_utf16(chinese, chinese_budget).rstrip()
        return truncate_utf16(
            f"{normalized_prefix}{chinese_short}",
            max_length,
        )

    english_budget = (
        max_length - prefix_units - utf16_code_units(separator) - chinese_units
    )
    if english_budget <= 1:
        return f"{normalized_prefix}{chinese}"
    english_short = truncate_utf16(english, english_budget).rstrip()
    if not english_short:
        return f"{normalized_prefix}{chinese}"
    return truncate_utf16(
        f"{normalized_prefix}{chinese}{separator}{english_short}",
        max_length,
    )


def compose_bilingual_title(
    chinese_title: str,
    english_title: str,
    *,
    prefix: str = "",
    max_length: int = 80,
) -> str:
    return compose_localized_title(
        chinese_title,
        english_title,
        prefix=prefix,
        fallback_title="中英双语精选",
        max_length=max_length,
    )


def normalize_tags(
    value: Any,
    *,
    fallback: list[str] | None = None,
    required: list[str] | None = None,
    excluded: list[str] | None = None,
) -> str:
    if isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = re.split(r"[,，、;\n]+", str(value or ""))
    candidates = list(required or []) + candidates + list(fallback or [])
    excluded_values = {"中英双语".casefold(), "中文翻译".casefold()}
    excluded_values.update(
        str(item).strip().casefold() for item in (excluded or [])
    )
    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tag = re.sub(r"^[#＃]+", "", candidate.strip())
        tag = re.sub(r"\s+", " ", tag).strip(" ,，、;；")
        if not tag or tag.casefold() in excluded_values:
            continue
        tag = tag[:20].rstrip()
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(tag)
        if len(cleaned) >= 10:
            break
    return ",".join(cleaned)


def _subtitle_sample(segments: list[SubtitleSegment]) -> list[dict[str, Any]]:
    if len(segments) <= 30:
        selected = segments
    else:
        selected = segments[:12] + segments[len(segments) // 2 - 4 : len(segments) // 2 + 4] + segments[-10:]
    return [
        {"id": item.id, "start": round(item.start, 2), "text": item.text[:300]}
        for item in selected
    ]


def build_publish_metadata_messages(
    metadata: dict[str, Any],
    segments: list[SubtitleSegment],
    mapping: dict[str, Any],
) -> list[dict[str, str]]:
    category_options = [
        {"tid": row["tid"], "category": row["path"]}
        for row in mapping["categories"]
    ]
    system = (
        "你是哔哩哔哩视频本地化编辑。根据英文标题、原简介、原标签以及可能存在的字幕样本，"
        "生成准确克制的中文标题、投稿标签并推荐最具体的哔哩哔哩小分区。"
        "不得编造视频中没有的事实，不使用夸张点击诱导。中文标题不要包含【中英双语】前缀，"
        "应自然概括内容并尽量控制在 12—32 个中文字符。标签返回 5—8 个内容相关词，"
        "不要返回‘中英双语’或‘中文翻译’这类描述视频形式的标签，"
        "不要带 # 或逗号。tid 必须严格选自给定分区列表。只返回合法 JSON："
        '{"chinese_title":"示例中文标题","tags":["标签1","标签2"],'
        '"tid":231,"reason":"一句话说明推荐依据"}'
    )
    payload = {
        "original_title": str(metadata.get("title") or "")[:500],
        "channel": str(metadata.get("channel") or "")[:200],
        "youtube_category": str(metadata.get("topic") or "")[:200],
        "original_tags": list(metadata.get("tags") or [])[:30],
        "original_description": str(metadata.get("description") or "")[:6000],
        "subtitle_sample": _subtitle_sample(segments),
        "allowed_bilibili_categories": category_options,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "请分析内容并按指定结构输出 JSON：\n"
            + json.dumps(payload, ensure_ascii=False),
        },
    ]


def normalize_ai_recommendation(
    payload: dict[str, Any],
    metadata: dict[str, Any],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    chinese_title = _PREFIX_PATTERN.sub(
        "",
        " ".join(str(payload.get("chinese_title") or "").split()),
    ).strip(" ｜|+-")
    if not chinese_title or not _CJK_PATTERN.search(chinese_title):
        raise ValueError("AI API 没有返回有效的中文标题")
    chinese_title = chinese_title[:40].rstrip()
    tid = int(payload.get("tid"))
    category = category_for_tid(mapping, tid)
    source_tags = [str(item) for item in metadata.get("tags") or []]
    tags = normalize_tags(payload.get("tags"), fallback=source_tags[:2])
    if len(tags.split(",")) < 3:
        raise ValueError("AI API 返回的投稿标签不足")
    reason = " ".join(str(payload.get("reason") or "").split())[:160]
    original_title = str(metadata.get("title") or "").strip()
    return {
        "status": "RECOMMENDED",
        "title_zh": chinese_title,
        "original_title": original_title,
        "upload_title": compose_bilingual_title(chinese_title, original_title),
        "tags": tags,
        "tid": category["tid"],
        "category_name": category["name"],
        "parent_tid": category["parent_tid"],
        "parent_name": category["parent_name"],
        "category_path": category["path"],
        "recommendation_reason": reason or "根据标题、简介和字幕内容综合推荐。",
        "warning": "",
    }


def fallback_publish_metadata(
    metadata: dict[str, Any],
    mapping: dict[str, Any],
    *,
    warning: str,
) -> dict[str, Any]:
    category = category_for_tid(mapping, int(mapping["fallback_tid"]))
    original_title = str(metadata.get("title") or "").strip()
    chinese_title = original_title if _CJK_PATTERN.search(original_title) else "中英双语精选"
    return {
        "status": "FALLBACK",
        "title_zh": chinese_title[:40],
        "original_title": original_title,
        "upload_title": compose_bilingual_title(chinese_title, original_title),
        "tags": normalize_tags(metadata.get("tags"), fallback=[category["name"]]),
        "tid": category["tid"],
        "category_name": category["name"],
        "parent_tid": category["parent_tid"],
        "parent_name": category["parent_name"],
        "category_path": category["path"],
        "recommendation_reason": "智能推荐暂不可用，已使用通用分区，请在投稿前人工核对。",
        "warning": warning[:500],
    }


def build_publish_description(
    original_description: str,
    *,
    disclaimer: str,
    original_heading: str,
    max_length: int = 2000,
    max_utf8_bytes: int = 1900,
) -> str:
    source = str(original_description or "").strip()
    if not source:
        source = "（原视频未提供简介）"
    fixed = f"{str(disclaimer).strip()}\n\n{str(original_heading).strip()}\n"
    available = max(0, max_length - utf16_code_units(fixed))
    source = truncate_utf16(source, available)
    description = truncate_utf16(fixed + source, max_length)
    return truncate_utf8(description, max_utf8_bytes)


__all__ = [
    "PUBLISH_METADATA_PROMPT_VERSION",
    "build_publish_description",
    "build_publish_metadata_messages",
    "category_for_tid",
    "compose_bilingual_title",
    "compose_localized_title",
    "fallback_publish_metadata",
    "load_category_mapping",
    "normalize_ai_recommendation",
    "normalize_title_prefix",
    "normalize_tags",
    "truncate_utf8",
    "truncate_utf16",
    "utf8_bytes",
    "utf16_code_units",
]
