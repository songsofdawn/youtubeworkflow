from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "replace_data" / "data" / "learning"

def ensure_tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '')")
    db.execute("CREATE TABLE IF NOT EXISTS query_runs (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS query_hits (run_id TEXT NOT NULL, video_id TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,video_id))")
    db.execute("CREATE TABLE IF NOT EXISTS attributions (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL)")

def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    dest = root / "data" / "learning"
    dest.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = root / "data" / f"learning_backup_{stamp}"
    if any(dest.iterdir()):
        shutil.copytree(dest, backup)
        print(f"[备份] {backup}")

    # videos
    src_videos = SEED / "videos"
    dst_videos = dest / "videos"
    dst_videos.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in src_videos.glob("*.json"):
        dst = dst_videos / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            copied += 1
    print(f"[视频分析] 新增 {copied} 个 seed JSON")

    # merge events, preserve query history
    dst_db = dest / "events.sqlite3"
    src_db = SEED / "events.sqlite3"
    with sqlite3.connect(dst_db) as out, sqlite3.connect(src_db) as inp:
        ensure_tables(out)
        rows = inp.execute("SELECT event_id,video_id,kind,created_at,metadata,status,error FROM events").fetchall()
        before = out.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        out.executemany(
            "INSERT OR IGNORE INTO events(event_id,video_id,kind,created_at,metadata,status,error) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        out.commit()
        after = out.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"[事件] 新增 {after-before} 条 seed events")

    # rebuild with project's real code
    sys.path.insert(0, str(root))
    try:
        from src.learning.learning_service import LearningService
        profile = LearningService(root).rebuild()
        print(f"[完成] LearningService.rebuild() 成功，sample_size={profile.get('sample_size')}")
    except Exception as exc:
        # fallback files ensure immediate baseline still exists
        shutil.copy2(SEED / "user_content_profile.json", dest / "user_content_profile.json")
        shutil.copy2(SEED / "taxonomy_suggestions.json", dest / "taxonomy_suggestions.json")
        print(f"[警告] 自动 rebuild 失败：{type(exc).__name__}: {exc}")
        print("[回退] 已复制 seed profile；项目下一次正常 rebuild 会从已合并 events/videos 重新聚合。")

if __name__ == "__main__":
    main()
