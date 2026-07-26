from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from src.clean_subtitles import clean_subtitle_file, clean_cues, parse_srt, parse_webvtt


def vtt(*cues: tuple[str, str, str]) -> str:
    blocks = ["WEBVTT", "Kind: captions", "Language: en"]
    for start, end, text in cues:
        blocks.append(f"{start} --> {end} align:start position:0%\n{text}")
    return "\n\n".join(blocks) + "\n"


class SubtitleCleanerTests(TestCase):
    def _clean(self, content: str, chinese: str | None = None):
        temporary = tempfile.TemporaryDirectory()
        directory = Path(temporary.name)
        source = directory / "en.auto.vtt"; source.write_text(content, encoding="utf-8")
        if chinese is not None:
            (directory / "zh.auto.vtt").write_text(chinese.replace("Language: en", "Language: zh-Hans"), encoding="utf-8")
        result = clean_subtitle_file(source)
        return temporary, source, result

    def test_removes_ten_millisecond_transition_cue(self) -> None:
        temporary, _, result = self._clean(vtt(
            ("00:00:00.000", "00:00:00.010", "flash"),
            ("00:00:00.010", "00:00:02.000", "Useful sentence."),
        ))
        try:
            self.assertEqual(result["transition_removed"], 1)
            self.assertNotIn("flash", Path(result["clean_srt"]).read_text(encoding="utf-8"))
        finally:
            temporary.cleanup()

    def test_removes_empty_cue(self) -> None:
        temporary, _, result = self._clean(vtt(
            ("00:00:00.000", "00:00:01.000", "<c> </c>"),
            ("00:00:01.000", "00:00:03.000", "Visible text."),
        ))
        try:
            self.assertEqual(result["empty_removed"], 1)
            self.assertEqual(len(parse_srt(result["clean_srt"])), 1)
        finally:
            temporary.cleanup()

    def test_leading_blank_line_inside_vtt_cue_does_not_drop_text(self) -> None:
        temporary, _, result = self._clean(vtt(
            ("00:00:00.000", "00:00:02.000", " \n<c>Hello after blank.</c>"),
        ))
        try:
            cues = parse_srt(result["clean_srt"])
            self.assertEqual(len(cues), 1)
            self.assertEqual(cues[0].text, "Hello after blank.")
        finally:
            temporary.cleanup()

    def test_merges_exact_adjacent_duplicates(self) -> None:
        temporary, _, result = self._clean(vtt(
            ("00:00:00.000", "00:00:01.000", "Same sentence."),
            ("00:00:01.000", "00:00:02.000", "Same sentence."),
        ))
        try:
            cues = parse_srt(result["clean_srt"])
            self.assertEqual(len(cues), 1)
            self.assertGreaterEqual(result["exact_duplicates_merged"], 1)
        finally:
            temporary.cleanup()

    def test_rolling_caption_keeps_only_new_content(self) -> None:
        temporary, _, result = self._clean(vtt(
            ("00:00:00.000", "00:00:02.000", "Hello world."),
            ("00:00:02.000", "00:00:04.000", "Hello world. This is new."),
            ("00:00:04.000", "00:00:06.000", "This is new. Great news."),
        ))
        try:
            text = " ".join(cue.text for cue in parse_srt(result["clean_srt"]))
            self.assertEqual(text.casefold().count("hello world"), 1)
            self.assertEqual(text.casefold().count("this is new"), 1)
            self.assertEqual(text.casefold().count("great news"), 1)
            self.assertGreaterEqual(result["rolling_cues_reduced"], 2)
        finally:
            temporary.cleanup()

    def test_fixed_timeline_never_overlaps(self) -> None:
        raw = parse_webvtt(self._write_temp(vtt(
            ("00:00:00.000", "00:00:02.000", "First sentence."),
            ("00:00:01.500", "00:00:03.000", "Second sentence."),
        )))
        cleaned, _ = clean_cues(raw)
        self.assertTrue(all(current.start >= previous.end for previous, current in zip(cleaned, cleaned[1:])))

    def test_original_vtt_is_never_modified(self) -> None:
        content = vtt(("00:00:00.000", "00:00:02.000", "Text <00:00:00.500><c>appears</c>."))
        temporary, source, result = self._clean(content)
        try:
            self.assertEqual(source.read_text(encoding="utf-8"), content)
            self.assertTrue(Path(result["raw_srt"]).is_file())
            self.assertTrue(Path(result["clean_srt"]).is_file())
        finally:
            temporary.cleanup()

    def test_final_srt_is_natural_continuous_and_non_repeating(self) -> None:
        temporary, _, result = self._clean(vtt(
            ("00:00:00.000", "00:00:01.200", "We have"),
            ("00:00:01.200", "00:00:02.400", "We have good news."),
            ("00:00:02.400", "00:00:02.410", "good news."),
            ("00:00:02.410", "00:00:04.500", "Good news. It finally happened!"),
        ))
        try:
            cues = parse_srt(result["clean_srt"])
            combined = " ".join(cue.text for cue in cues)
            self.assertIn("We have good news.", combined)
            self.assertIn("It finally happened!", combined)
            self.assertNotIn("We have We have", combined)
            self.assertTrue(all(len(cue.text.splitlines()) <= 2 for cue in cues))
            self.assertTrue(all(current.start >= previous.end for previous, current in zip(cues, cues[1:])))
        finally:
            temporary.cleanup()

    def test_chinese_clean_track_uses_english_clean_timeline(self) -> None:
        english = vtt(
            ("00:00:00.000", "00:00:02.000", "First sentence."),
            ("00:00:02.000", "00:00:04.000", "Second sentence."),
        )
        chinese = vtt(
            ("00:00:00.000", "00:00:02.000", "第一句。"),
            ("00:00:02.000", "00:00:04.000", "第二句。"),
        )
        temporary, _, result = self._clean(english, chinese)
        try:
            english_cues = parse_srt(result["clean_srt"])
            chinese_cues = parse_srt(result["zh_clean_srt"])
            self.assertEqual(Path(result["zh_clean_srt"]).name, "zh.youtube.clean.srt")
            self.assertEqual([(cue.start, cue.end) for cue in chinese_cues], [(cue.start, cue.end) for cue in english_cues])
            self.assertIn("第一句", chinese_cues[0].text)
        finally:
            temporary.cleanup()

    def _write_temp(self, content: str) -> Path:
        temporary = tempfile.NamedTemporaryFile(suffix=".vtt", delete=False)
        path = Path(temporary.name); temporary.close()
        path.write_text(content, encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path
