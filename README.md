# YouTube Workflow

这是一个面向 Windows 的 YouTube 视频工作流，覆盖：候选视频发现与版权审核、视频/字幕/音频下载、英文滚动字幕清洗、本地 GPU 英文语音识别，以及 DeepSeek 中文字幕翻译。

项目遵循以下原则：

- 下载前必须经过 `selected` 和 `rights_status` 双重审核；
- 不覆盖 YouTube 下载的原始字幕；
- 本地语音识别只加载项目内模型，不会联网下载模型；
- DeepSeek 密钥只保存在本地 `.env`，不会提交到 Git；
- 下载的视频、音频、本地模型、虚拟环境和生成结果默认不会上传 GitHub。

## 项目目录

```text
youtubeworkflow\
├─config\                       配置文件
├─src\                          Python 源代码
├─tests\                        自动化测试
├─candidates\                   候选视频清单（Git 忽略）
├─downloads\                    下载任务与处理结果（Git 忽略）
├─models\                       本地模型（Git 忽略）
├─private\                      Cookies 等私密文件（Git 忽略）
├─.venv\                        下载与翻译环境（Git 忽略）
├─.venv_stage3\                 本地 GPU 识别环境（Git 忽略）
├─run_stage3.bat                字幕处理主菜单
└─push_to_github.bat            推送当前已提交分支到 GitHub
```

## 首次配置

### 1. 创建本地环境变量文件

复制 `.env.example` 为 `.env`：

```bat
copy .env.example .env
```

编辑 `.env`：

```dotenv
YOUTUBE_API_KEY=你的YouTube API密钥
DEEPSEEK_API_KEY=你的DeepSeek API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

说明：

- 只运行下载时，可以不填写 `DEEPSEEK_API_KEY`；
- 只运行本地 GPU 语音识别时，不需要 DeepSeek 密钥；
- `.env` 已由 `.gitignore` 排除，禁止使用 `git add -f .env` 强制提交。

### 2. 安装下载和翻译环境

项目的常规命令使用 `.venv`：

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements_stage1.txt
.venv\Scripts\python.exe -m pip install -r requirements_stage3.txt
```

下载功能还需要项目本地工具：

```text
tools\yt-dlp.exe
tools\ffmpeg.exe
tools\ffprobe.exe
```

运行以下命令检查下载环境：

```bat
setup_stage2.bat
```

### 3. 安装本地 GPU 语音识别环境

推荐使用 Python 3.11 单独创建环境：

```bat
py -3.11 -m venv .venv_stage3
.venv_stage3\Scripts\python.exe -m pip install --upgrade pip
.venv_stage3\Scripts\python.exe -m pip install -r requirements_stage3.txt
```

需要复现当前 Windows GPU 验证环境的精确版本时，最后一条命令可改为：

```bat
.venv_stage3\Scripts\python.exe -m pip install -r requirements_stage3.lock.txt
```

默认配置使用：

- NVIDIA CUDA；
- `float16`；
- `faster-whisper 1.2.1`；
- `ctranslate2 4.8.1`；
- 英语识别、VAD 和词级时间戳。

把已经准备好的 CTranslate2 格式 `large-v3` 模型放到：

```text
models\faster-whisper-large-v3\
```

目录至少必须包含：

```text
config.json
model.bin
tokenizer.json
vocabulary.json
```

程序会把这个目录解析为绝对路径并直接传给 `WhisperModel`。缺少文件时立即报错，不会使用 `large-v3` 模型名称联网下载。

识别配置位于 `config\stage3_config.json`。除非显卡不支持，否则建议保留：

```json
{
  "asr_device": "cuda",
  "asr_compute_type": "float16",
  "asr_language": "en"
}
```

## 候选发现与版权审核

1. 运行 `setup_stage1_fixed.bat`；
2. 确认 `.env` 已填写 `YOUTUBE_API_KEY`；
3. 运行 `run_fetch_candidates_fixed.bat`；
4. 打开最新的 `candidates\YYYY-MM-DD_US_localization_top50.csv` 或 JSON；
5. 人工审核并设置 `selected` 与 `rights_status`。

