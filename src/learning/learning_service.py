import json
import logging
import uuid
from pathlib import Path

from src.discovery_next.query_performance import QueryPerformance
from src.discovery_next.strategy_builder import build_strategy
from src.discovery_next.taxonomy import Taxonomy
from .profile_builder import build_profile
from .schemas import LearningEvent, VideoAnalysis
from .storage import LearningStore, atomic_json, read_json, utc_now
from .video_analyzer import VideoAnalyzer, public_metadata

LOG = logging.getLogger(__name__)


class LearningService:
    def __init__(self, root, analyzer=None):
        self.root = Path(root)
        self.store = LearningStore(root)
        self.analyzer = analyzer

    def record(self, raw, kind="download", source="unknown"):
        metadata = public_metadata(raw, source)
        event = LearningEvent(event_id=f'download:{metadata["video_id"]}' if kind == "download" else uuid.uuid4().hex,
                              video_id=metadata["video_id"], kind=kind, created_at=utc_now())
        with self.store.connect() as db:
            hit = db.execute("SELECT r.payload FROM query_hits h JOIN query_runs r ON r.run_id=h.run_id WHERE h.video_id=? AND h.shown=1 ORDER BY json_extract(r.payload,'$.run_at') DESC LIMIT 1", (event.video_id,)).fetchone()
            if hit:
                origin = json.loads(hit[0])
                metadata["discovered_via"] = "discovery_next"
                metadata["discovery_origin"] = {k: origin.get(k) for k in ("run_id", "query", "domain", "entity", "format", "generator")}
            cursor = db.execute("INSERT OR IGNORE INTO events(event_id,video_id,kind,created_at,metadata) VALUES (?,?,?,?,?)", (event.event_id, event.video_id, event.kind, event.created_at, json.dumps(metadata)))
            if cursor.rowcount:
                QueryPerformance.attribute(db, event)
        LOG.info("learning event persisted video=%s kind=%s", event.video_id, kind)
        return event.event_id

    def rebuild(self):
        taxonomy = Taxonomy(self.root).load()
        performance = QueryPerformance(self.root).history()
        with self.store.connect() as db:
            events = [dict(r) for r in db.execute("SELECT * FROM events")]
            profile, suggestions = build_profile(self.root, events)
            strategy = build_strategy(profile, taxonomy, performance, Taxonomy(self.root).formats)
            for name, value in (("user_content_profile", profile), ("taxonomy_suggestions", suggestions), ("discovery_strategy", strategy)):
                atomic_json(self.root / f"data/learning/{name}.json", value)
        LOG.info("learning profile and strategy updated samples=%s", profile["sample_size"])
        return profile

    def process(self, event_id, progress=None, *, force=False):
        def report(message, value):
            LOG.info(message)
            if progress:
                progress(message, value)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("学习事件不存在")
        metadata = json.loads(row["metadata"])
        path = self.root / "data/learning/videos" / f'{row["video_id"]}.json'
        try:
            report(f'学习事件 {row["video_id"]}：读取元数据', 5)
            existing = read_json(path)
            if existing and not force:
                VideoAnalysis.model_validate(existing["analysis"])
                if existing["analysis"]["video_id"] != row["video_id"]:
                    raise ValueError("已存分析 ID 不匹配")
            else:
                config = read_json(self.root / "config/trending_config.json", {})
                analyzer = self.analyzer or VideoAnalyzer(config, Taxonomy(self.root).load())
                report("学习：调用结构化内容分析", 20)
                analysis = VideoAnalysis.model_validate(analyzer.analyze(metadata)).model_dump()
                if analysis["video_id"] != row["video_id"]:
                    raise ValueError("分析 ID 不匹配")
                with self.store.connect():
                    atomic_json(path, {"version": 1, "analyzed_at": utc_now(), "metadata": metadata, "analysis": analysis})
                report("学习：单视频 JSON 已保存", 65)
            profile = self.rebuild()
            with self.store.connect() as db:
                db.execute("UPDATE events SET status='complete', error='' WHERE event_id=?", (event_id,))
            report("学习：画像和 Discovery Strategy 已更新", 100)
            return profile
        except Exception as exc:
            with self.store.connect() as db:
                db.execute("UPDATE events SET status='pending', error=? WHERE event_id=?", (type(exc).__name__, event_id))
            report(f'学习未完成，事件已保留：{type(exc).__name__}', 0)
            raise

    def queue_pending(self):
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT event_id FROM events WHERE status='pending'")]
        for event_id in ids:
            enqueue_learning(self.root, event_id)
        return len(ids)


def enqueue_learning(root, event_id, *, force=False):
    from src.control_panel.jobs import JobStore
    root = Path(root)
    store = JobStore(root / "work/control_panel/control_panel.sqlite3", root / "logs/control_panel/jobs")
    return store.enqueue("learning", event_id, {"event_id": event_id, "force": force}, resource_class="gpu_heavy", reuse_active_kinds={"learning"})


def on_download_success(root, metadata, source):
    """Durable hook shared by URL, candidate and repair downloads; never calls AI."""
    try:
        service = LearningService(root)
        event_id = service.record(metadata, source=source)
        with service.store.connect() as db:
            status = db.execute("SELECT status FROM events WHERE event_id=?", (event_id,)).fetchone()[0]
        if status != "complete":
            enqueue_learning(root, event_id)
        LOG.info("learning download queued video=%s", metadata.get("id"))
    except Exception as exc:
        # A learning failure must not turn a successfully downloaded video into failure.
        LOG.warning("learning enqueue failed (%s); retry through learning service", type(exc).__name__)
