from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.stage4.subtitle_recovery import clip_recovered_pair_to_video_duration
from src.stage4.subtitle_validator import parse_srt_strict, validate_subtitles


ENGLISH = """1
00:00:00,000 --> 00:00:01,000
Hello.

2
00:00:01,500 --> 00:00:03,000
Last sentence.
"""
CHINESE = """1
00:00:00,000 --> 00:00:01,000
你好。

2
00:00:01,500 --> 00:00:03,000
最后一句。
"""


class SubtitleRecoveryTests(unittest.TestCase):
    def test_recovered_pair_is_clipped_to_real_video_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            english = root / "en.recovered.srt"
            chinese = root / "zh.recovered.srt"
            english.write_text(ENGLISH, encoding="utf-8")
            chinese.write_text(CHINESE, encoding="utf-8")
            report = clip_recovered_pair_to_video_duration(
                english,
                chinese,
                2.5,
            )
            self.assertTrue(report["applied"])
            self.assertEqual(report["clipped_segment_count"], 1)
            self.assertTrue(
                validate_subtitles(
                    english,
                    chinese,
                    video_duration=2.5,
                ).passed
            )
            cues, _ = parse_srt_strict(english)
            self.assertEqual(cues[-1].end, 2.5)


if __name__ == "__main__":
    unittest.main()
