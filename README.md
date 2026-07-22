# YouTube Workflow

本项目覆盖候选视频发现、版权审核、视频下载、英文滚动字幕清洗和 DeepSeek 中文翻译。下载流程不会修改候选发现流程生成的 JSON 或 CSV；字幕本地化流程也不会覆盖 YouTube 原始字幕。

## 阶段一：候选发现与审核

1. 运行 `setup_stage1_fixed.bat`，并在 `.env` 设置 `YOUTUBE_API_KEY`。
2. 运行 `run_fetch_candidates_fixed.bat`。
3. 打开最新的 `candidates\YYYY-MM-DD_US_localization_top50.csv` 或 JSON，人工审核每条记录。

允许阶段二下载的记录必须同时满足：

- `selected` 为 `1`、`true` 或字符串 `"1"`；
- `rights_status` 为 `APPROVED`、`OWNED`、`LICENSED` 或 `PERMISSION_GRANTED`。

例如：

```json
{
  "video_id": "VIDEO_ID",
  "selected": 1,
  "rights_status": "PERMISSION_GRANTED"
}
```

`PENDING`、`REJECTED` 和 `selected=0` 始终跳过。`--video-ids` 只缩小候选范围，不能绕过上述检查。

## 阶段二：视频、字幕、元数据与音频

先运行 `setup_stage2.bat` 检查 `.venv` 及项目本地的 `yt-dlp.exe`、`ffmpeg.exe`、`ffprobe.exe`。阶段二不依赖系统 PATH 或 Conda。可选 Cookies 文件为 `private\cookies.txt`，必须是非空的 Netscape 格式；程序不会从浏览器自动读取 Cookies，也不会输出其内容。

批量下载最新候选榜：

```bat
run_stage2_download_selected.bat
```

也可以把某个候选 JSON 拖到该 BAT 上，或直接执行：

```bat
.venv\Scripts\python.exe src\download_selected_candidates.py
.venv\Scripts\python.exe src\download_selected_candidates.py --input candidates\2026-07-21_US_localization_top50.json
.venv\Scripts\python.exe src\download_selected_candidates.py --video-ids ID1 ID2
```

候选输出位于 `downloads\candidates\YYYY-MM-DD\排名_VIDEOID_安全标题\`。每个任务包含 MP4、48 kHz 双声道 PCM WAV、完整元数据、缩略图和 `download_manifest.json`。字幕会分别请求英文与中文，并对每种语言采用“人工字幕优先、自动字幕回退”：英文保存为 `en.manual.vtt/.srt` 或 `en.auto.vtt/.srt`，中文保存为 `zh.manual.vtt/.srt` 或 `zh.auto.vtt/.srt`。某种语言不存在不会使视频下载失败。

下载完成后会自动清洗英文滚动字幕。`en.auto.vtt` 和 `en.auto.srt` 保持为原始备份，同时生成 `en.auto.raw.srt` 与去重后的 `en.clean.srt`。`zh.auto.vtt/.srt` 也只作为参考备份；`zh.clean.srt` 使用 `en.clean.srt` 的清洗后时间轴生成。也可单独执行：

```bat
.venv\Scripts\python.exe src\clean_subtitles.py --input "字幕目录\en.auto.vtt"
```

自由 URL 下载可双击 `run_download_url.bat`，阅读版权提示、输入 URL，并输入 `Y` 确认。命令行调用也必须传 `--confirm-rights`：

```bat
.venv\Scripts\python.exe src\download_video.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --output downloads\manual --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --metadata-only --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --subtitles-only --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --no-audio-extract --confirm-rights
.venv\Scripts\python.exe src\download_video.py --url "URL" --force --confirm-rights
```

自由下载输出位于 `downloads\manual\YYYY-MM-DD\VIDEOID_安全标题\`。所有下载都强制 `--no-playlist`。成功任务重复运行会直接跳过；部分任务只补缺失文件。候选批量模式使用 `downloads\download_archive.txt`，若归档已有 ID 但本地视频被删除，本次修复会自动绕过归档。

如果批量统计中出现 `failed`，可双击 `run_repair_failed_downloads.bat`。它只扫描 `downloads\candidates` 中未成功或关键文件缺失的任务，从 manifest 指向的候选 JSON 恢复记录，重新验证 `selected` 和 `rights_status`，并最多重试 3 次。它不会重新下载已经完整的步骤。先查看待修复列表或只修复指定视频也可以使用：

```bat
.venv\Scripts\python.exe src\repair_failed_downloads.py --dry-run
.venv\Scripts\python.exe src\repair_failed_downloads.py
.venv\Scripts\python.exe src\repair_failed_downloads.py --video-ids ID1 ID2
.venv\Scripts\python.exe src\repair_failed_downloads.py --attempts 5 --retry-delay 5
```

下载设置见 `config\download_config.json`，默认最高 1080p、重试 10 次、保留 `.part` 以支持断点续传。

## 英文字幕清洗与 DeepSeek 中文翻译

安装依赖：

```bat
.venv\Scripts\python.exe -m pip install -r requirements_stage3.txt
```

在本地 `.env` 中配置以下变量。`.env` 已被 Git 忽略，不要把真实密钥写入源码或提交到仓库：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

可把单个视频任务目录或包含多个视频的日期目录拖到 `run_stage3.bat`，也可双击 BAT 后输入目录。菜单提供以下操作：

1. 清洗并重建英文字幕；
2. 检查翻译配置和预计批次数，不调用 API；
3. 翻译已经清洗的英文字幕，支持断点续跑；
4. 先清洗英文字幕，再翻译成中文；
5. 翻译并润色全部中文字幕；
6. 忽略 checkpoint，从头重新翻译。

命令行也可以直接指定操作：

```bat
run_stage3.bat "downloads\candidates\2026-07-21" clean
run_stage3.bat "downloads\candidates\2026-07-21" check
run_stage3.bat "downloads\candidates\2026-07-21" translate
run_stage3.bat "downloads\candidates\2026-07-21" full
run_stage3.bat "downloads\candidates\2026-07-21" polish
run_stage3.bat "downloads\candidates\2026-07-21" retranslate
```

所有付费操作都要求再次输入 `YES`。日期目录会自动发现其下的全部视频任务；单个视频失败不会中断其他视频，没有英文字幕的任务会被跳过。最终推荐使用 `subtitles\en.clean.srt` 和 `subtitles\zh.clean.srt`。翻译用量、检查结果和断点文件分别保存在 `translation\api_usage.json`、`translation\subtitle_qc.json` 和 `translation\checkpoints\`。

## 测试

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试全部使用 mock，不会访问 YouTube 或消耗 API 配额。
