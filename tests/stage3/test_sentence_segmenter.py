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

    def test_capacity_split_moves_article_to_its_noun(self) -> None:
        words = [
            WordEvent(text, index * 0.2, index * 0.2 + 0.15, index * 0.2, 1)
            for index, text in enumerate("I really want to buy a cat today".split())
        ]
        segments = segment_sentences(
            words,
            {**CONFIG, "english_max_chars_per_line": 12, "max_lines": 2},
        )
        texts = [item.text for item in segments]
        self.assertNotIn("a", [text.split()[-1].casefold() for text in texts[:-1]])
        self.assertTrue(any(text.startswith("a cat") for text in texts))
        self.assertTrue(all(len(text) <= 24 for text in texts))

    def test_dependent_phrase_is_rebalanced_as_a_unit(self) -> None:
        words = [
            WordEvent(text, index * 0.2, index * 0.2 + 0.15, index * 0.2, 1)
            for index, text in enumerate("We keep all of the little pieces together".split())
        ]
        segments = segment_sentences(
            words,
            {**CONFIG, "english_max_chars_per_line": 10, "max_lines": 2},
        )
        texts = [item.text for item in segments]
        self.assertNotIn("of the", texts[0])
        self.assertTrue(any(text.startswith("of the little") for text in texts[1:]))
        self.assertGreaterEqual(len(texts[-1].split()), 2)
        self.assertTrue(all(len(text) <= 20 for text in texts))

    def test_ellipsis_does_not_split_an_unfinished_phrase(self) -> None:
        words = [
            WordEvent("I", 0, 0.2, 0, 1),
            WordEvent("want", 0.2, 0.4, 0.2, 1),
            WordEvent("to", 0.4, 0.6, 0.4, 1),
            WordEvent("buy...", 0.6, 0.8, 0.6, 1),
            WordEvent("a", 0.9, 1.0, 0.9, 2),
            WordEvent("cat", 1.0, 1.2, 1.0, 2),
        ]
        self.assertEqual(
            [item.text for item in segment_sentences(words, CONFIG)],
            ["I want to buy... a cat"],
        )

    def test_short_final_fragment_merges_without_exceeding_hard_duration(self) -> None:
        words = [
            WordEvent("We", 0, 0.5, 0, 1),
            WordEvent("found", 0.5, 1.0, 0.5, 1),
            WordEvent("it", 1.5, 1.8, 1.5, 2),
        ]
        config = {**CONFIG, "semantic_join_gap_seconds": 1.0, "hard_max_segment_duration": 3.0}
        self.assertEqual([item.text for item in segment_sentences(words, config)], ["We found it"])

    def test_real_sentence_stop_is_not_merged_with_short_next_caption(self) -> None:
        words = [WordEvent("Hello.", 0, 1, 0, 1), WordEvent("Next", 1.1, 2, 1.1, 2)]
        self.assertEqual([item.text for item in segment_sentences(words, CONFIG)], ["Hello.", "Next"])
