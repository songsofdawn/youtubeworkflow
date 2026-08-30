from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ALLOWED_FEEDBACK = {
    "interested",
    "boring",
    "irrelevant",
    "duplicate",
    "wrong_language",
    "unsafe",
    "selected",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DiscoveryStore:
    """Small local cache and explicit user preference store.

    The database contains public video metadata and model outputs only. It never
    stores API keys, cookies, or downloaded media.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_cache (
                    cache_key TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    video_id TEXT PRIMARY KEY,
                    feedback TEXT NOT NULL,
                    title TEXT NOT NULL,
                    channel_title TEXT NOT NULL,
                    pack_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_kind ON feedback(feedback, updated_at)"
            )

    def get_evaluation(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM evaluation_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def put_evaluation(
        self,
        cache_key: str,
        video_id: str,
        stage: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_cache(cache_key, video_id, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cache_key,
                    video_id,
                    stage,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    _utc_now(),
                ),
            )

    def get_embedding(self, cache_key: str) -> list[float] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT vector_json FROM embedding_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            values = json.loads(str(row["vector_json"]))
            return [float(value) for value in values] if isinstance(values, list) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def put_embedding(self, cache_key: str, model: str, vector: list[float]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO embedding_cache(cache_key, model, vector_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    vector_json = excluded.vector_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cache_key,
                    model,
                    json.dumps(vector, separators=(",", ":")),
                    _utc_now(),
                ),
            )

    def record_feedback(self, item: dict[str, Any], feedback: str) -> dict[str, Any]:
        kind = str(feedback or "").strip().casefold()
        if kind not in ALLOWED_FEEDBACK:
            raise ValueError("不支持的智能发现反馈类型")
        video_id = str(item.get("video_id") or "").strip()
        if not video_id or len(video_id) > 32:
            raise ValueError("视频 ID 无效")
        record = {
            "video_id": video_id,
            "feedback": kind,
            "title": str(item.get("title") or video_id).strip()[:500],
            "channel_title": str(item.get("channel_title") or "").strip()[:300],
            "pack_id": str(item.get("pack_id") or "").strip()[:64],
            "updated_at": _utc_now(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feedback(video_id, feedback, title, channel_title, pack_id, updated_at)
                VALUES (:video_id, :feedback, :title, :channel_title, :pack_id, :updated_at)
                ON CONFLICT(video_id) DO UPDATE SET
                    feedback = excluded.feedback,
                    title = excluded.title,
                    channel_title = excluded.channel_title,
                    pack_id = excluded.pack_id,
                    updated_at = excluded.updated_at
                """,
                record,
            )
        return record

    def feedback_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM feedback ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def feedback_by_video(self) -> dict[str, str]:
        return {
            str(row["video_id"]): str(row["feedback"])
            for row in self.feedback_rows(limit=1000)
        }

    def preference_examples(self, limit_per_kind: int = 8) -> dict[str, list[dict[str, str]]]:
        positive: list[dict[str, str]] = []
        negative: list[dict[str, str]] = []
        for row in self.feedback_rows(limit=500):
            item = {
                "title": str(row["title"]),
                "channel": str(row["channel_title"]),
                "topic": str(row["pack_id"]),
                "feedback": str(row["feedback"]),
            }
            if row["feedback"] in {"interested", "selected"}:
                if len(positive) < limit_per_kind:
                    positive.append(item)
            elif len(negative) < limit_per_kind:
                negative.append(item)
            if len(positive) >= limit_per_kind and len(negative) >= limit_per_kind:
                break
        return {"positive": positive, "negative": negative}

    def feedback_summary(self) -> dict[str, Any]:
        rows = self.feedback_rows(limit=1000)
        counts: dict[str, int] = {}
        for row in rows:
            kind = str(row["feedback"])
            counts[kind] = counts.get(kind, 0) + 1
        return {"total": len(rows), "counts": counts}


__all__ = ["ALLOWED_FEEDBACK", "DiscoveryStore"]
