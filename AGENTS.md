# YouTube Workflow 项目智能体指南

这是本项目的智能体工作说明（项目约定文件名为 `AGENTS.md`，等同于用户要求的
`agent.md`）。任何修改前先读完本文件，再只打开与任务相关的源码、配置、测试和文档。
除非任务明确要求，不要扫描或读取 `downloads/`、`models/`、`dist/`、虚拟环境、日志、
生成字幕、`work/` 和归档长文档。

## 1. 项目目标与工作边界

这是一个 Windows 本地 YouTube 本地化工作流：

```text
URL / 关键词 / 智能发现
        ↓
下载源视频、源音频、字幕、封面和元数据
        ↓
YouTube 英文字幕 ↔ faster-whisper 英文识别与评分选择
        ↓
AI 翻译或 YouTube 中文字幕
        ↓
可选 Demucs + VoxCPM2 单主播中文配音
        ↓
ASS / 软字幕 MKV / 硬字幕 MP4
        ↓
人工确认后可选 biliup 投稿
```

默认原则是保留原始素材、保留 ID 和时间轴、可从检查点续跑，并且不在没有明确授权时
下载或调用付费 API。项目面向本机使用，控制面板只能监听 `127.0.0.1` 或 `localhost`。

工作树可能已有用户私下修改、未提交文件或删除项。先执行 `git status --short` 了解现状，
把这些变化视为用户资产；不要 reset、checkout、clean、批量格式化或改写与任务无关的文件。
本项目文件编辑使用 `apply_patch`；不要用 shell 重定向、`cat`、临时脚本覆盖文件。

## 2. 源码与配置的事实来源

| 功能 | 先读这些文件 | 主要持久化产物 |
|---|---|---|
| 控制面板后端与队列 | `src/control_panel/app.py`、`server.py`、`jobs.py`、`tasks.py` | `work/control_panel/control_panel.sqlite3`、作业日志 |
| 控制面板前端 | `src/control_panel/static/index.html`、`app.js`、`styles.css`、`discovery_upgrade.js`、`discovery_upgrade.css` | 无 |
| YouTube 搜索/发现 | `src/control_panel/youtube.py`、`src/discovery/pipeline.py`、`src/discovery/ollama_client.py`、`src/discovery/store.py` | `work/discovery/discovery.sqlite3`、发现结果 JSON |
| 发现配置 | `config/trending_config.json`、`config/discovery_keywords.json` | `work/discovery_history.json` |
| 下载 | `src/download_core.py`、`src/download_video.py`、`src/download_selected_candidates.py`、`src/repair_failed_downloads.py` | `downloads/.../download_manifest.json` |
| 英文字幕、翻译、人工审核 | `src/stage3/pipeline.py`、`config/stage3_config.json`、`src/stage3/config_adapter.py`、`src/stage3/review_workflow.py` | `stage3_manifest.json`、`stage3/`、`subtitles/` |
| 翻译供应商 | `src/stage3/llm_providers.py`、`src/stage3/translator_deepseek.py` | `stage3/translation/`、翻译检查点和使用量 |
| 成片 | `src/stage4/render_pipeline.py`、`src/stage4/bilingual_ass.py`、`src/stage4/input_resolver.py`、`src/stage4/layout_review.py`、`config/stage4_config.json` | `stage4/stage4_manifest.json`、ASS、MKV、MP4 |
| 中文配音 | `src/dubbing/`、`src/run_dubbing.py`、`src/control_panel/dubbing_worker.py`、`config/dubbing_config.json` | `dubbing/manifest.json`、WAV |
| 投稿 | `src/control_panel/publishing.py`、`config/publish_config.json`、`config/bilibili_categories.json` | `stage5/` 投稿与自动化记录 |
| 验证 | `tests/`、`verify_project.bat` | 无 |

`translator_deepseek.py`、`DeepSeekTranslator` 和内部值 `deepseek` 是历史兼容名称，当前
代表供应商中立的 AI 翻译路径。不要仅为改名而迁移它们；旧检查点、BAT、测试和任务扫描器
依赖这些名称。

## 3. 当前运行架构

### 控制面板与调度