允许下载的记录必须同时满足：

- `selected` 为 `1`、`true` 或字符串 `"1"`；
- `rights_status` 为 `APPROVED`、`OWNED`、`LICENSED` 或 `PERMISSION_GRANTED`。

示例：

```json
{
  "video_id": "VIDEO_ID",
  "selected": 1,
  "rights_status": "PERMISSION_GRANTED"
}
```

`PENDING`、`REJECTED` 和 `selected=0` 始终跳过。`--video-ids` 只缩小候选范围，不能绕过审核。

## 下载视频、字幕、元数据与音频

批量下载已审核的候选视频：

```bat
run_stage2_download_selected.bat
```

也可以把候选 JSON 拖到 BAT 上，或使用命令行：

```bat
.venv\Scripts\python.exe src\download_selected_candidates.py
.venv\Scripts\python.exe src\download_selected_candidates.py --input candidates\2026-07-21_US_localization_top50.json
.venv\Scripts\python.exe src\download_selected_candidates.py --video-ids ID1 ID2
```

候选任务保存在：

```text
downloads\candidates\YYYY-MM-DD\排名_VIDEOID_安全标题\
```

每个完整任务通常包含：

```text
download_manifest.json
audio\source_audio.wav
metadata\candidate.json
metadata\description.txt
metadata\info.json
metadata\thumbnail.jpg
subtitles\...
video\source.mp4
```

英文与中文字幕均采用“人工字幕优先、自动字幕回退”。原始字幕保留为备份，清洗过程不会覆盖它们。

### 自由 URL 下载

双击 `run_download_url.bat`，阅读版权提示、输入 URL，并输入 `Y` 确认。也可以运行：

```bat
.venv\Scripts\python.exe src\download_video.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --output downloads\manual --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --metadata-only --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --subtitles-only --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --no-audio-extract --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --force --confirm-rights
```

自由下载结果位于：

```text
downloads\manual\YYYY-MM-DD\VIDEOID_安全标题\
```

所有下载均强制单视频模式。成功任务重复运行会跳过，部分完成任务只补缺失文件。

### 修复失败的下载

双击：

```text
run_repair_failed_downloads.bat
```

或执行：

```bat
.venv\Scripts\python.exe src\repair_failed_downloads.py --dry-run
.venv\Scripts\python.exe src\repair_failed_downloads.py
.venv\Scripts\python.exe src\repair_failed_downloads.py --video-ids ID1 ID2
.venv\Scripts\python.exe src\repair_failed_downloads.py --attempts 5 --retry-delay 5
```

修复程序只处理失败或关键文件缺失的任务，并重新验证版权审核字段。

## 字幕处理主菜单

双击 `run_stage3.bat`。当出现：

```text
Video task directory:
```

输入以下任意一种目录：

- 单个视频任务目录：目录中直接存在 `download_manifest.json`；
- 日期目录：程序会递归处理其中所有带 `download_manifest.json` 的视频任务。

菜单功能如下：

| 选项 | 作用 | 是否调用付费 API |
|---|---|---|
| 1 | 清洗并重建英文字幕 | 否 |
| 2 | 检查翻译配置、字幕数量和预计批次数 | 否 |
| 3 | 把最终选中的英文字幕翻译成中文，支持断点续跑 | 是 |
| 4 | 先清洗英文字幕，再翻译成中文 | 是 |
| 5 | 重新翻译并润色全部中文字幕 | 是 |
| 6 | 忽略翻译断点，从头重新翻译 | 是 |
| 7 | 使用本地 GPU 识别每个任务的前 30 秒 | 否 |
| 8 | 自动评估并选择英文字幕；现有字幕低分时完整运行 Whisper | 否 |
| 9 | 自动评估并选择英文字幕；必要时运行 Whisper，然后翻译中文 | 是 |

所有付费操作都会显示提示，并且只有输入 `YES` 才会开始调用 DeepSeek API。

