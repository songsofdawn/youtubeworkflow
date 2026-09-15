import json
import math
import uuid
from datetime import datetime, timedelta, timezone

from src.learning.storage import LearningStore, utc_now
from .query_planner import normalize_query

METRICS = ("returned_count", "new_unique_count", "duplicate_count", "eligible_count", "ai_selected_count",
           "ai_accept_count", "ai_high_quality_count", "shown_count", "download_count",
           "positive_feedback_count", "negative_feedback_count")


def performance_score(record):
    returned = max(1, int(record.get("returned_count", 0)))
    new_rate = record.get("new_unique_count", 0) / returned
    eligible_rate = record.get("eligible_count", 0) / returned
    quality_rate = record.get("ai_high_quality_count", 0) / max(1, record.get("ai_selected_count", record.get("eligible_count", 0)))
    downstream = min(1.0, record.get("download_count", 0) * .35 + record.get("positive_feedback_count", 0) * .45)
    negative = min(1.0, record.get("negative_feedback_count", 0) * .5)
    return round(.34 * new_rate + .26 * eligible_rate + .22 * quality_rate + .28 * downstream - .35 * negative, 5)


class QueryPerformance:
    def __init__(self, root, config=None):
        self.store = LearningStore(root)
        cfg = (config or {}).get("discovery_next", {})
        self.decay_days = float(cfg.get("query_performance_decay_days", 45))
        self.bad_threshold = float(cfg.get("bad_query_suppression_threshold", .08))
        self.cooldown_hours = int(cfg.get("query_cooldown_hours", 72))

    def start(self, query, hours):
        record = {**query, "run_id": uuid.uuid4().hex, "time_window": hours, "run_at": utc_now(),
                  "normalized_query": query.get("normalized_query") or normalize_query(query.get("query", "")),
                  "status": "running", **{key: 0 for key in METRICS}}
        self.save(record, [])
        return record

    def save(self, record, hits, shown=()):
        shown = set(shown)
        returned = max(1, record.get("returned_count", 0))
        record.update(duplicate_rate=record.get("duplicate_count", 0) / returned,
                      new_unique_rate=record.get("new_unique_count", 0) / returned,
                      eligible_rate=record.get("eligible_count", 0) / returned,
                      high_quality_rate=record.get("ai_high_quality_count", 0) / max(1, record.get("ai_selected_count", 0)))
        record["performance_score"] = performance_score(record)
        if record.get("status") == "complete" and record["performance_score"] < self.bad_threshold and record.get("returned_count", 0):
            record["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(hours=self.cooldown_hours)).isoformat()
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO query_runs VALUES (?, ?)", (record["run_id"], json.dumps(record)))
            for video_id in hits:
                db.execute("INSERT INTO query_hits VALUES (?, ?, ?) ON CONFLICT(run_id,video_id) DO UPDATE SET shown=max(shown,excluded.shown)", (record["run_id"], video_id, int(video_id in shown)))

    @staticmethod
    def attribute(db, event):
        hit = db.execute("SELECT h.run_id FROM query_hits h JOIN query_runs r ON r.run_id=h.run_id WHERE h.video_id=? AND h.shown=1 ORDER BY json_extract(r.payload,'$.run_at') DESC LIMIT 1", (event.video_id,)).fetchone()
        if hit:
            db.execute("INSERT OR IGNORE INTO attributions VALUES (?, ?, ?)", (event.event_id, hit[0], event.kind))

    def history(self):
        with self.store.connect() as db:
            records = [json.loads(r[0]) for r in db.execute("SELECT payload FROM query_runs ORDER BY rowid DESC LIMIT 2000")]
            for record in records:
                for key in METRICS:
                    record.setdefault(key, 0)
                for row in db.execute("SELECT a.kind, count(*) AS n FROM attributions a JOIN events e ON e.event_id=a.event_id WHERE a.run_id=? AND (a.kind='download' OR NOT EXISTS (SELECT 1 FROM events newer WHERE newer.video_id=e.video_id AND newer.kind!='download' AND (newer.created_at,newer.event_id)>(e.created_at,e.event_id))) GROUP BY a.kind", (record["run_id"],)):
                    key = "download_count" if row[0] == "download" else "positive_feedback_count" if row[0] in {"interested", "strong_interest"} else "negative_feedback_count"
                    record[key] += row[1]
                try:
                    age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(record["run_at"])).total_seconds() / 86400)
                except (KeyError, ValueError, TypeError):
                    age = 0
                record["performance_score"] = round(performance_score(record) * math.exp(-age / max(1, self.decay_days)), 5)
        return records

    def seen_ids(self):
        with self.store.connect() as db:
            return {r[0] for r in db.execute("SELECT DISTINCT video_id FROM query_hits")}


def utility(records, domain, entity, form):
    history = [r for r in records if r.get("domain") == domain and (not entity or r.get("entity") == entity) and (not form or r.get("format") == form)]
    return sum(float(row.get("performance_score", performance_score(row))) for row in history) / max(1, len(history))
