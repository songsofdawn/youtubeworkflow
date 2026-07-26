from __future__ import annotations

from unittest import TestCase

from src.stage3.publish_metadata import (
    build_publish_description,
    compose_bilingual_title,
    load_category_mapping,
    normalize_ai_recommendation,
    normalize_tags,
    truncate_utf8,
    truncate_utf16,
    utf8_bytes,
    utf16_code_units,
)


class PublishMetadataTests(TestCase):
    def test_official_category_mapping_has_unique_specific_tids(self) -> None:
        mapping = load_category_mapping()
        tids = [row["tid"] for row in mapping["categories"]]
        self.assertGreaterEqual(len(tids), 100)
        self.assertEqual(len(tids), len(set(tids)))
        computer = next(row for row in mapping["categories"] if row["tid"] == 231)
        self.assertEqual(computer["path"], "科技 / 计算机技术")

    def test_ai_recommendation_builds_bilingual_title_and_normalized_tags(self) -> None:
        mapping = load_category_mapping()
        result = normalize_ai_recommendation(
            {
                "chinese_title": "如何构建可靠的软件系统",
                "tags": ["软件工程", "#系统设计", "软件工程"],
                "tid": 231,
                "reason": "内容主要讨论软件系统设计。",
            },
            {
                "title": "How to Build Reliable Software Systems",
                "tags": ["Programming"],
            },
            mapping,
        )
        self.assertEqual(result["category_path"], "科技 / 计算机技术")
        self.assertTrue(
            result["upload_title"].startswith("【中英双语】如何构建可靠的软件系统｜")
        )
        self.assertEqual(
            result["tags"],
            "中英双语,中文翻译,软件工程,系统设计,Programming",
        )

    def test_title_and_description_respect_bilibili_limits(self) -> None:
        title = compose_bilingual_title("这是一个很长但准确的中文标题" * 3, "English title " * 10)
        description = build_publish_description(
            "original " * 1000,
            disclaimer="【免责声明】\n测试声明",
            original_heading="【原视频简介】",
        )
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.startswith("【中英双语】"))
        self.assertLessEqual(utf16_code_units(description), 2000)
        self.assertLessEqual(utf8_bytes(description), 1900)
        self.assertIn("【原视频简介】", description)

    def test_description_limit_matches_bilibili_utf16_counting(self) -> None:
        description = build_publish_description(
            "🧰💯" + ("original " * 1000),
            disclaimer="【免责声明】\n测试声明",
            original_heading="【原视频简介】",
        )
        self.assertLessEqual(utf16_code_units(description), 2000)
        shortened = truncate_utf16("a🧰b", 3)
        self.assertEqual(shortened, "a…")
        self.assertLessEqual(utf16_code_units(shortened), 3)
        self.assertEqual(truncate_utf8("中abcd", 6), "中…")

    def test_tags_are_comma_separated_deduplicated_and_bounded(self) -> None:
        tags = normalize_tags(["#Roblox", "Roblox", "非常长的标签" * 10])
        rows = tags.split(",")
        self.assertEqual(rows[:3], ["中英双语", "中文翻译", "Roblox"])
        self.assertLessEqual(len(rows), 10)
        self.assertTrue(all(len(row) <= 20 for row in rows))