也可以直接指定 BAT 模式：

```bat
run_stage3.bat "downloads\candidates\2026-07-21" clean
run_stage3.bat "downloads\candidates\2026-07-21" check
run_stage3.bat "downloads\candidates\2026-07-21" translate
run_stage3.bat "downloads\candidates\2026-07-21" full
run_stage3.bat "downloads\candidates\2026-07-21" polish
run_stage3.bat "downloads\candidates\2026-07-21" retranslate
run_stage3.bat "单个视频任务目录" asr30
run_stage3.bat "视频任务目录或日期目录" autoselect
run_stage3.bat "视频任务目录或日期目录" autotranslate
```

推荐的一键入口：

- 只需要最终英文字幕：选择 `8`；
- 需要最终英文字幕并继续翻译中文：选择 `9`，然后输入 `YES` 确认付费调用。

选项 `8` 实际执行 `--steps select --subtitle-source auto`。选项 `9` 先执行同一个自动选源命令；成功后显示选源摘要，再要求输入 `YES`，最后执行 `--steps translate --allow-paid-api`。两者都使用 `.venv_stage3`，不会先错误执行一次独立英文清洗；选源流程会自行清洗最终选中的人工字幕或 YouTube 字幕。

## 本地 GPU 英文语音识别

### 第一次先验证 30 秒

最简单的方法是运行 `run_stage3.bat` 并选择 `7`。

等价命令：

```bat
.venv_stage3\Scripts\python.exe src\run_stage3.py ^
  --video-dir "单个视频任务目录" ^
  --steps asr ^
  --subtitle-source whisper ^
  --asr-max-seconds 30 ^
  --force
```

该命令不会裁剪或修改源音频，只限制识别到第 30 秒。菜单测试模式带有 `--force`，因此每次选择 `7` 都会重新运行测试。

### 识别完整视频

30 秒验证成功后，去掉时间限制和强制参数：

```bat
.venv_stage3\Scripts\python.exe src\run_stage3.py ^
  --video-dir "单个视频任务目录" ^
  --steps asr ^
  --subtitle-source whisper
```

重复执行相同命令时，如果音频哈希、模型配置和输出文件都匹配，程序会复用成功检查点。需要从头重新识别时添加：

```text
--force
```

当前恢复粒度是“一个视频任务”，不是伪装的音频分块断点。

### 自动选择字幕来源

```bat
.venv_stage3\Scripts\python.exe src\run_stage3.py ^
  --video-dir "视频任务目录或日期目录" ^
  --steps asr ^
  --subtitle-source auto
```

自动选择顺序：

1. 人工英文字幕评分达到 70：使用人工字幕；
2. 否则，YouTube 英文字幕评分达到 65：使用 YouTube 字幕；
3. 两者都不满足：运行本地 `faster-whisper`；
4. 最终结果统一写入 `subtitles\en.selected.srt`。

也可以强制指定：

```text
--subtitle-source manual
--subtitle-source youtube
--subtitle-source whisper
```

强制指定的来源不存在时，程序会明确报错，不会静默换成其他来源。

### 音频来源优先级

程序按以下顺序选择输入：

1. `audio\source_audio.wav`；
2. 任务中的 M4A；
3. 任务中的 MP3；
4. `video\source.mp4` 或其他可用视频。

识别前后都会计算输入文件 SHA256，源音频和视频不会被改写。

### 识别完成后翻译中文

推荐分两步使用不同环境：

1. 使用 `.venv_stage3` 完成本地英文识别；
2. 再运行 `run_stage3.bat`，输入同一任务目录并选择 `3`。

翻译程序会优先读取 `subtitles\en.selected.srt`。只有第二步会调用 DeepSeek API。

## 字幕与报告文件

单个任务的重要输出如下：