- `START_HERE.bat` 和 `start_panel.bat` 最终启动 `src/run_control_panel.py`；`set_runtime.bat`
  按 `runtime/python/python.exe`、`.venv/Scripts/python.exe`、历史 `.venv_stage3` 顺序选运行时。
- 面板异步处理下载、字幕、翻译、发现、配音、成片和投稿。发现通过 `POST /api/discover`
  返回 202 作业，再由 `/api/discovery/result?job_id=...` 读取结果。
- 默认资源槽是 2 个网络下载、2 个付费 API、1 个 GPU heavy、1 个上传；全局同时运行上限
  为源码/GPU 版 4 个进程、Portable CPU 版 3 个进程。不要绕过 `gpu_heavy`、`paid_api` 或
  上传冷却来“提高并发”。
- 面板接口会校验本地来源、JSON 大小和路径范围；健康接口只能返回是否配置/就绪等布尔或
  脱敏状态，不能回显密钥、Cookie 或账号内容。

### 智能发现 V4

- 领域目录来自 `config/discovery_keywords.json`；当前有 14 个可编辑领域。查询字符串以
  `|` 分隔，第 1 项是宽泛主查询，后最多 3 项是补充查询。关键词只用于弱相关性评分，
  不参与 YouTube 召回，也不作为硬过滤条件。
- 主查询按 `viewCount`、`date`、`relevance` 召回，补充查询默认只按 `viewCount` 召回；
  精确时长在 `videos.list` 元数据阶段过滤。自适应第 2 页只在候选不足时触发，并受每领域
  最大调用数和总搜索上限约束。当前 `trending_config.json` 的总搜索上限为 96、基础召回目标
  为 1000、每领域最多返回 100 条。
- `hot` 是默认排序：独立 Hot Recall Lane 加热门保护；达到当前时间窗口的播放量或 VPH
  阈值的视频不能被普通 Qwen reject/机会分门槛轻易淘汰。`potential` 以元数据内容质量和
  本地化潜力为主，但真正热门候选仍受保护。不要把这两条通道合并成单一分数。
- 默认本地模型是 Ollama `qwen3.5:9b` 和 Embedding `qwen3-embedding:0.6b`；当前配置默认
  关闭 AI 查询词规划，视觉复评和 Embedding 可分别开关。Ollama 不可用时允许退回规则评分。
  Ollama 只能收到 YouTube 公开元数据和缩略图，不得收到 API Key、Cookie、本地视频或字幕。
- 反馈接口允许 `interested`、`selected`、`boring`、`irrelevant`、`duplicate`、
  `wrong_language`、`unsafe`，最新反馈覆盖旧反馈，并用于后续排序。发现结果始终只是候选，
  选中后仍必须经过下载权利闸门。

项目还保留 `src/fetch_daily_candidates.py` 的传统日报候选流程，以及
`src/download_selected_candidates.py` / `src/repair_failed_downloads.py`。它们写入
`candidates/YYYY-MM-DD_US_localization_top50.*`，下载前必须同时满足候选记录
`selected=1` 和 `rights_status` 为 `APPROVED`、`OWNED`、`LICENSED` 或
`PERMISSION_GRANTED`。不要把日报候选流程误当成面板智能发现，也不要自动替用户批准候选。

### 下载

- 直接 URL / 视频 ID 和候选下载都最终调用 `download_core.download_one_video`，普通任务在
  `downloads/manual/`，候选任务在 `downloads/candidates/`，按日期和视频 ID/标题建目录。
- 默认最高 1080p，保存 `video/source.mp4`、`audio/source_audio.wav`、原始 VTT/SRT、
  `metadata/info.json`、简介、缩略图和 `download_manifest.json`。下载失败可以是
  `partial_success`，应依据 manifest 续跑，而不是删除已有源文件。
- Cookies 只接受 Netscape 格式，默认位置是 `private/cookies.txt`。HTTP 403 时会按配置尝试
  `web_embedded` PO token 路径和 CDN 续传；日志、命令和接口必须继续脱敏。
- 任何下载入口都需要权利确认；不要降低 `selected` / `rights_status` 校验，不要把搜索结果
  或 YouTube 字幕存在误认为下载、翻译、改编或投稿授权。

### Stage 3：英文来源、翻译与审核

