import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from pydantic import ValidationError

from src.control_panel.jobs import JobStore, WorkflowWorker
from src.control_panel.youtube import TargetedYouTubeSearch
from src.discovery_next.query_generator import generate_queries
from src.discovery_next.query_performance import QueryPerformance, utility
from src.discovery_next.service import DiscoveryNextService
from src.discovery_next.strategy_builder import build_strategy
from src.discovery_next.taxonomy import Taxonomy
from src.learning.learning_service import LearningService, on_download_success
from src.learning.schemas import VideoAnalysis
from src.learning.storage import atomic_json, read_json
from src.learning.video_analyzer import public_metadata


VIDEO = "abcdefghijk"
NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def analysis(video_id=VIDEO):
    return {"video_id": video_id, "domains": [{"name": "minecraft", "score": 0.9}],
            "topics": ["civilization simulation"], "entities": ["civilization", "AI agents", "100 players"],
            "formats": ["simulation"], "content_traits": {k: 0.8 for k in ("novelty", "visual_payoff", "story_strength", "knowledge_value", "localization_value", "result_payoff")},
            "positive_signals": ["visible result"], "negative_signals": [], "search_concepts": ["civilization simulation"],
            "taxonomy_suggestions": [{"name": "civilization_simulation", "label": "文明模拟", "confidence": 0.9, "suggested_entities": ["AI agents"], "suggested_formats": ["simulation"]}]}


def metadata(video_id=VIDEO):
    return {"id": video_id, "title": "Minecraft civilization simulation", "description": "100 players build a civilization", "channel_id": "UC" + "a" * 22, "channel": "Simulation Lab"}


class FakeYouTube:
    def __init__(self, *, title="Minecraft civilization simulation", fail_details=False):
        self.calls = []
        self.title = title
        self.fail_details = fail_details

    def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint == "search":
            return {"items": [{"id": {"videoId": VIDEO}}]}
        if self.fail_details:
            raise RuntimeError("offline injected failure")
        return {"items": [{"id": VIDEO, "snippet": {"title": self.title, "description": "Minecraft civilization simulation experiment", "channelTitle": "Lab", "channelId": "UC" + "a" * 22, "publishedAt": "2026-09-14T12:00:00Z", "defaultAudioLanguage": "en", "liveBroadcastContent": "none", "thumbnails": {}}, "contentDetails": {"duration": "PT10M", "caption": "true"}, "statistics": {"viewCount": "200000", "likeCount": "1000", "commentCount": "50"}, "status": {"privacyStatus": "public", "embeddable": True}}]}


class DiscoveryNextTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"discovery_max_search_requests": 3, "discovery_llm": {"enabled": False}, "english_only": True}
        atomic_json(self.root / "config/trending_config.json", self.config)
        self.analyzer = Mock()
        self.analyzer.analyze.side_effect = lambda row: analysis(row["video_id"])
        self.service = LearningService(self.root, self.analyzer)

    def test_download_learning_closed_loop_and_idempotency(self):
        event_id = self.service.record(metadata(), source="manual")
        profile = self.service.process(event_id)
        self.assertEqual(profile["sample_size"], 1)
        self.assertEqual(profile["format_preferences"]["simulation"], 0.35)
        self.assertEqual(self.service.record(metadata()), event_id)
        self.service.process(event_id)
        self.analyzer.analyze.assert_called_once()
        saved = read_json(self.root / f"data/learning/videos/{VIDEO}.json")
        self.assertEqual(saved["analysis"]["video_id"], VIDEO)
        taxonomy = Taxonomy(self.root)
        domains = [d for d in taxonomy.load() if d["id"] == "minecraft"]
        cold = build_strategy({}, domains, [], taxonomy.formats)
        learned = build_strategy(profile, domains, [], taxonomy.formats)
        self.assertTrue(any(c["entity"] == "ai agents" for c in learned["priority_combinations"]))
        self.assertGreater(learned["priority_combinations"][0]["weight"], cold["priority_combinations"][0]["weight"])
        queries = generate_queries(learned, domains, taxonomy.formats, 15, seed=42)
        self.assertTrue(any("civilization simulation" in q["query"] for q in queries))
        self.assertEqual(self.service.rebuild(), profile)

    def test_invalid_reanalysis_preserves_good_json_and_profile(self):
        event_id = self.service.record(metadata())
        self.service.process(event_id)
        paths = [self.root / f"data/learning/videos/{VIDEO}.json", self.root / "data/learning/user_content_profile.json"]
        before = [p.read_bytes() for p in paths]
        invalid = analysis()
        invalid["content_traits"]["novelty"] = float("nan")
        self.analyzer.analyze.side_effect = None
        self.analyzer.analyze.return_value = invalid
        with self.assertRaises(ValidationError):
            self.service.process(event_id, force=True)
        self.assertEqual([p.read_bytes() for p in paths], before)
        with self.service.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM events").fetchone()[0], "pending")

    def test_wrong_id_and_missing_fields_do_not_create_video_record(self):
        event = self.service.record(metadata())
        self.analyzer.analyze.side_effect = None
        self.analyzer.analyze.return_value = analysis("12345678901")
        with self.assertRaises(ValueError):
            self.service.process(event)
        self.assertFalse((self.root / f"data/learning/videos/{VIDEO}.json").exists())
        with self.assertRaises(ValidationError):
            VideoAnalysis.model_validate({"video_id": VIDEO})
        with self.assertRaises(ValidationError):
            self.service.record({"id": "../bad/path"})

    def test_latest_feedback_replaces_prior_preference(self):
        self.service.process(self.service.record(metadata()))
        self.service.process(self.service.record(metadata(), "strong_interest"))
        self.assertEqual(self.service.rebuild()["format_preferences"]["simulation"], 1.0)
        self.service.process(self.service.record(metadata(), "not_interested"))
        profile = self.service.rebuild()
        self.assertEqual(profile["sample_size"], 1)
        self.assertNotIn("simulation", profile["format_preferences"])
        self.assertGreater(profile["negative_preferences"]["simulation"], 0)

    def test_new_domain_requires_no_query_list(self):
        taxonomy = Taxonomy(self.root)
        domain = {"id": "extreme_survival", "label": "Extreme Survival", "description": "Extreme environments", "entities": ["arctic", "desert"], "formats": ["extreme_survival", "challenge"], "positive_traits": ["visible_result"], "negative_intents": ["livestream"]}
        domains = taxonomy.save([domain])
        self.assertNotIn("query", domains[0])
        queries = generate_queries(build_strategy({}, domains, []), domains, taxonomy.formats, 3, seed=1)
        self.assertEqual(len(queries), 3)
        self.assertTrue(all(q["entity"] in domain["entities"] for q in queries))
        with self.assertRaises(ValidationError):
            taxonomy.save([{**domain, "query": "old keyword"}])
        with self.assertRaises(ValueError):
            taxonomy.save([{**domain, "formats": ["unknown"]}])

    def test_real_performance_not_run_count_drives_weight(self):
        good = {"domain": "minecraft", "entity": "mods", "format": "simulation", "returned_count": 10, "eligible_count": 8, "ai_high_quality_count": 5, "download_count": 2, "positive_feedback_count": 1, "negative_feedback_count": 0}
        bad = {**good, "eligible_count": 0, "ai_high_quality_count": 0, "download_count": 0, "positive_feedback_count": 0}
        self.assertGreater(utility([good] * 10, "minecraft", "mods", "simulation"), utility([bad], "minecraft", "mods", "simulation"))

    def test_search_classification_is_independent_of_source_domain(self):
        domains = [d for d in Taxonomy(self.root).load() if d["id"] in {"minecraft", "chemistry"}]
        fake_query = {"domain": "chemistry", "entity": "materials", "format": "experiment", "query": "materials experiment", "order": "relevance"}
        with patch("src.discovery_next.service.generate_queries", return_value=[fake_query]):
            result = DiscoveryNextService(self.root).run(youtube=FakeYouTube(), packs=domains, selected_ids=[d["id"] for d in domains], hours=72, per_pack=5, config=self.config, now=NOW)
        self.assertEqual(result["results"][0]["pack_id"], "minecraft")
        self.assertEqual(result["results"][0]["rights_status"], "PENDING")
        run = QueryPerformance(self.root).history()[0]
        self.assertEqual((run["returned_count"], run["eligible_count"], run["shown_count"]), (1, 1, 1))
        event = self.service.record(metadata())
        self.service.process(event)
        saved = read_json(self.root / f"data/learning/videos/{VIDEO}.json")
        self.assertEqual(saved["metadata"]["discovery_origin"]["domain"], "chemistry")
        self.assertEqual(QueryPerformance(self.root).history()[0]["download_count"], 1)
        self.service.record(metadata())
        self.assertEqual(QueryPerformance(self.root).history()[0]["download_count"], 1)
        self.service.record(metadata(), "interested")
        self.service.record(metadata(), "not_interested")
        run = QueryPerformance(self.root).history()[0]
        self.assertEqual((run["positive_feedback_count"], run["negative_feedback_count"]), (0, 1))

    def test_search_failure_preserves_returned_ids_and_no_impression(self):
        domains = [d for d in Taxonomy(self.root).load() if d["id"] == "minecraft"]
        result = DiscoveryNextService(self.root).run(youtube=FakeYouTube(fail_details=True), packs=domains, selected_ids=["minecraft"], hours=72, per_pack=5, config=self.config, now=NOW)
        self.assertEqual(result["results"], [])
        run = QueryPerformance(self.root).history()[0]
        self.assertEqual(run["returned_count"], 1)
        self.assertEqual(run["status"], "failed")
        self.service.record(metadata())
        self.assertEqual(QueryPerformance(self.root).history()[0]["download_count"], 0)

    def test_hot_protection_and_hard_filters(self):
        domains = [d for d in Taxonomy(self.root).load() if d["id"] == "minecraft"]
        bad = analysis()
        bad["content_traits"] = {k: 0.0 for k in bad["content_traits"]}
        bad["domains"] = []
        analyzer = Mock()
        analyzer.analyze.return_value = bad
        config = {**self.config, "discovery_llm": {"enabled": True}}
        service = DiscoveryNextService(self.root, analyzer)
        args = dict(youtube=FakeYouTube(), packs=domains, selected_ids=["minecraft"], hours=72, per_pack=5, config=config, now=NOW)
        self.assertEqual(len(service.run(**args)["results"]), 1)
        self.assertEqual(service.run(**args, known_video_ids={VIDEO})["results"], [])
        self.assertEqual(service.run(**args, minimum_duration_seconds=1000)["results"], [])

    def test_download_hook_queues_gpu_without_model_call(self):
        with patch("src.learning.video_analyzer.VideoAnalyzer.analyze", side_effect=AssertionError("No AI in download")):
            on_download_success(self.root, metadata(), "manual")
            on_download_success(self.root, metadata(), "manual")
        store = JobStore(self.root / "work/control_panel/control_panel.sqlite3", self.root / "logs/control_panel/jobs")
        jobs = store.queued(resource_class="gpu_heavy")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "learning")
        self.assertEqual(WorkflowWorker.initial_resource("learning", {}), "gpu_heavy")
        self.assertEqual(self.service.queue_pending(), 1)
        self.assertEqual(len(store.queued(resource_class="gpu_heavy")), 1)

    def test_worker_executes_learning_and_emits_stage_log(self):
        on_download_success(self.root, metadata(), "manual")
        store = JobStore(self.root / "work/control_panel/control_panel.sqlite3", self.root / "logs/control_panel/jobs")
        job = store.claim_next({"learning"}, {"gpu_heavy"})
        worker = WorkflowWorker(self.root, store, Mock(), Mock())
        config = {"discovery_llm": {"enabled": True}}
        atomic_json(self.root / "config/trending_config.json", config)
        with patch("src.learning.video_analyzer.VideoAnalyzer.analyze", return_value=analysis()):
            worker._execute(job)
        self.assertEqual(store.get(job["id"])["status"], "completed")
        log = Path(job["log_path"]).read_text(encoding="utf-8")
        self.assertIn("单视频 JSON 已保存", log)
        self.assertIn("Discovery Strategy 已更新", log)

    def test_cancel_before_query_does_not_consume_search_budget(self):
        client = FakeYouTube()
        domains = [d for d in Taxonomy(self.root).load() if d["id"] == "minecraft"]
        with self.assertRaises(InterruptedError):
            DiscoveryNextService(self.root).run(youtube=client, packs=domains, selected_ids=["minecraft"], hours=72,
                                               per_pack=5, config=self.config, now=NOW,
                                               cancelled=Mock(side_effect=InterruptedError))
        self.assertEqual(client.calls, [])
        self.assertEqual(QueryPerformance(self.root).history(), [])

    def test_suggestions_have_actual_evidence_and_never_modify_taxonomy(self):
        taxonomy = Taxonomy(self.root)
        taxonomy.save(taxonomy.load())
        before = taxonomy.path.read_bytes()
        for video in [VIDEO, "12345678901"]:
            self.service.process(self.service.record(metadata(video)))
        suggestions = read_json(self.root / "data/learning/taxonomy_suggestions.json")["taxonomy_suggestions"]
        self.assertEqual(suggestions[0]["evidence_count"], 2)
        self.assertEqual(taxonomy.path.read_bytes(), before)

    def test_public_metadata_excludes_private_fields(self):
        result = public_metadata({**metadata(), "http_headers": {"Cookie": "secret"}, "filepath": "C:/private", "subtitles": {"en": "private"}})
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_panel_ignores_legacy_keyword_config(self):
        atomic_json(self.root / "config/discovery_keywords.json", {"packs": [{"id": "legacy_only", "query": "wrong"}]})
        catalog = TargetedYouTubeSearch(self.root).discovery_catalog()
        self.assertTrue(any(d["id"] == "extreme_survival" for d in catalog))
        self.assertFalse(any("query" in d or d["id"] == "legacy_only" for d in catalog))