```text
subtitles\en.auto.raw.srt          YouTube 原始英文字幕备份
subtitles\en.clean.srt             已清洗的 YouTube/人工英文字幕
subtitles\en.whisper.raw.srt       本地模型的原始识别片段
subtitles\en.whisper.clean.srt     重新断句和修复时间轴后的识别字幕
subtitles\en.selected.srt          最终选中的统一英文输入
subtitles\zh.raw.srt               DeepSeek 原始翻译结果
subtitles\zh.clean.srt             最终推荐使用的中文字幕
stage3\source_comparison.json      字幕源评分、最终选择和选择原因
stage3\asr\asr_info.json          模型、GPU、语言、耗时和音频哈希
stage3\asr\asr_raw_segments.json  原始识别片段
stage3\asr\asr_words.json         词级时间戳和置信度
stage3\asr\asr_clean_segments.json 重建后的字幕片段
stage3\asr\asr_qc.json            机器可读质量报告
stage3\asr\asr_qc.txt             人工可读质量报告
stage3\asr\asr_checkpoint.json    任务级成功检查点
translation\api_usage.json         DeepSeek 使用量
translation\subtitle_qc.json       中文翻译质量检查
translation\checkpoints\           翻译批次断点
stage3_manifest.json                整体处理状态
```

识别质量检查包含空段、非法时间、倒序、重叠、过短/过长、字符速度、低置信度词、词时间缺失、覆盖率、重复短语和结尾截断等指标。

## 常见问题

### 提示 `subtitles/en.clean.srt does not exist`

翻译前还没有可用的英文字幕。任选一种处理方式：

- 重新运行 BAT 并选择 `1`；
- 需要直接清洗并翻译时选择 `4`；
- 没有可靠英文字幕时，先运行本地 GPU 识别，再选择 `3` 翻译。

### 提示缺少 `.venv_stage3`

执行：

```bat
py -3.11 -m venv .venv_stage3
.venv_stage3\Scripts\python.exe -m pip install -r requirements_stage3.txt
```

### 提示模型目录不完整

检查以下文件是否都存在：

```text
models\faster-whisper-large-v3\config.json
models\faster-whisper-large-v3\model.bin
models\faster-whisper-large-v3\tokenizer.json
models\faster-whisper-large-v3\vocabulary.json
```

不要把配置改成裸模型名称 `large-v3`，否则程序会拒绝启动。

### 提示缺少 CUDA DLL 或看不到 GPU

确认：

- NVIDIA 驱动正常，`nvidia-smi` 可以识别显卡；
- 命令使用的是 `.venv_stage3\Scripts\python.exe`；
- `faster-whisper`、`ctranslate2`、CUDA runtime 和 cuDNN 依赖安装在同一个环境；
- 没有在导入 `ctranslate2` 前绕过项目的 CUDA 注册模块。

### 质量报告通过但存在字符速度警告

字符速度属于可读性提示。空段、非法时间、倒序、重叠和重复等核心问题为零时，报告仍可通过。可以打开 `stage3\asr\asr_qc.txt` 查看具体字幕编号。

## 运行测试

完整测试：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

本地语音识别相关自动化测试使用 mock，不需要 GPU、不访问网络，也不会调用付费 API。

检查源代码语法：

```bat
.venv\Scripts\python.exe -m compileall -q src tests
.venv_stage3\Scripts\python.exe -m compileall -q src
```

## 上传 GitHub

`.gitignore` 已排除以下内容：

- `.env`、Cookies、私钥和本地凭据；
- `.venv`、`.venv_stage3` 和 Python 缓存；
- `downloads`、视频、音频、字幕和运行日志；
- `models` 以及常见大模型权重；
- 翻译断点、识别报告和临时文件。

创建提交后，可以双击：

```text
push_to_github.bat
```

它只把当前已经提交的分支推送到 `origin`，不会自动执行 `git add` 或创建新提交。GitHub 首次认证时，Windows 可能弹出登录窗口。

等价命令：

```bat
git push origin main
```

上传前可自行复查：

```bat
git status
git status --ignored --short
git ls-files .env models downloads .venv .venv_stage3
```

最后一条命令应当没有输出。不要使用 `git add -f` 强制添加被忽略的私密文件或大文件。
