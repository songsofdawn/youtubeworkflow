from __future__ import annotations

import html
import math
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from langdetect import LangDetectException, detect


def normalized_text(value: str) -> str:
    return " ".join(html.unescape(value or "").casefold().split())


def phrase_in_text(text: str, phrase: str) -> bool:
    phrase = normalized_text(phrase)
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def phrase_hits(text: str, phrases: Iterable[str]) -> list[str]:
    normalized = normalized_text(text)
    return [phrase for phrase in phrases if phrase_in_text(normalized, phrase)]


def assess_language(snippet: dict[str, Any], has_caption: bool, markers: list[str]) -> dict[str, Any]:
    audio = normalized_text(str(snippet.get("defaultAudioLanguage") or ""))
    declared = normalized_text(str(snippet.get("defaultLanguage") or ""))
    title = str(snippet.get("title", ""))
    description = str(snippet.get("description", ""))[:1000]
    text = normalized_text(f"{title} {description}")
    marker_hits = phrase_hits(text, markers)

    if audio.startswith("en"):
        return {"detected_language": "en", "language_confidence": "high", "language_source": "defaultAudioLanguage", "language_filter_reason": "", "is_english": True}
    if audio and not audio.startswith("en"):
        return {"detected_language": audio, "language_confidence": "high", "language_source": "defaultAudioLanguage", "language_filter_reason": "non_english_audio", "is_english": False}
    if marker_hits:
        return {"detected_language": marker_hits[0], "language_confidence": "high", "language_source": "explicit_marker", "language_filter_reason": "non_english_marker", "is_english": False}
    if declared.startswith("en"):
        return {"detected_language": "en", "language_confidence": "medium", "language_source": "defaultLanguage", "language_filter_reason": "", "is_english": True}
    if declared and not declared.startswith("en"):
        return {"detected_language": declared, "language_confidence": "medium", "language_source": "defaultLanguage", "language_filter_reason": "non_english_declared", "is_english": False}
    try:
        detected = detect(f"{title} {description}".strip())
    except LangDetectException:
        detected = "unknown"
    if detected == "en":
        source = "text_detection+caption" if has_caption else "text_detection"
        return {"detected_language": "en", "language_confidence": "medium", "language_source": source, "language_filter_reason": "", "is_english": True}
    return {"detected_language": detected, "language_confidence": "low", "language_source": "text_detection", "language_filter_reason": "english_not_confirmed", "is_english": False}


def calculate_interest(text: str, primary_topic: str, config: dict[str, Any]) -> dict[str, Any]:
    hits: list[str] = []
    score = 0.0
    for strength, phrases in config["interest_phrases"].items():
        found = phrase_hits(text, phrases)
        hits.extend(found)
        score += len(found) * float(config["interest_phrase_weights"][strength])
    topic_hits = phrase_hits(text, config.get("topic_interest_phrases", {}).get(primary_topic, []))
    for value in topic_hits:
        if value not in hits:
            hits.append(value)
            score += float(config.get("topic_interest_bonus_per_hit", 1.5))
    boring = phrase_hits(text, config.get("boring_penalty_phrases", []))
    boring.extend(value for value in phrase_hits(text, config.get("topic_penalty_phrases", {}).get(primary_topic, [])) if value not in boring)
    return {"interest_score": round(score, 3), "interest_hits": sorted(set(hits)), "boring_penalty": len(set(boring)) * float(config["boring_penalty_per_hit"]), "boring_hits": sorted(set(boring))}


def calculate_topic_relevance(snippet: dict[str, Any], source_topics: set[str], category_title: str, config: dict[str, Any]) -> tuple[str, list[str], float, dict[str, list[str]]]:
    title = normalized_text(str(snippet.get("title", "")))
    description = normalized_text(str(snippet.get("description", ""))[:1500])
    tags = normalized_text(" ".join(str(tag) for tag in snippet.get("tags", [])))
    channel = normalized_text(str(snippet.get("channelTitle", "")))
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, list[str]] = defaultdict(list)
    for topic, keywords in config["topic_keywords"].items():
        for keyword in keywords:
            if phrase_in_text(title, keyword):
                scores[topic] += 6; hits[topic].append(keyword)
            elif phrase_in_text(tags, keyword):
                scores[topic] += 3; hits[topic].append(keyword)
            elif phrase_in_text(description, keyword):
                scores[topic] += 2; hits[topic].append(keyword)
            elif phrase_in_text(channel, keyword):
                scores[topic] += 0.5
        if topic in source_topics:
            scores[topic] += 3
        if config.get("category_to_topic", {}).get(category_title) == topic:
            scores[topic] += 2
        if topic == "gaming":
            priority_hits = phrase_hits(title, config.get("priority_games", []))
            if priority_hits:
                scores[topic] += 12
                hits[topic].extend(priority_hits)
    if not scores:
        return "other", [], 0.0, {}
    ordered = sorted(scores, key=lambda topic: (scores[topic], len(hits[topic])), reverse=True)
    minimum = float(config["topic_relevance_min"])
    all_topics = [topic for topic in ordered if scores[topic] >= minimum]
    primary = all_topics[0] if all_topics else "other"
    primary_score = min(100.0, scores[primary]) if primary != "other" else max(scores.values(), default=0.0)
    return primary, all_topics, round(primary_score, 3), {topic: sorted(set(hits[topic])) for topic in all_topics}


