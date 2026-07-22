from __future__ import annotations

from pathlib import Path

from src.stage3.cuda_runtime import configure_cuda_runtime

# 项目根目录：test_cuda_faster_whisper.py 所在目录
PROJECT_ROOT = Path(__file__).resolve().parent

# 必须在导入 ctranslate2 和 faster_whisper 前执行
CUDA_RUNTIME = configure_cuda_runtime()

import ctranslate2
from faster_whisper import WhisperModel


MODEL_DIR = (
    PROJECT_ROOT
    / "models"
    / "faster-whisper-large-v3"
)

AUDIO_PATH = (
    PROJECT_ROOT
    / "work"
    / "faster_whisper_test.wav"
)


def check_model_files() -> None:
    required_files = [
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.json",
    ]

    missing_files = [
        filename
        for filename in required_files
        if not (MODEL_DIR / filename).is_file()
    ]

    if missing_files:
        raise FileNotFoundError(
            f"large-v3模型目录不完整：{MODEL_DIR}\n"
            f"缺少文件：{', '.join(missing_files)}"
        )


def main() -> None:
    print("项目根目录：", PROJECT_ROOT)
    print("CTranslate2版本：", ctranslate2.__version__)
    print("可见GPU数量：", ctranslate2.get_cuda_device_count())
    print(
        "CUDA计算类型：",
        ctranslate2.get_supported_compute_types("cuda"),
    )

    check_model_files()

    print("正在从本地加载large-v3……")
    print("模型目录：", MODEL_DIR)

    model = WhisperModel(
        str(MODEL_DIR),
        device="cuda",
        compute_type="float16",
    )

    print("large-v3已成功加载到GPU。")

    # 没有测试音频时，只验证模型加载
    if not AUDIO_PATH.is_file():
        print()
        print("未找到30秒测试音频：")
        print(AUDIO_PATH)
        print("模型加载测试已经通过，暂不执行音频转写。")
        return

    print()
    print("开始转写测试音频：")
    print(AUDIO_PATH)

    segments, info = model.transcribe(
        str(AUDIO_PATH),
        language="en",
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )

    print(
        f"识别语言：{info.language}；"
        f"语言置信度：{info.language_probability:.4f}"
    )

    segment_count = 0

    # segments是生成器，必须迭代才会真正执行识别
    for segment in segments:
        segment_count += 1

        print(
            f"[{segment.start:7.2f} -> {segment.end:7.2f}] "
            f"{segment.text.strip()}"
        )

    print()
    print(f"共识别出 {segment_count} 个片段。")
    print("large-v3 GPU转写测试成功。")


if __name__ == "__main__":
    main()
