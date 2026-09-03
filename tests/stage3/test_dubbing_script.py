from __future__ import annotations

from pathlib import Path

from src.stage3.dubbing_script import (
    build_dubbing_utterances,
    canonical_script_payload,
    canonical_text_from_payload,
    normalize_script_text,
)
from src.stage3.models import SubtitleSegment, TranslationSegment


def segment(identifier: int, start: float, end: float, text: str) -> SubtitleSegment:
    return SubtitleSegment(identifier, start, end, text)


def test_visual_fragments_merge_into_one_dubbing_utterance() -> None:
    source = [
        segment(1, 10.00, 10.80, "Today we talked"),
        segment(2, 10.82, 11.45, "about a lot"),
        segment(3, 11.47, 12.80, "of different things."),
    ]

    result = build_dubbing_utterances(source)

    assert len(result) == 1
    assert result[0].start == 10.00
    assert result[0].end == 12.80
    assert result[0].text == "Today we talked about a lot of different things."
    assert result[0].source_segment_ids == [1, 2, 3]


def test_complete_sentence_boundary_is_preserved() -> None:
    source = [
        segment(1, 0.0, 2.5, "This already works."),
        segment(2, 2.6, 5.0, "Now we test the next one."),
    ]

    result = build_dubbing_utterances(source)

    assert [item.text for item in result] == [
        "This already works.",
        "Now we test the next one.",
    ]


def test_short_reaction_can_stay_independent() -> None:
    source = [
        segment(1, 0.0, 0.7, "Wow!"),
        segment(2, 0.75, 3.0, "That is actually pretty good."),
    ]

    result = build_dubbing_utterances(source)

    assert len(result) == 2
    assert result[0].text == "Wow!"


def test_canonical_payload_has_one_chinese_source_of_truth() -> None:
    utterances = [segment(1, 1.0, 4.0, "We tested all three servers.")]
    translated = [
        TranslationSegment(
            1,
            1.0,
            4.0,
            utterances[0].text,
            "我们把这三台服务器都测试了一遍。",
            "我们把这三台服务器都测试了一遍。",
        )
    ]

    payload = canonical_script_payload(
        Path("en.selected.srt"),
        Path("en.dubbing.srt"),
        Path("zh.dubbing.srt"),
        utterances,
        translated,
    )

    assert payload["architecture"] == "single_script_dual_segmentation"
    assert payload["utterance_count"] == 1
    assert payload["utterances"][0]["zh_text"] == "我们把这三台服务器都测试了一遍。"
    assert normalize_script_text(canonical_text_from_payload(payload)) == normalize_script_text(
        "我们把这三台服务器都测试了一遍。"
    )


def test_comma_visual_break_is_remerged_for_speech() -> None:
    source = [
        segment(1, 0.0, 2.4, "The important thing is,"),
        segment(2, 2.45, 4.8, "we need to test it first."),
    ]

    result = build_dubbing_utterances(source)

    assert len(result) == 1
    assert result[0].text == "The important thing is, we need to test it first."


def test_semantic_boundary_prefilter_flags_false_period_fragments() -> None:
    from src.stage3.dubbing_script import suspicious_boundary_candidates

    source = [
        segment(1, 0.0, 0.8, "The most."),
        segment(2, 0.82, 1.5, "Overlooked AI."),
        segment(3, 1.52, 2.0, "Stock."),
        segment(4, 2.5, 3.0, "Wow!"),
    ]

    candidates = suspicious_boundary_candidates(source)
    left_ids = {item["left_id"] for item in candidates}

    assert 1 in left_ids
    assert 2 in left_ids
    assert 4 not in left_ids


def test_semantic_boundary_decisions_merge_without_preserving_fake_period() -> None:
    from src.stage3.dubbing_script import apply_semantic_boundary_decisions

    source = [
        segment(1, 0.0, 0.8, "The most."),
        segment(2, 0.82, 1.5, "Overlooked AI."),
        segment(3, 1.52, 2.0, "Stock."),
    ]

    repaired, report = apply_semantic_boundary_decisions(
        source,
        {1: True, 2: True},
        {"semantic_hard_max_duration_seconds": 10.5},
    )

    assert len(repaired) == 1
    assert repaired[0].text == "The most Overlooked AI Stock."
    assert repaired[0].source_segment_ids == [1, 2, 3]
    assert report["merged_boundary_count"] == 2
