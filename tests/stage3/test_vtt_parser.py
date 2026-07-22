from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from src.stage3.youtube_vtt_parser import parse_youtube_vtt


FIXTURE = Path(__file__).parent / "fixtures" / "rolling_sample.vtt"


class VttParserTests(TestCase):
    def test_parses_header_note_html_entities_and_multiline(self) -> None:
        cues = parse_youtube_vtt(FIXTURE)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[0].text, "Hello")
        self.assertEqual(cues[2].text, "world & friends.")

    def test_recovers_inline_word_times(self) -> None:
        cue = parse_youtube_vtt(FIXTURE)[1]
        self.assertEqual([word.text for word in cue.words], ["Hello", "world"])
        self.assertAlmostEqual(cue.words[0].start, 0.1)
        self.assertAlmostEqual(cue.words[1].start, 0.5)

    def test_plain_vtt_uses_cue_timing_approximation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.vtt"
            path.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nOne two\n", encoding="utf-8")
            cue = parse_youtube_vtt(path)[0]
            self.assertEqual((cue.words[0].start, cue.words[-1].end), (1.0, 3.0))
