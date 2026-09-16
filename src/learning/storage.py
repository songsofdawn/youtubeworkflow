"""One transaction lock for event/analysis/profile commits across processes."""
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class LearningStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "data/learning/events.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '')")
            db.execute("CREATE TABLE IF NOT EXISTS query_runs (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS query_hits (run_id TEXT NOT NULL, video_id TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,video_id))")
            db.execute("CREATE TABLE IF NOT EXISTS attributions (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS video_metric_snapshots (video_id TEXT NOT NULL, observed_at TEXT NOT NULL, view_count INTEGER NOT NULL DEFAULT 0, like_count INTEGER NOT NULL DEFAULT 0, comment_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(video_id, observed_at))")
            db.execute("CREATE TABLE IF NOT EXISTS schema_meta (name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
            db.execute("INSERT INTO schema_meta(name,version) VALUES ('learning',2) ON CONFLICT(name) DO UPDATE SET version=max(version,excluded.version)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
