"""Persistent YouTube engagement snapshots and real incremental velocity."""
from __future__ import annotations

from datetime import datetime, timezone

from src.learning.storage import LearningStore


class MetricSnapshotStore:
    def __init__(self, root):
        self.store = LearningStore(root)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def record(self, video_id, view_count, like_count, comment_count, *, observed_at=None):
        observed_at = self._aware(observed_at or datetime.now(timezone.utc))
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO video_metric_snapshots(video_id, observed_at, view_count, like_count, comment_count) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(video_id, observed_at) DO UPDATE SET view_count=excluded.view_count, like_count=excluded.like_count, comment_count=excluded.comment_count",
                (video_id, observed_at.isoformat(), int(view_count or 0), int(like_count or 0), int(comment_count or 0)),
            )

    def rates(self, video_id, *, fallback_views_per_hour=0.0, fallback_likes_per_hour=0.0, fallback_comments_per_hour=0.0):
        """Return real per-hour rates from the two most recent snapshots.

        When fewer than two snapshots exist, or the elapsed time is not positive,
        callers get the supplied fallback rates so existing ``views / age_hours``
        behaviour remains intact.
        """
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT view_count, like_count, comment_count, observed_at "
                "FROM video_metric_snapshots WHERE video_id=? "
                "ORDER BY observed_at DESC, rowid DESC LIMIT 2",
                (video_id,),
            ).fetchall()
        if len(rows) < 2:
            return {
                "views_per_hour": float(fallback_views_per_hour),
                "likes_per_hour": float(fallback_likes_per_hour),
                "comments_per_hour": float(fallback_comments_per_hour),
                "source": "age_fallback",
            }
        latest, previous = rows
        try:
            latest_at = datetime.fromisoformat(latest["observed_at"])
            previous_at = datetime.fromisoformat(previous["observed_at"])
        except (KeyError, TypeError, ValueError):
            latest_at = previous_at = None
        if latest_at is None or previous_at is None:
            hours = 0.0
        else:
            hours = (self._aware(latest_at) - self._aware(previous_at)).total_seconds() / 3600
        if hours <= 0:
            return {
                "views_per_hour": float(fallback_views_per_hour),
                "likes_per_hour": float(fallback_likes_per_hour),
                "comments_per_hour": float(fallback_comments_per_hour),
                "source": "age_fallback",
            }
        return {
            "views_per_hour": max(0.0, float(latest["view_count"] - previous["view_count"]) / hours),
            "likes_per_hour": max(0.0, float(latest["like_count"] - previous["like_count"]) / hours),
            "comments_per_hour": max(0.0, float(latest["comment_count"] - previous["comment_count"]) / hours),
            "source": "incremental",
        }
