from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase

from src.run_stage3 import print_source_selection_summary


ROOT = Path(__file__).resolve().parents[2]


class Stage3EntrypointTests(TestCase):
    def test_batch_auto_modes_use_select_and_stage3_environment(self) -> None:
        content = (ROOT / "run_stage3.bat").read_text(encoding="utf-8")
        self.assertIn('if /I "%RUN_MODE%"=="autoselect"', content)
        self.assertIn('set "RUN_ARGS=--steps select --subtitle-source auto --resume"', content)
        self.assertIn('if /I "%RUN_MODE%"=="autotranslate"', content)
        self.assertGreaterEqual(
            content.count('set "RUN_ARGS=--steps select --subtitle-source auto --resume"'),
            2,
        )
        self.assertIn('set "TRANSLATE_AFTER_SELECT=1"', content)
        self.assertIn('set "RUN_ARGS=--steps translate --resume --allow-paid-api"', content)
        self.assertGreaterEqual(content.count('set "STAGE3_PYTHON=.venv_stage3\\Scripts\\python.exe"'), 3)
        self.assertIn('set "PAID_MODE=1"', content)

    def test_chinese_source_summary_contains_all_required_fields(self) -> None:
        task = Path("C:/video/task")
        selection = {
            "whisper_started": True,
            "selected_path": str(task / "subtitles" / "en.selected.srt"),
            "selection_report": {
                "youtube": {"path": "youtube.clean.srt", "source_type": "auto", "final_score": 64},
                "whisper": {"path": "whisper.clean.srt", "final_score": 98},
                "agreement_score": 75,
                "selected_source": "whisper",
                "selected_output_path": str(task / "subtitles" / "en.selected.srt"),
                "selection_reason": "字幕评分不足，回退到本地 Whisper",
                "review_required": False,
            },
        }
        output = io.StringIO()
        with redirect_stdout(output):
            print_source_selection_summary(task, selection)
        text = output.getvalue()
        for label in (
            "YouTube clean 路径：youtube.clean.srt",
            "YouTube 来源类型：auto",
            "YouTube 六维总分：64.00",
            "Whisper clean 路径：whisper.clean.srt",
            "是否启动 Whisper：是",
            "Whisper 六维总分：98.00",
            "双源一致度：75.00",
            "最终字幕来源：Whisper",
            "选择原因：字幕评分不足，回退到本地 Whisper",
            "en.selected.srt 路径：",
            "selection_report.json 路径：",
        ):
            self.assertIn(label, text)
