from __future__ import annotations

from unittest import TestCase

from src.stage3.models import WordEvent
from src.stage3.sentence_segmenter import segment_sentences


CONFIG = {"sentence_gap_seconds": 0.6, "max_segment_duration": 7.0, "english_max_chars_per_line": 42, "max_lines": 2}


class SentenceSegmenterTests(TestCase):
    def test_sentence_punctuation_splits(self) -> None:
        words = [WordEvent("Hello.", 0, 1, 0, 1), WordEvent("Next", 1.1, 2, 1.1, 2)]
        self.assertEqual([item.text for item in segment_sentences(words, CONFIG)], ["Hello.", "Next"])

    def test_large_pause_splits_without_punctuation(self) -> None:
        words = [WordEvent("Hello", 0, 0.5, 0, 1), WordEvent("again", 1.2, 2, 1.2, 2)]
        self.assertEqual(len(segment_sentences(words, CONFIG)), 2)

    def test_long_text_splits_at_word_boundary(self) -> None:
        words = [WordEvent("word", i * 0.1, i * 0.1 + 0.09, i * 0.1, 1) for i in range(30)]
        segments = segment_sentences(words, {**CONFIG, "english_max_chars_per_line": 10})
        self.assertGreater(len(segments), 1)
        self.assertTrue(all("word" in item.text for item in segments))
