# YouTube Workflow

本项目包含两个彼此隔离的阶段：阶段一发现候选视频并生成人工审核清单；阶段二只下载已通过版权审核的候选，或下载用户明确确认拥有使用权的单个 URL。阶段二不会修改阶段一生成的 JSON 或 CSV。

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

## 测试

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试全部使用 mock，不会访问 YouTube 或消耗 API 配额。
