import json
import uuid

from src.learning.storage import LearningStore, utc_now


class QueryPerformance:
    def __init__(self, root):
        self.store = LearningStore(root)

    def start(self, query, hours):
        record = {**query, "run_id": uuid.uuid4().hex, "time_window": hours, "run_at": utc_now(),
                  "status": "running", **{key: 0 for key in ("returned_count", "new_unique_count", "eligible_count", "ai_high_quality_count", "shown_count", "download_count", "positive_feedback_count", "negative_feedback_count")}}
        self.save(record, [])
        return record

    def save(self, record, hits, shown=()):
        shown = set(shown)
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO query_runs VALUES (?, ?)", (record["run_id"], json.dumps(record)))
            for video_id in hits:
                db.execute("INSERT INTO query_hits VALUES (?, ?, ?) ON CONFLICT(run_id,video_id) DO UPDATE SET shown=max(shown,excluded.shown)", (record["run_id"], video_id, int(video_id in shown)))

    @staticmethod
    def attribute(db, event):
        # Credit the last actual impression, not every query that returned a video.
        hit = db.execute("SELECT h.run_id FROM query_hits h JOIN query_runs r ON r.run_id=h.run_id WHERE h.video_id=? AND h.shown=1 ORDER BY json_extract(r.payload,'$.run_at') DESC LIMIT 1", (event.video_id,)).fetchone()
        if hit:
            db.execute("INSERT OR IGNORE INTO attributions VALUES (?, ?, ?)", (event.event_id, hit[0], event.kind))

    def history(self):
        with self.store.connect() as db:
            records = [json.loads(r[0]) for r in db.execute("SELECT payload FROM query_runs ORDER BY rowid DESC LIMIT 1000")]
            for record in records:
                for row in db.execute("SELECT a.kind, count(*) AS n FROM attributions a JOIN events e ON e.event_id=a.event_id WHERE a.run_id=? AND (a.kind='download' OR NOT EXISTS (SELECT 1 FROM events newer WHERE newer.video_id=e.video_id AND newer.kind!='download' AND (newer.created_at,newer.event_id)>(e.created_at,e.event_id))) GROUP BY a.kind", (record["run_id"],)):
                    key = "download_count" if row[0] == "download" else "positive_feedback_count" if row[0] in {"interested", "strong_interest"} else "negative_feedback_count"
                    record[key] += row[1]
        return records

    def seen_ids(self):
        with self.store.connect() as db:
            return {r[0] for r in db.execute("SELECT DISTINCT video_id FROM query_hits")}


def utility(records, domain, entity, form):
    history = [r for r in records if (r["domain"], r["entity"], r["format"]) == (domain, entity, form)]
    returned = sum(r["returned_count"] for r in history)
    reward = sum(r["eligible_count"] * 0.1 + r["ai_high_quality_count"] * 0.3 + r["download_count"] * 4 + r["positive_feedback_count"] * 5 - r["negative_feedback_count"] * 5 for r in history)
    return reward / (10 + returned)
