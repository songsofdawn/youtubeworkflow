from __future__ import annotations

from unittest import TestCase

from src.stage3.models import SubtitleSegment
from src.stage3.timeline_builder import rebuild_timeline


CONFIG = {"minimum_gap_ms": 20, "min_segment_duration": 0.8, "max_segment_duration": 7.0}


class TimelineBuilderTests(TestCase):
    def test_final_timeline_is_valid_and_non_overlapping(self) -> None:
        segments = [SubtitleSegment(1, 0, 1, "one"), SubtitleSegment(2, 0.8, 2, "two")]
        fixed = rebuild_timeline(segments, CONFIG, 3)
        self.assertTrue(all(item.start < item.end for item in fixed))
        self.assertTrue(all(right.start >= left.end for left, right in zip(fixed, fixed[1:])))

    def test_short_segment_merges_instead_of_extending_into_next(self) -> None:
        segments = [SubtitleSegment(1, 0, 1, "one"), SubtitleSegment(2, 1.1, 1.2, "tiny")]
        fixed = rebuild_timeline(segments, CONFIG, 2)
        self.assertEqual(len(fixed), 1)
        self.assertIn("tiny", fixed[0].text)

    def test_media_duration_caps_last_segment(self) -> None:
        fixed = rebuild_timeline([SubtitleSegment(1, 4, 8, "end")], CONFIG, 5)
        self.assertLessEqual(fixed[-1].end, 5)

    def test_short_segment_merge_never_breaks_caption_capacity(self) -> None:
        config = {**CONFIG, "english_max_chars_per_line": 5, "max_lines": 2}
        segments = [
            SubtitleSegment(1, 0, 1, "123456789"),
            SubtitleSegment(2, 1.05, 1.2, "tail"),
        ]
        fixed = rebuild_timeline(segments, config, 2)
        self.assertEqual([item.text for item in fixed], ["123456789", "tail"])
        self.assertTrue(all(len(item.text) <= 10 for item in fixed))
