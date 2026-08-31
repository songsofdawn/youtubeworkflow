# YouTube Workflow

面向 Windows 的本地 YouTube 视频双语化工作流：从视频发现与下载，到英文字幕选择、
Whisper 识别、AI 中文字幕、可选的单主播中文 AI 配音、成片，再到可选的哔哩哔哩投稿，
都在一个本地控制面板中完成。

```text
YouTube URL / 关键词 / 智能发现
                ↓
      下载原视频、音频、字幕与元数据
                ↓
  YouTube 英文字幕 ↔ 本地 Whisper 识别
                ↓
      AI 翻译 / YouTube 自动中文
                ↓
  可选 Demucs + VoxCPM2 中文配音
                ↓
 ASS / 软字幕 MKV / 硬字幕 MP4
                ↓
        人工确认后投稿哔哩哔哩
```

这个项目优先保证三件事：原始素材不被覆盖、耗时步骤可以断点续跑、任何下载和 API
调用都必须经过明确授权。

[便携版用户指南](PORTABLE_README.md) ·
[下载 Releases](https://github.com/songsofdawn/youtubeworkflow/releases) ·
[第三方组件说明](THIRD_PARTY_NOTICES.md)

快速导航：
[选择版本](#应该使用哪个版本) ·
[源码安装](#源码安装) ·
[首次配置](#首次配置) ·
[完整工作流](#从下载到成片) ·
[无人值守](#无人值守自动化) ·
[哔哩哔哩投稿](#投稿哔哩哔哩) ·
[输出文件](#输出文件) ·
[常见问题](#常见问题)

## 应该使用哪个版本

| 你的情况 | 推荐版本 | 说明 |
|---|---|---|
| 只想直接使用软件 | Portable CPU | 无需安装 Python；兼容没有 NVIDIA 显卡的电脑 |
| 有 NVIDIA 显卡，希望加快 Whisper | Portable GPU | 自带 CUDA/cuDNN 运行库，但仍需要兼容的 NVIDIA 驱动 |
| 需要修改代码、配置或参与开发 | 源码版 | 使用项目 `.venv`，本地工具与 Whisper 模型需单独准备 |

普通用户建议下载 Portable 压缩包，完整解压后双击 `START_HERE.bat`。便携版的 CPU/GPU
选择、首次配置和日常操作见 [PORTABLE_README.md](PORTABLE_README.md)。

## 核心能力

- 直接粘贴单个或多个 YouTube URL / 视频 ID；
- 使用 YouTube Data API 关键词搜索，或用 Ollama 做本地智能发现、排序和语义去重；
- 下载最高 1080p 的源视频，并保留源音频、原字幕、封面、简介、视频 ID 与下载记录；
- 在 YouTube 手工英文、YouTube 自动英文和本地 faster-whisper large-v3 之间选择；
- 支持多家 AI API，按批保存翻译检查点，失败后只继续未完成字幕；
- 可选使用 Demucs 分离人声、VoxCPM2 克隆单主播音色并生成中文配音；
- 输出双语 ASS、软字幕 MKV、硬字幕 MP4，并对过宽或闪读字幕先复核再编码；
- 支持批量任务、资源并行、实时日志、终止、重试和安全删除；
- 可选 biliup 登录与投稿，并提供限速、日上限、冷却和重复投稿保护；
- 可配置“无人值守自动化”，自动处理到双语字幕、原音轨双语成片、中文配音成片或完整投稿。

## 使用前必须知道

### 素材权利

只有你明确确认拥有、已获许可或有权使用的素材才能进入下载队列。搜索结果和智能发现
结果只代表候选内容，不代表你获得了下载、翻译、改编或投稿权。前端和后端都会检查权利
确认，项目不会绕过该限制。

### 外部服务与费用

直接下载 URL 不需要 YouTube API Key。关键词搜索和智能发现需要 YouTube Data API Key；
AI 翻译、云端投稿文案和哔哩哔哩投稿则分别需要对应服务。API 价格、免费额度和限流可能
变化，以各供应商账户为准。项目不会在失败时自动切换到另一个可能收费的供应商。

### 本地隐私

控制面板只允许监听 `127.0.0.1` 或 `localhost`。API Key 保存在根目录 `.env`，YouTube
Cookie 默认保存在 `private\cookies.txt`，哔哩哔哩账号文件保存在本机 biliup 目录。
网页只显示“已配置/未配置”，不会回显密钥原文。

## 源码安装

### 1. 环境要求

- 64 位 Windows；
- Python 3.11；
- 足够存放 Whisper large-v3、下载视频和成片的磁盘空间；
- GPU 模式需要 NVIDIA 显卡和兼容驱动，CPU 模式不需要独立显卡；
- 只有运行前端语法检查时才需要 Node.js，日常使用不需要。

克隆项目：

```bat
git clone https://github.com/songsofdawn/youtubeworkflow.git
cd youtubeworkflow
```

### 2. 创建 Python 环境

GPU / 完整锁定环境：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

`requirements.lock.txt` 包含 Windows 源码版所需的 CUDA 12 和 cuDNN 9 Python 运行库，
不要求另外安装 CUDA Toolkit，但不会替代 NVIDIA 显卡驱动。

只使用 CPU 时可安装较小的直接依赖清单：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

CPU 环境还需要把 `config\stage3_config.json` 中的 ASR 设置改为：

```json
"device": "cpu",
"compute_type": "int8"
```

### 3. 准备本地组件

这些大文件和第三方程序不会随 Git 仓库提交：

| 组件 | 默认位置 | 用途 |
|---|---|---|
| yt-dlp | `tools\bin\yt-dlp.exe` | 视频、字幕和元数据下载 |
| FFmpeg | `tools\bin\ffmpeg.exe` | 音频提取和成片 |
| FFprobe | `tools\bin\ffprobe.exe` | 媒体结构与时长检查 |
| Deno | `tools\bin\deno.exe` | yt-dlp 的 JavaScript 运行时 |
| faster-whisper large-v3 | `models\faster-whisper-large-v3` | 本地英文语音识别 |
| VoxCPM2 | `models\VoxCPM2` | 可选的本地中文音色克隆与 TTS |
| biliup / bbup | `biliup\bbup-app` | 可选的哔哩哔哩登录和投稿 |

Whisper 模型目录至少应包含 `config.json`、`model.bin`、`tokenizer.json` 和
`vocabulary.json`。项目只加载本地模型目录；文件缺失时会明确报错，不会按模型名静默
联网下载。

如果你不想手工准备这些组件，请使用 Portable 发行包。

中文配音是独立的可选运行时，不会改变已有 Whisper/翻译环境，也不会在未勾选时运行。
第一次使用需要一次性安装配音环境和 VoxCPM2 模型；只使用原有下载、字幕和成片功能时，
不需要安装以下内容。

#### 中文配音首次安装速查

1. 在项目根目录创建独立 Python 3.11 环境，不要把 VoxCPM2 装进主 `.venv`：

```bat
py -3.11 -m venv .venv_dubbing
.venv_dubbing\Scripts\python.exe -m pip install --upgrade pip
```

电脑已经安装 NVIDIA 驱动、CUDA Toolkit，或另一个 Python 环境已经安装 PyTorch，并不代表
新建的 `.venv_dubbing` 中也有 PyTorch；虚拟环境彼此隔离，仍需执行下一步。只有在你明确
知道另一个 Python 3.10–3.12 环境的完整 `python.exe` 路径，并确认其中已同时具备兼容的
CUDA PyTorch、Demucs、VoxCPM2 和 soundfile 时，才可以把 `runtime_python` 指向该环境并
跳过重复安装。为避免与 Whisper 和主程序依赖冲突，仍推荐使用 `.venv_dubbing`。

2. 安装支持 CUDA 的 PyTorch。下面是 2026-08 在 Windows/NVIDIA 上核对过的 CUDA 12.8
组合，适合本项目当前测试机的 RTX 5060 Ti；以后更换显卡或版本时，先到
[PyTorch 官方安装页](https://pytorch.org/get-started/locally/)重新确认命令：

```bat
.venv_dubbing\Scripts\python.exe -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
```

VoxCPM2 要求 Python 3.10–3.12、PyTorch 2.5 以上和 CUDA 12 以上。不要安装 CPU 版
PyTorch 冒充 GPU 环境；本项目默认也不会在用户不知情时退回极慢的 CPU TTS。

3. 安装配音依赖：

```bat
.venv_dubbing\Scripts\python.exe -m pip install -r requirements_dubbing.txt
```

4. 明确下载一次 VoxCPM2，或者从模型页面手工下载，并放到固定目录。以下命令会下载约
5 GB 大模型，只有确认磁盘和网络条件后才执行：

```bat
.venv_dubbing\Scripts\hf.exe download openbmb/VoxCPM2 --local-dir models\VoxCPM2
```

模型目录最终至少应包含：

```text
models\VoxCPM2\config.json
models\VoxCPM2\audiovae.pth
models\VoxCPM2\model.safetensors
models\VoxCPM2\tokenizer.json
models\VoxCPM2\tokenizer_config.json
```

如果 `hf.exe` 不存在，可从 [OpenBMB/VoxCPM2 模型页](https://huggingface.co/openbmb/VoxCPM2)
手工下载完整仓库。程序只加载这个本地目录，不会用模型名静默联网下载。

5. 验证独立环境确实能看到 CUDA：

```bat
.venv_dubbing\Scripts\python.exe -c "import torch,demucs,voxcpm; print('torch=',torch.__version__); print('cuda=',torch.cuda.is_available()); print('gpu=',torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE')"
```

应看到 `cuda=True` 和实际显卡名称。随后运行 `start_panel.bat`，页面应显示“配音环境已
就绪”。VoxCPM2 官方给出的典型显存需求约为 8 GB，实际占用随文本长度和优化选项变化；
除模型外还应给 Python 环境、Demucs 缓存和任务 WAV 预留充足磁盘空间。

Demucs 的 `htdemucs` 权重由其运行时缓存在
`%USERPROFILE%\.cache\torch\hub\checkpoints`；第一次明确执行配音时若缓存不存在，Demucs
可能联网准备权重，进度会显示在任务日志中。配音路径、设备、时长阈值和背景压低量见
`config\dubbing_config.json`。

> 给后续 Codex：中配入口是 `src\run_dubbing.py`，核心实现位于 `src\dubbing\`，配置是
> `config\dubbing_config.json`，独立依赖是 `requirements_dubbing.txt`。必须继续遵守：只读
> `zh.reviewed.srt` / `zh.clean.srt`、不在配音阶段运行 Whisper、不静默下载 VoxCPM2、每个
> 任务只加载一次模型、复用现有 `gpu_heavy=1` 调度和 Stage 4 renderer。修改前同时阅读根
> 目录 `AGENTS.md`。

### 4. 创建本地配置并启动

```bat
if not exist .env copy .env.example .env
start_panel.bat
```

浏览器会自动打开 <http://127.0.0.1:8765>。如果没有自动打开，可手动访问该地址。

需要使用其他端口时：

```bat
.venv\Scripts\python.exe -m src.run_control_panel --port 8877
```

重复启动时，程序会识别同一项目的面板进程：相同版本只重新打开页面；代码已更新且没有
活动任务时自动替换旧进程；如果旧进程仍有任务，则保留旧进程以保护处理进度。

## 首次配置

第一次打开页面时，顶部会提示“按需要配置服务”。所有服务都是按功能选配：

| 配置 | 什么时候需要 | 不配置时仍可做什么 |
|---|---|---|
| YouTube API Key | 关键词搜索、智能发现 | 仍可直接粘贴 URL / 视频 ID 下载 |
| YouTube Cookie | 登录验证、年龄限制、机器人验证 | 普通公开视频通常仍可下载 |
| AI 翻译 API | AI 中文字幕、可选的云端投稿文案 | 可使用已有的 YouTube 自动中文 |
| Ollama | 本地智能发现、可选的本地投稿文案 | 智能发现可退回规则评分 |
| biliup 账号 | 投稿哔哩哔哩 | 下载、字幕和成片不受影响 |

点击页面右上角“配置服务”可以随时修改。密钥输入框留空会保留已经保存的值。

### YouTube API Key

1. 在 Google Cloud 创建或选择项目；
2. 启用 YouTube Data API v3；
3. 创建 API Key；
4. 在控制面板中保存并重新检测。

官方入口：[YouTube Data API 入门](https://developers.google.com/youtube/v3/getting-started) ·
[Google Cloud 凭据](https://console.cloud.google.com/apis/credentials)

### YouTube Cookie

1. 在浏览器登录下载时要使用的 YouTube 账号；
2. 只导出 `youtube.com` Cookie，格式选择 Mozilla/Netscape `cookies.txt`；
3. 在“配置服务”中选择 TXT 文件并保存；
4. 状态显示“已导入”后重试下载。

参考：[yt-dlp Cookie 说明](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) ·
[YouTube Cookie 导出建议](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)

Cookie 等同登录凭据。不要上传到 Issue、聊天或网盘，也不要把已经使用过的程序目录重新
打包给别人。

### AI 翻译供应商

供应商、密钥变量和可选模型只在 `src\stage3\llm_providers.py` 中统一声明，控制面板会
显示当前可用目录：

| 供应商 | 密钥变量 | 内置模型示例 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | DeepSeek V4 Flash / Pro |
| 智谱 GLM | `ZHIPU_API_KEY` | GLM-4.7-Flash / FlashX / GLM-5.2 |
| 阿里云百炼 / 通义千问 | `DASHSCOPE_API_KEY` | Qwen3.7 Flash / Plus / Qwen-MT-Plus |
| Moonshot / Kimi | `MOONSHOT_API_KEY` | Kimi K2.6 / K2.5 |
| MiniMax | `MINIMAX_API_KEY` | MiniMax M2.7 / Highspeed |
| 火山方舟 / 豆包 | `ARK_API_KEY` | Doubao Seed 2.0 Lite |
| OpenAI | `OPENAI_API_KEY` | GPT-5.6 Luna / Terra / Sol / GPT-4.1 mini |
| Anthropic / Claude | `ANTHROPIC_API_KEY` | Claude Haiku 4.5 / Sonnet 5 / Opus 5 |
| 自定义 OpenAI 兼容接口 | `CUSTOM_LLM_API_KEY` | 自定义模型与 Base URL |

模型可用性、价格和免费政策由供应商决定；项目中的标签不构成长期价格承诺。最稳妥的做法
是在控制面板保存后先检查账户额度，再处理较长视频。

也可以手工编辑 `.env`：

```dotenv
YOUTUBE_API_KEY=

TRANSLATION_PROVIDER=deepseek
TRANSLATION_MODEL=deepseek-v4-flash
TRANSLATION_BASE_URL=https://api.deepseek.com
TRANSLATION_THINKING=disabled
TRANSLATION_BATCH_SIZE=32
TRANSLATION_CONTEXT_BEFORE=2
TRANSLATION_CONTEXT_AFTER=2
TRANSLATION_MAX_OUTPUT_TOKENS=4096

DEEPSEEK_API_KEY=
ZHIPU_API_KEY=
DASHSCOPE_API_KEY=
MOONSHOT_API_KEY=
MINIMAX_API_KEY=
ARK_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
CUSTOM_LLM_API_KEY=
```

不要提交填写过的 `.env`，也不要使用 `git add -f` 绕过忽略规则。

## 从下载到成片

### 1. 添加视频

控制面板提供三种入口：

- **视频 ID / URL**：每行一个地址或 ID，不需要 YouTube API Key；
- **关键词搜索**：设置关键词、数量和排序，再从结果中选择；
- **智能发现**：按领域和时间窗口深度召回，再做本地评分、去重和反馈学习。

无论使用哪一种方式，下载前都必须勾选“确认拥有下载和使用权”。默认下载最高 1080p
的源视频、源音频、可用中英文字幕、封面、简介和元数据；翻译与成片不会覆盖这些文件。

智能发现完整功能默认使用 Ollama 的 `qwen3.5:9b` 和
`qwen3-embedding:0.6b`：

```powershell
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b
```

模型可在“配置服务 → Ollama 本地智能发现”中更换或停用。Ollama 不可用时，任务会给出
警告并退回规则评分。模型只接收 YouTube 公开元数据和缩略图，不会收到 API Key、Cookie、
本地视频或字幕。智能发现会消耗 YouTube Data API 配额，实际限制以 Google Cloud 为准。

### 2. 生成英文字幕

项目会根据任务设置使用以下来源：

1. YouTube 手工英文字幕；
2. YouTube 自动英文字幕；
3. 本地 faster-whisper large-v3 识别。

默认开启“自动英文字幕仍启用 Whisper 对比”，程序会比较覆盖率、时间轴、稳定性、可读性
和来源可信度，再生成统一的 `subtitles\en.selected.srt`。关闭该选项后，已有 YouTube
英文字幕时不会额外运行 Whisper；没有英文字幕时仍会用 Whisper 兜底。

CPU 可以完成识别，但长视频可能很慢。GPU 版使用 `cuda + float16`。成片会自动检测
NVENC；不可用时回退到 CPU x264。

### 3. 生成中文字幕

选中一个或多个任务，然后选择：

- **AI API 翻译**：使用当前供应商和模型，加入队列前必须勾选“允许调用所选 AI API”；
- **YouTube 自动中文**：不调用翻译 API，但视频必须存在可用的自动中文字幕。

默认翻译策略是 Thinking 关闭、每批 32 条、前后各 2 条只读上下文、最大输出 4096
Token。程序按批次保存结果，响应缺少 ID 时只重试待完成字幕，不重新发送已经成功的行。
只有显式使用“整批润色”才会进行正常的第二次翻译。

本地质检只验证 JSON、字幕 ID、非空译文、时间轴、非法控制字符和明显的单 ID 内容污染；
不会用“翻译腔”“字符速度”或英文残留等主观启发式规则自动购买第二遍翻译。

遇到 `429` 或智谱 `1305` 时，程序会先写入当前检查点，再按 5、15、30、60、120 秒明确
等待。Key、余额或权限错误不会盲目重试，也不会自动切换供应商。

### 4. 生成双语字幕或成片

| 输出模式 | 适合场景 |
|---|---|
| 仅双语 ASS | 预览、继续编辑，不编码视频 |
| 软字幕 MKV | 字幕轨可开关，适合归档和后期 |
| 硬字幕 MP4 | 字幕压入画面，适合直接投稿 |
| 两种都生成 | 同时生成 MKV 与 MP4 |

成片保留原始配音。默认英文在上、中文在下，每种语言在任意时刻严格保持一行。1080p
目标字号为中文 60px / 英文 44px；长句先缩小到可发布下限 54px / 40px，再进行轻微
横向压缩或自然分页。

如果字幕在安全字号下仍可能裁切，或分页会让画面闪读，任务会显示“需要复核”，只写入
预览与质检报告，不启动 FFmpeg。点击任务卡右侧黄色“审”按钮，可以缩短成片专用文字，
或明确选择只在成片中隐藏该条。保存后程序先重新预检，全部通过才继续编码。原英文、
原中文、字幕 ID 和时间轴始终不被覆盖。

### 5. 生成中文 AI 配音（可选）

先完成 AI 中文字幕，使任务中存在 `subtitles\zh.reviewed.srt`（优先）或
`subtitles\zh.clean.srt`。配音阶段不会运行 Whisper，也不会使用 YouTube 自动中文字幕
代替这两个输入。

选中任务后勾选“启用中文 AI 配音（VoxCPM2）”，可自动选取 5–10 秒参考人声，或手工填写
原视频开始/结束秒数；成片字幕可选“仅中文”或“中英双语”。流程固定为：

```text
原视频音轨 → Demucs vocals / background → reference.wav
中文字幕逐句 → VoxCPM2（每个任务只加载一次模型）→ 单句 WAV
单句时长适配 → 按字幕绝对时间轴拼接 → 背景音 duck/mix → Stage 4 成片
```

程序优先保留自然语速：生成音频不超过可用时槽约 110% 时直接使用；轻微超长时最多加速到
1.3 倍；仍超长则保留结果并在任务卡标为“配音需要复核”，不会静默截断句子。每句完成后
立即写入 `dubbing\manifest.json`；中途失败、进程终止或显存不足后重试，只重做缺失或输入
哈希已变化的句子。修改某句中文字幕只会使对应句的配音缓存失效。“重新生成中配”会显式
强制重做全部 TTS 片段。每个新片段还会保存独立 metadata；若 `manifest.json` 因进程终止
比磁盘 WAV 落后一小段，续跑会在确认字幕、参考声音、模型设置和 WAV 均有效后恢复这些片段。

项目会把当前目录的 `tools\bin` 自动放到中配进程 PATH 首位，不需要手工执行 `set PATH`，
也不会修改 Windows 用户或系统 PATH。任务启动前会检查 FFmpeg Shared DLL，并用当前
`.venv_dubbing` 的 Torchaudio/TorchCodec 实际保存一个极短临时 WAV；失败时控制面板显示
简洁的缺少 FFmpeg、非 Shared Build、DLL 加载失败或版本/依赖不兼容提示。

V1 仅支持一个主播音色和一个参考片段，不做说话人分离、多人声角色映射、lip sync、
自动情绪分析，也不会根据语义或主观听感自动判断配音质量。音色克隆与发布前请确认已取得
声音与素材使用授权。

实际操作顺序：

1. 正常下载视频并处理到中文字幕，确认任务目录已有 `subtitles\zh.reviewed.srt` 或
   `subtitles\zh.clean.srt`。
2. 在任务列表勾选要处理的视频。
3. 勾选“启用中文 AI 配音（VoxCPM2）”。环境提示必须显示已就绪。
4. 第一次建议使用“自动选取 5–10 秒”；若音色不理想，再改为手动时间段，选择只有主播
   清晰说话、音乐和杂音较少的连续片段。
5. 选择配音成片显示“仅中文”或“中英双语”，点击“一键生成成片”。
6. 任务卡会依次显示分离人声、参考声音、逐句进度、时长处理、混音和成片。完成后点击
   “音”打开中配目录，或打开任务目录查看 `stage4\video\final_chinese_dubbed_*.mp4`。

普通重试会校验缓存，只继续失败或变化的句子；“重新生成中配”会强制重做所有 TTS 句子，
但仍可复用有效的 Demucs 分离结果。首次运行通常最慢，因为需要生成 Demucs 缓存、分离
整段音频并首次加载 VoxCPM2；之后同一任务的重试会明显更快。

### 6. 查看状态、停止或续跑

任务卡按“下载 → 英文 → AI 翻译 → 配音 → 成片 → 投稿”显示状态。未启用配音的旧任务会
把该阶段显示为跳过，并保持原来的进度计算。并行调度 v0.5 默认提供：

- 2 个下载槽；
- 2 个 AI API 槽；
- 1 个本地识别 / 成片重任务槽；
- 1 个上传槽；
- 全局上限：源码/GPU 版 4 个进程，Portable CPU 版 3 个进程。

单个视频仍遵守内容依赖顺序，不同视频则可以交错推进。点击“终止”只停止当前子进程，
不会删除已完成产物；修复问题后点击“重试”或重新执行相同操作，程序会校验检查点并继续。

“清空历史记录”只删除已结束的作业记录和日志。“删除任务”或“批量删除”会永久删除目标
视频的本地文件；运行中和排队中的任务必须先终止。

## 无人值守自动化

“无人值守自动化”可以把新下载任务自动处理到以下任一终点：

- 双语字幕；
- 双语成片或中文配音成片；
- 完整投稿。

英文策略、中文策略、可选中文配音、成片格式、无配音视频处理、异常处理和投稿资料模型都可
独立选择。开启中文配音后还可选择自动/手动参考声音、仅中文/中英双语字幕，以及中配需要
复核时“阻止后续”或“仍继续”的策略。
“始终 API 翻译”会忽略已有 YouTube 中文并重新翻译；“只用 YouTube 中文”则永远不会
调用翻译 API。完整投稿要求生成硬字幕 MP4。

无人值守中文配音只允许用于“生成成片”或“完整投稿”，并会锁定为“始终使用 API 翻译”；
不能与“只生成字幕”、YouTube 自动中文字幕或“仅 ASS”组合。原因是中配阶段只接受
`zh.reviewed.srt` / API 生成的 `zh.clean.srt`，不能把 YouTube 自动中文直接当作配音脚本。

### 面板配置与组合规则

在面板顶部打开“无人值守自动化”，按以下顺序配置：

1. 将“自动化执行到”设为“生成成片后停止”或“完整投稿”；
2. 打开“中文 AI 配音”，面板会自动把“中文字幕来源”切换并锁定为“始终使用 API 翻译”；
3. 选择自动参考声音，或填写手动参考片段的开始秒和结束秒；
4. 选择成片显示“仅中文”或“中英双语”字幕；
5. 保持默认的“阻止成片/投稿并按异常策略处理”，或明确选择带风险的继续策略；
6. 若终点是完整投稿，选择“硬字幕 MP4”或“两种都生成”，并核对投稿账号与可见性。

| 选项 | 开启中配后允许的值 | 说明 |
|---|---|---|
| 自动化终点 | 生成成片、完整投稿 | 只生成字幕时不会运行配音阶段 |
| 中文字幕来源 | 始终使用 API 翻译 | 防止把 YouTube 自动中文直接用于 TTS |
| 中配参考声音 | 自动 5–10 秒、手动时间段 | 手动模式要求结束秒大于开始秒 |
| 中配成片字幕 | 仅中文、中英双语 | 只改变成片字幕显示，不改变配音脚本来源 |
| 自动成片格式 | 软字幕 MKV、硬字幕 MP4、两种都生成 | 仅 ASS 没有可替换的音轨，不能用于中配 |
| 完整投稿格式 | 硬字幕 MP4、两种都生成 | 投稿必须存在硬字幕 MP4 |
| 中配需要复核时 | 阻止后续、仍继续 | 默认阻止；继续模式需要主动选择 |

面板会保存这些选项，并在流程卡中展示本次实际执行链路。加入队列前，后端还会再次检查
VoxCPM2 模型、独立 Python 运行时、PyTorch/CUDA、翻译 API 和投稿环境，不能仅靠修改页面
参数绕过组合限制。

普通的“AI 字幕尚未人工浏览”提示不会阻塞无人值守任务，但结构错误、时间轴错误、字幕
裁切或闪读仍会阻止上传。默认异常策略会记录原因、跳过该视频并继续队列。自动化投稿
默认公开；需要先检查稿件时，请主动开启“仅自己可见”。

中配的默认复核策略同样是安全阻止：只要 `dubbing\manifest.json` 标记时槽超限，就会在
Stage 4 之前停止，并按总异常策略跳过或保留失败；只有明确选择“仍继续成片与投稿”才会
越过该警告。投稿始终读取本次 Stage 4 manifest 记录的中配成片路径，不会找不到中配文件时
静默改投保留原音轨的旧双语成片。

仅中文字幕会生成 `final_chinese_dubbed_hardsub.mp4`，中英双语字幕会生成
`final_chinese_dubbed_bilingual_hardsub.mp4`。完整投稿不按文件名猜测，而是读取
`stage4\stage4_manifest.json` 中的 `hardsub_output_path`；记录的中配文件缺失或为空时，
任务会明确失败并要求重新成片。

确认无配音且没有英文字幕的视频，可按设置保留原视频并生成“【无配音】”投稿信息，或
直接跳过。该模式不会把无配音素材伪装成双语成片。

## 投稿哔哩哔哩

### 登录

在“配置服务”中点击“打开登录工具”，使用 bbup/biliup 窗口登录自己的账号，再回到页面
点击“登录后重新检测”。也可以双击：

```text
login_bilibili.bat
```

账号文件默认保存在 `biliup\bbup-app\data`。已有 biliup 账号 JSON 也可放在
`private\biliup_accounts`。不要提交或分发这些文件。

### 投稿前检查

手动投稿前需要核对账号、标题、标签、简介、分区、自制/转载类型、转载来源、可见性、
禁止转载选项和封面。首次测试建议勾选“仅自己可见”。如果还没有硬字幕 MP4，投稿队列
会先完成硬字幕成片。

成功后任务会记录 BV 号和链接，防止重复投稿。默认最短投稿间隔为 10 分钟，每个本机
自然日最多成功投稿 20 条；都可在“配置服务 → 哔哩哔哩账号”调整。平台返回
`137022`“投稿过于频繁”时，当前任务会回到队列，全部上传暂停 6 小时；重启程序不会
绕过冷却，页面会显示预计恢复时间。

## 输出文件

每个视频位于 `downloads\日期\视频ID_标题\`。最方便的方式是点击任务卡右侧“打开任务
目录”。以下路径均相对于单个视频任务目录：

| 相对路径 | 内容 |
|---|---|
| `video\source.mp4` | 下载的原视频 |
| `audio\source_audio.wav` | 为 Whisper 准备的源音频 |
| `metadata\info.json` | 原视频元数据 |
| `metadata\description.txt` | 原视频简介 |
| `metadata\thumbnail.jpg` | 下载封面 |
| `subtitles\en.selected.srt` | 最终选定的英文字幕 |
| `subtitles\zh.raw.srt` | AI 返回的原始中文结果 |
| `subtitles\zh.clean.srt` | 结构检查通过的中文字幕 |
| `subtitles\zh.reviewed.srt` | 可选的人工审核版中文字幕 |
| `dubbing\source.wav` | 从原视频提取的源音频 |
| `dubbing\vocals.wav` / `dubbing\background.wav` | Demucs 人声与背景声 |
| `dubbing\reference.wav` | 自动或手工选择的音色参考片段 |
| `dubbing\segments\*.wav` | 按字幕编号保存的原始单句 TTS |
| `dubbing\chinese_voice.wav` | 按原字幕绝对时间轴拼接的中文人声 |
| `dubbing\dubbed_audio.wav` | 中文人声与原背景混合后的最终音轨 |
| `dubbing\manifest.json` | 配音设置、逐句输入哈希、时长与续跑状态 |
| `stage4\subtitles\bilingual.ass` | 正式双语 ASS |
| `stage4\subtitles\chinese_dubbed.ass` | 中文配音成片的仅中文 ASS |
| `stage4\subtitles\bilingual_preview.ass` | 排版需要复核时的预览 |
| `stage4\subtitles\en.layout_reviewed.srt` | 成片专用英文排版副本 |
| `stage4\subtitles\zh.layout_reviewed.srt` | 成片专用中文排版副本 |
| `stage4\video\final_bilingual_softsub.mkv` | 软字幕成片 |
| `stage4\video\final_bilingual_hardsub.mp4` | 硬字幕成片与默认投稿文件 |
| `stage4\video\final_chinese_dubbed_softsub.mkv` | 仅中文字幕的中文配音软字幕成片 |
| `stage4\video\final_chinese_dubbed_bilingual_softsub.mkv` | 中英双语字幕的中文配音软字幕成片 |
| `stage4\video\final_chinese_dubbed_hardsub.mp4` | 仅中文字幕的中文配音硬字幕成片 |
| `stage4\video\final_chinese_dubbed_bilingual_hardsub.mp4` | 中英双语字幕的中文配音硬字幕成片 |
| `download_manifest.json` | 下载状态与源文件记录 |
| `stage3_manifest.json` | 字幕选择和翻译状态 |
| `stage4\stage4_manifest.json` | 成片、质检与续跑状态 |

任务目录中的 JSON、检查点、QC 报告和日志用于断点续跑与旧任务兼容。不要手工删除或改名；
需要释放空间时，使用面板的删除功能并核对视频 ID。

## 目录结构

```text
youtubeworkflow\
├─ START_HERE.bat             Portable 推荐入口，也可转到源码启动脚本
├─ start_panel.bat            本地控制面板入口
├─ login_bilibili.bat         哔哩哔哩登录工具
├─ subtitle_tools.bat         高级字幕审核、润色与重译工具
├─ build_portable.bat         构建 CPU/GPU Portable 包
├─ verify_project.bat         完整离线验证
├─ .env.example               本地密钥配置模板
├─ config\                    下载、字幕、成片、发现与投稿配置
├─ src\                       应用源码
├─ tests\                     离线自动化测试
├─ tools\bin\                FFmpeg、yt-dlp、Deno 等本地程序
├─ models\                   本地 Whisper 与可选 VoxCPM2 模型
├─ biliup\                   可选的哔哩哔哩工具
├─ downloads\                视频任务与输出
├─ private\                  Cookie 和可选账号文件
├─ logs\                     任务日志
└─ work\                     队列、发现数据库与临时状态
```

`downloads`、`private`、`logs`、`work`、`models`、`tools`、`biliup` 和虚拟环境均不应
提交到 Git。

## 常用配置文件

| 文件 | 负责内容 |
|---|---|
| `.env` | YouTube Key、AI 供应商、模型、Base URL 与密钥 |
| `config\download_config.json` | 下载清晰度、字幕语言、Cookie 与重试 |
| `config\stage3_config.json` | Whisper、英文选择、翻译批次与结构 QC |
| `config\stage4_config.json` | 双语样式、排版安全线、编码器与音频策略 |
| `config\dubbing_config.json` | VoxCPM2 路径、Demucs、参考音频、时长和混音策略 |
| `config\trending_config.json` | 搜索、智能发现和 Ollama 参数 |
| `config\discovery_keywords.json` | 智能发现领域、查询词和归类关键词 |
| `config\publish_config.json` | biliup、投稿间隔、日上限和冷却策略 |

优先通过控制面板修改用户级设置。直接编辑 JSON 前先关闭面板并保留备份；错误类型、越界
数值或错误路径会导致对应步骤拒绝运行。

## 高级字幕工具

普通使用不需要离开控制面板。需要导出/导入人工审核表、整批润色、强制从头重译，或只
测试前 30 秒 Whisper 时，可以运行：

```bat
subtitle_tools.bat "完整的视频任务目录"
```

脚本会标明哪些操作会调用 AI API，并再次要求输入 `YES`。强制重译会忽略原翻译检查点，
只应在明确需要替换整份译文时使用。

命令行入口可先查看帮助：

```bat
.venv\Scripts\python.exe -m src.download_video --help
.venv\Scripts\python.exe -m src.run_stage3 --help
.venv\Scripts\python.exe -m src.run_stage4 --help
.venv\Scripts\python.exe -m src.run_control_panel --help
```

## 构建 Portable 发行包

构建机需要已经准备好源码环境、本地工具、Whisper 模型和 biliup。首次构建还需要联网
下载 Python 3.11 Embedded 运行时及对应 wheel。

```bat
build_portable.bat
```

默认同时构建 CPU 和 GPU。也可以指定版本和发行类型：

```bat
build_portable.bat all 0.4.0
build_portable.bat cpu 0.4.0
build_portable.bat gpu 0.4.0
build_portable.bat all 0.4.0 --skip-archive
```

输出位于 `dist`。每个包都会：

- 包含独立 Python、FFmpeg、FFprobe、yt-dlp、Deno、Whisper 模型和 biliup；中文配音的
  VoxCPM2 权重与独立运行时不打入默认包，需要用户按上述可选步骤准备；
- 把 `PORTABLE_README.md` 复制为用户看到的根目录 `README.md`；
- 使用干净的 `.env`，不包含 Cookie 或制作者账号；
- 生成 `portable_manifest.json`、`PACKAGE_FILES.txt` 和 ZIP SHA256；
- 检查 Python 导入、命令入口、本地程序、模型完整性和账号清洁度。

更底层的 PowerShell 入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_portable.ps1 -Edition all -Version 0.4.0
```

## 验证

完整离线验证：

```bat
verify_project.bat
```

主要检查命令：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m compileall -q src tests
node --check src\control_panel\static\app.js
git diff --check
```

测试会模拟 AI、YouTube、FFmpeg 和投稿调用，不访问真实供应商，也不会读取或打印 API
Key。`verify_project.bat` 还会检查本地工具、Whisper 模型、依赖一致性，以及私密/生成目录
是否误入版本控制。

## 常见问题

| 现象 | 处理方法 |
|---|---|
| 双击后窗口立即关闭 | 在命令提示符运行 `start_panel.bat` 查看错误；通常是 `.venv`、本地工具或完整解压问题 |
| 浏览器没有自动打开 | 手动访问 <http://127.0.0.1:8765>；端口占用时换用 `--port 8877` |
| 路径过长或 FFmpeg 找不到文件 | 把项目移到较短的本地路径，例如 `D:\YouTubeWorkflow`；不要在 ZIP、网络盘或 OneDrive 占位目录运行 |
| YouTube 要求登录、验证年龄或检查机器人 | 重新导出并导入有效的 YouTube Cookie |
| 下载中途出现 `HTTP 403` | 程序会保留分片并尝试 `web_embedded` 续传；仍失败时更新 yt-dlp、切换网络，必要时参考 [PO Token 指南](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) |
| 没有可用英文字幕 | 检查 Whisper 模型；GPU 版检查驱动，也可用 CPU 模式验证 |
| AI 翻译报 `401/403` | 检查 Key、Base URL、模型和账户权限 |
| AI 翻译报 `402` | 检查账户余额或计费状态 |
| AI 翻译报 `429/1305` | 这是限流或拥堵；程序会保存检查点后等待，不要同时强制重开多个任务 |
| 成片显示“需要复核” | 点击黄色“审”，缩短受影响字幕或明确隐藏该条；预检通过后自动继续 |
| 中文配音显示环境未就绪 | 检查 `.venv_dubbing`、Demucs/VoxCPM2 包、PyTorch CUDA、NVIDIA 驱动和 `models\VoxCPM2` |
| 某句配音失败或显存不足 | 修复运行环境后点重试；已完成单句不会重做。需要全部重做时点“重新生成中配” |
| 没有投稿按钮 | 确认硬字幕成片已完成、没有排版复核，并已登录 biliup |
| 投稿显示 `137022` | 不要重复新建投稿；等待页面显示的冷却结束，或调大投稿间隔 |
| 重新运行是否重复付费 | 配置和提示版本不变时复用检查点，只请求缺失项；更换供应商、模型、Thinking 或强制重译会使相关检查点失效 |

## 安全、版权与数据边界

- 只下载、翻译、改编和投稿你拥有、已获许可或明确有权使用的内容；
- `.env`、Cookie、账号文件和下载内容都可能包含敏感信息，不要提交或转发；
- AI 翻译会把字幕文本和必要上下文发送给你选择的供应商；
- Ollama 智能发现只使用 YouTube 公开元数据和缩略图；
- 删除任务是不可恢复操作，确认视频 ID 和目录后再执行；
- 已经泄露到聊天、Issue、日志或公开仓库的 Key 应立即在供应商后台吊销并重建；
- 给别人分发软件时使用全新的构建产物，不要发送已经运行和配置过的目录。

项目提供的是本地处理工具，不授予任何视频、音乐、字幕或平台内容的版权与使用权。
Portable 包包含多个独立授权的第三方组件；公开或商业分发前请阅读
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，并自行核对所使用二进制文件和模型的
上游许可义务。