- P0 清理 YouTube 手工/自动英文 VTT/SRT、去滚动字幕噪声并重建句段；P2 结合 YouTube 和
  faster-whisper large-v3 进行结构、时间轴、覆盖率、稳定性、可读性和来源可信度评分，写入
  `subtitles/en.selected.srt` 与 selection report。默认 YouTube 自动英文存在时仍运行 Whisper
  对比；关闭选项只影响对比，不影响没有英文来源时的 Whisper 兜底。
- P1 只做结构性验证：JSON、ID 集合、非空译文、时间轴、合法控制字符和单 ID 内容溢出。
  翻译措辞质量由用户选择的模型负责；不能新增“翻译腔”、英文残留、主观听感或本地模型
  语义 QC，也不能按这些判断自动购买第二遍翻译。
- 默认翻译是 Thinking 关闭、动态生产批次 64–96、前后各 2 条只读上下文、最大输出 4096，
  软目标约 4500 提示词 Token。最后尾批、响应恢复和降级请求可以小于 64；降级批次默认 16。
  `--polish-all` 是唯一正常的整批第二次翻译入口，不能隐式开启。
- `429`（包括智谱 `1305`）必须在当前批次检查点写盘后做显式长退避
  5/15/30/60/120 秒加抖动；OpenAI SDK 的隐藏重试保持关闭。响应为空、JSON 截断、ID 缺失
  或污染时，只重试 pending IDs，必要时隔离为单 ID、关闭 native JSON/Thinking 并缩小请求。
  不得自动切换到另一个可能收费的供应商。
- `--for-dubbing` 会生成统一的单主播中文脚本和 canonical JSON；只有明确允许付费 API 时，
  才能做配音边界修复或时长辅助重写。人工审核 TSV 导入应生成 `zh.reviewed.srt`，不要覆盖
  `zh.clean.srt` 的原始结构结果。

### Stage 4：排版与编码

- 默认一行英文加一行中文，英文在上、中文在下；渲染时折叠源 SRT 换行、使用单一分隔符、
  禁止 ASS 自动换行。1080p 目标字号为中文 60px / 英文 44px，可发布下限为 54px / 40px；
  字号按画面高度缩放，不得恢复旧的宽高比放大或 24/20px 小字号。
- 长句先用自然分页，再在必要时降到发布下限并做轻微水平压缩。若仍会裁切、分页导致闪读、
  时间过短或无法保持每种语言一行，必须写预览/QC 并返回 `REVIEW_REQUIRED`，在 FFmpeg 前
  停止；禁止输出已知裁切、过小或闪烁的坏成片。
- 黄色“审”按钮产生 `stage4` 专用 layout-reviewed 字幕副本，只影响成片排版，不得修改原始
  英文、原始中文、canonical 文本、ID 或时间轴。修复后必须重新预检。
- `hardsub` 失败/需要复核时不能冒充完成。自动投稿应读取
  `stage4/stage4_manifest.json` 的 `hardsub_output_path`，不能按文件名猜测实际投稿文件。
  软字幕默认 MKV；硬字幕默认 MP4；编码器 `auto` 优先 NVENC，不可用时回退 libx264。

### 中文配音与投稿

- 配音入口是 `src/run_dubbing.py`，使用独立 `.venv_dubbing`、本地 VoxCPM2 和 Demucs。输入
  只能是 `subtitles/zh.reviewed.srt`（优先）或 `subtitles/zh.clean.srt`；配音阶段不得运行
  Whisper，也不得直接把 YouTube 自动中文当成 TTS 脚本。
- V1 只支持单主播、单参考片段、单音色；不做说话人分离、多人角色映射、lip sync、自动情绪
  判断或主观听感 QC。TTS 逐句写 `dubbing/manifest.json`，只重做缺失或输入哈希变化的句子。
  默认 `performance.keep_voxcpm_warm=false`；只有配置明确打开时才使用常驻 JSONL worker。
- Demucs 分离、静音裁剪、连续区域时长调度、无重叠拼接和响度处理都应保留。时长适配上限由
  配置控制，当前最大拉伸为 1.15；超过限制或无法安全对齐应返回需要复核，不得静默截断或
  叠加句子。配音相关辅助 AI 也必须走显式 `--allow-paid-api`。