def copyright_risk(category_title: str, text: str, license_name: str, hard_hits: list[str]) -> str:
    if hard_hits:
        return "very_high"
    if license_name == "creativeCommon":
        return "low"
    if category_title in {"Music", "Film & Animation"} or phrase_hits(text, ["performance", "concert", "broadcast", "highlights"]):
        return "high"
    return "medium"


def metric_scores(row: dict[str, Any], config: dict[str, Any]) -> None:
    row["growth_score"] = round(min(100.0, 20.0 * math.log10(float(row["views_per_hour"]) + 1)), 3)
    row["engagement_score"] = round(min(100.0, float(row["like_rate"]) * 900 + float(row["comment_rate"]) * 4500), 3)
    row["freshness_score"] = round(max(0.0, 100.0 * (1.0 - float(row["age_hours"]) / 72.0)), 3)
    confidence_score = {"high": 100.0, "medium": 75.0, "low": 25.0}.get(row["language_confidence"], 0.0)
    suitability = confidence_score * 0.45 + min(100.0, float(row["interest_score"]) * 4) * 0.35 + (100.0 if row["has_caption"] else 40.0) * 0.2
    preferred = config.get("tutorial_preferred_duration_seconds", [240, 1200])
    if row["primary_topic"] == "tutorials" and int(preferred[0]) <= int(row["duration_seconds"]) <= int(preferred[1]):
        suitability += float(config.get("tutorial_localization_bonus", 0))
    suitability -= min(40.0, float(row["boring_penalty"]))
    row["localization_suitability_score"] = round(max(0.0, min(100.0, suitability)), 3)
    row["localization_suitability"] = "high" if suitability >= 70 else "medium" if suitability >= 45 else "low"
    source_count = len(row["popular_source_details"]) + len(row["search_source_details"])
    row["source_diversity_score"] = min(100.0, source_count * 20.0)
    row["caption_score"] = 100.0 if row["has_caption"] else 0.0
    interest_normalized = min(100.0, float(row["interest_score"]) * 4)
    topic_normalized = min(100.0, float(row["topic_relevance_score"]) * 4)
    components = {**row, "interest_score_normalized": interest_normalized, "topic_relevance_normalized": topic_normalized}
    raw = sum(float(weight) * float(components[name]) for name, weight in config["final_score_weights"].items())
    penalties = config["penalties"]
    copyright_penalty = float(penalties[f"copyright_{row['copyright_risk']}"])
    language_penalty = float(penalties[f"language_{row['language_confidence']}"]) if row["language_confidence"] != "high" else 0.0
    row["copyright_penalty"] = copyright_penalty
    row["language_uncertainty_penalty"] = language_penalty
    row["event_duplicate_penalty"] = 0.0
    row["raw_score"] = round(raw, 3)
    row["final_score"] = round(max(0.0, raw - float(row["boring_penalty"]) - copyright_penalty - language_penalty), 3)
    row["score"] = row["final_score"]


def event_tokens(title: str, stop_words: list[str]) -> set[str]:
    clean = re.sub(r"[^\w\s]", " ", normalized_text(title), flags=re.UNICODE)
    stops = {word.casefold() for word in stop_words}
    return {token for token in clean.split() if len(token) > 1 and token not in stops}


def title_similarity(left: str, right: str, stop_words: list[str]) -> tuple[float, float]:
    left_tokens, right_tokens = event_tokens(left, stop_words), event_tokens(right, stop_words)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio()
    return jaccard, sequence


def assign_event_groups(rows: list[dict[str, Any]], config: dict[str, Any]) -> None:
    settings = config["event_similarity"]
    representatives: list[tuple[str, str]] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda item: item["final_score"], reverse=True):
        group_id = ""
        for existing_id, title in representatives:
            jaccard, sequence = title_similarity(row["title"], title, settings["stop_words"])
            if jaccard >= float(settings["jaccard_threshold"]) or sequence >= float(settings["sequence_threshold"]):
                group_id = existing_id
                break
        if not group_id:
            group_id = f"event_{len(representatives) + 1:04d}"
            representatives.append((group_id, row["title"]))
        row["event_key"] = " ".join(sorted(event_tokens(row["title"], settings["stop_words"])))
        row["semantic_duplicate_group"] = group_id
        groups[group_id].append(row)
    penalty_step = float(config["penalties"]["event_duplicate"])
    for members in groups.values():
        for index, row in enumerate(sorted(members, key=lambda item: item["final_score"], reverse=True)):
            penalty = index * penalty_step
            row["event_duplicate_penalty"] = penalty
            row["final_score"] = round(max(0.0, row["final_score"] - penalty), 3)
            row["score"] = row["final_score"]
