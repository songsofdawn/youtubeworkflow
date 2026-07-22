from __future__ import annotations

from unittest import TestCase

from src.stage3.models import RawCue, WordEvent
from src.stage3.rolling_caption_cleaner import build_word_events, extract_increment


def cue(identifier: int, start: float, end: float, text: str) -> RawCue:
    tokens = text.split()
    duration = end - start
    words = [WordEvent(token, start + duration * i / len(tokens), start + duration * (i + 1) / len(tokens), start, identifier) for i, token in enumerate(tokens)]
    return RawCue(identifier, start, end, text, words)


class RollingCaptionCleanerTests(TestCase):
    def test_exact_duplicate_has_no_increment(self) -> None:
        self.assertEqual(extract_increment("Hello world", "Hello world")[0], "")

    def test_prefix_update_keeps_only_new_content(self) -> None:
        self.assertEqual(extract_increment("Hello", "Hello world")[0], "world")

    def test_suffix_prefix_overlap_is_removed(self) -> None:
        self.assertEqual(extract_increment("we like Roblox", "like Roblox today")[0], "today")

    def test_spoken_repetition_is_preserved(self) -> None:
        events, _ = build_word_events([cue(1, 0, 1, "No, no, no.")])
        self.assertEqual([item.text for item in events], ["No,", "no,", "no."])

    def test_ten_millisecond_transition_is_processed_before_removal(self) -> None:
        events, stats = build_word_events([cue(1, 0, 1, "Hello"), cue(2, 1, 1.01, "Hello world")])
        self.assertIn("world", [item.text for item in events])
        self.assertEqual(stats["transition_cues_processed"], 1)

    def test_empty_and_exact_duplicate_cues_are_counted(self) -> None:
        empty = RawCue(1, 0, 1, "", [])
        _, stats = build_word_events([empty, cue(2, 1, 2, "Hi"), cue(3, 2, 3, "Hi")])
        self.assertEqual(stats["empty_cues_removed"], 1)
        self.assertEqual(stats["exact_duplicate_cues_removed"], 1)