- 投稿需要 biliup 登录、硬字幕 MP4 和人工确认的账号/版权/可见性信息。当前发布保护默认是
  最短间隔 60 秒、每日成功 50 条、平台频率码 `137022` 冷却 24 小时；不要绕过冷却或重复
  投稿。自动投稿以 manifest 记录为准，并保留原始视频和真实的配音/字幕说明。

## 4. 配置、密钥与兼容性

- `.env.example` 是密钥模板；密钥只放项目根 `.env`，不得出现在源码、README、日志、测试
  夹具、返回值或补丁中。面板设置只写 allowlist 环境变量，并只展示 configured 布尔值。
- 当前翻译供应商由 `TRANSLATION_PROVIDER` 选择，目录和模型只在
  `src/stage3/llm_providers.py` 声明：DeepSeek、智谱 GLM、阿里云 Qwen、Kimi、MiniMax、
  豆包、OpenAI、Anthropic 和自定义 OpenAI 兼容接口。Anthropic 使用原生 `/v1/messages`；
  其他兼容接口使用 OpenAI client。添加供应商时同时更新 registry、面板 catalog、
  `.env.example`、README 和离线测试，绝不写真实 Key。
- 面板可编辑 provider/model/base URL/thinking/batch/context/max output，以及 Ollama 发现设置；
  动态批次的 `TRANSLATION_DYNAMIC_BATCH`、`TRANSLATION_BATCH_MIN/MAX` 和
  `TRANSLATION_BATCH_TARGET_TOKENS` 也要保持与 `.env.example`、`config/stage3_config.json`
  的行为一致。不要把旧的 `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` 兼容字段随意删除。
- `download_manifest.json`、`stage3_manifest.json`、`stage4/stage4_manifest.json`、
  `dubbing/manifest.json` 及翻译检查点都是续跑协议。变更字段、路径或哈希策略前先检查迁移
  与旧测试，保留旧 artifact 名称；源视频、源音频、原字幕和 timeline 不得被覆盖。
- `downloads/`、`candidates/`、`private/`、`logs/`、`work/`、`models/`、`tools/`、`biliup/`、
  `dist/` 和虚拟环境都是本地或生成内容，不要提交。删除任务属于不可恢复操作，只有用户明确
  请求且目标已经解析并限制在项目任务目录内时才可执行。

## 5. 修改路线与验证

先用 `rg` 定位引用和测试契约，再读入口及其直接依赖；不要为了“了解项目”读取大文件产物。
代码变更后使用项目运行时，不要假定系统 Python 已安装依赖：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m compileall -q src tests
node --check src\control_panel\static\app.js
git diff --check
```

翻译、发现、面板或配音改动至少运行对应的聚焦测试；所有测试必须离线，mock LLM、YouTube、
FFmpeg 和投稿调用。只有用户明确要求或本地依赖齐全时才运行真实下载、模型、编码或投稿。

推荐的聚焦测试：

```bat
.venv\Scripts\python.exe -m unittest tests.stage3.test_translator tests.stage3.test_stage3_pipeline tests.test_control_panel
.venv\Scripts\python.exe -m unittest tests.test_discovery tests.test_download_stage2 tests.stage4.test_stage4_pipeline tests.dubbing.test_dubbing_core
```

BAT 入口与 Portable 相关改动还要检查 `set_runtime.bat`、`verify_project.bat`、
`build_portable.ps1` 和 `PORTABLE_README.md`。文档改动至少执行 `git diff --check`，并检查
示例路径、默认值、命令和当前配置没有互相矛盾。

## 6. 文档分工

- `README.md`：面向使用者的当前安装、面板流程、发现、供应商、输出、排错、安全和投稿说明。
- `PORTABLE_README.md`：Portable CPU/GPU 包的独立使用指南，不把源码版专属步骤硬塞进去。
- `AGENTS.md`：面向后续智能体的边界、事实来源、不可破坏约束和验证方式；保持短而可执行。

如果用户只要求文档更新，不要顺手重构代码、整理私有改动或清理生成目录；交付时说明实际
修改的文档和验证结果，并注明无关工作树变化已保留。
