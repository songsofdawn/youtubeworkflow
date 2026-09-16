"""Discovery Next phase two orchestration."""
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.candidate_analysis import assess_language
from src.discovery.ollama_client import OllamaDiscoveryClient, OllamaSettings
from src.fetch_daily_candidates import best_thumbnail, format_duration, parse_iso8601_duration
from src.learning.learning_service import LearningService, enqueue_learning
from src.learning.storage import atomic_json, read_json
from src.learning.schemas import VideoAnalysis
from src.learning.video_analyzer import VideoAnalyzer, public_metadata
from .candidate_preselector import CandidatePreselector, semantic_key
from .candidate_ranker import CandidateRanker
from .metric_snapshots import MetricSnapshotStore
from .query_generator import generate_queries
from .query_performance import QueryPerformance
from .recall import RecallExecutor
from .strategy_builder import build_strategy
from .taxonomy import Taxonomy
from .query_planner import normalize_query, query_similarity


def contains(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(str(term).casefold().replace("_", " ")) + r"(?!\w)", text.casefold()))


def classify(row, domains):
    text = row["title"] + " " + row["description"] + " " + " ".join(row["tags"])
    return [d["id"] for d in domains if any(contains(text, term) for term in [d["id"].replace("_", " "), *d["entities"]])]


class DiscoveryNextService:
    def __init__(self, root, analyzer=None, query_client=None):
        self.root, self.analyzer, self.query_client = Path(root), analyzer, query_client
        self.performance = QueryPerformance(root)
        self.metric_snapshots = MetricSnapshotStore(root)

    def public_settings(self, config):
        settings = OllamaSettings.from_config(config).public_dict()
        return {**settings, "architecture": "discovery_next_v2", "max_search_requests": min(100, int(config.get("discovery_max_search_requests", 96))),
                "recall_target": int(config.get("discovery_recall_target", 1000)),
                "metadata_max_candidates": int(config.get("discovery_llm", {}).get("metadata_max_candidates", 120)),
                "minimum_duration_minutes": int(config.get("discovery_min_duration_seconds", 300)) // 60,
                "maximum_duration_minutes": int(config.get("discovery_max_duration_seconds", 10800)) // 60,
                "default_ranking_mode": str(config.get("discovery_default_ranking_mode", "potential")),
                "default_scope": "auto", "default_strength": "standard", "feedback": {},
                "learning_sample_size": read_json(self.root / "data/learning/user_content_profile.json", {}).get("sample_size", 0)}

    def health(self, config):
        status = OllamaDiscoveryClient(OllamaSettings.from_config(config)).health()
        return {**self.public_settings(config), "reachable": bool(status.get("reachable")), "model_ready": bool(status.get("model_ready"))}

    def record_feedback(self, item, feedback):
        aliases = {"selected": "interested", "boring": "too_common", "irrelevant": "off_topic", "duplicate": "not_interested", "wrong_language": "off_topic", "unsafe": "not_interested"}
        service = LearningService(self.root)
        event_id = service.record(item, aliases.get(feedback, feedback), "discovery_next")
        service.rebuild(); enqueue_learning(self.root, event_id)
        return {"video_id": item["video_id"], "feedback": feedback, "event_id": event_id}

    def _candidate(self, video_id, entry, domains, profile, *, hours, now, hot_views, hot_vph):
        item, attrs = entry["item"], entry["attributions"]
        snippet, content, stats, status = (item.get(k, {}) for k in ("snippet", "contentDetails", "statistics", "status"))
        duration = parse_iso8601_duration(content.get("duration", ""))
        try:
            published = datetime.fromisoformat(snippet.get("publishedAt", "").replace("Z", "+00:00")); age = (now - published).total_seconds() / 3600
        except (ValueError, TypeError):
            age = hours + 1
        views = int(stats.get("viewCount") or 0)
        likes = int(stats.get("likeCount") or 0)
        comments = int(stats.get("commentCount") or 0)
        self.metric_snapshots.record(video_id, views, likes, comments, observed_at=now)
        rates = self.metric_snapshots.rates(
            video_id,
            fallback_views_per_hour=views / max(age, 1),
            fallback_likes_per_hour=0.0,
            fallback_comments_per_hour=0.0,
        )
        vph = rates["views_per_hour"]
        row = {"video_id": video_id, "title": str(snippet.get("title", video_id)), "description": str(snippet.get("description", "")),
               "channel_title": str(snippet.get("channelTitle", "")), "channel_id": str(snippet.get("channelId", "")), "tags": snippet.get("tags", []),
               "category": snippet.get("categoryId", ""), "published_at": snippet.get("publishedAt", ""), "duration_seconds": duration,
               "duration": format_duration(duration), "view_count": views, "like_count": likes,
               "comment_count": comments, "views_per_hour": vph, "age_hours": age,
               "likes_per_hour": rates["likes_per_hour"], "comments_per_hour": rates["comments_per_hour"],
               "metric_rate_source": rates["source"],
               "has_caption": str(content.get("caption")) == "true", "thumbnail_url": best_thumbnail(snippet),
               "youtube_url": f"https://www.youtube.com/watch?v={video_id}", "license": status.get("license", "unknown"),
               "rights_status": "PENDING", "embeddable": status.get("embeddable", True), "hot_protected": views >= hot_views or vph >= hot_vph,
               "llm_status": "not_scored", "ai_potential_score": 50.0, "opportunity_score": 50.0,
               "story_strength": .5, "visual_payoff": .5, "knowledge_value": .5, "result_payoff": .5,
               "source_query_run_ids": [a["run_id"] for a in attrs], "matched_queries": list(dict.fromkeys(a["query"] for a in attrs)),
               "matched_domains": list(dict.fromkeys(a["domain"] for a in attrs)), "matched_recall_sources": list(dict.fromkeys(a["recall_source"] for a in attrs)),
               "matched_intents": list(dict.fromkeys(a["intent"] for a in attrs)),
               "matched_semantic_clusters": list(dict.fromkeys(normalize_query(a.get("query", "")) for a in attrs)),
               "first_seen_at": min((a.get("run_at", "") for a in attrs), default=""),
               "last_seen_at": max((a.get("run_at", "") for a in attrs), default=""),
               "semantic_cluster": semantic_key(str(snippet.get("title", ""))),
               "query_relevance": .7 if attrs else .4, "exploration_fit": 1.0 if any(a["recall_source"] in {"exploration", "coverage", "cross_domain"} for a in attrs) else 0,
               "emerging_fit": 1.0 if any(a["recall_source"] == "emerging" for a in attrs) else 0,
               "selection_reason": "多路召回后等待本地 AI 内容评价"}
        row["matched_pack_ids"] = classify(row, domains)
        prefs = profile.get("domain_preferences", {}); max_pref = max([float(v) for v in prefs.values()] or [1])
        row["user_preference_match"] = max([float(prefs.get(d, 0)) / max_pref for d in row["matched_pack_ids"]] or [.25])
        row["novelty"] = .65 if len(row["matched_queries"]) == 1 else .45
        row["semantic_uniqueness"] = .7
        return row

    def run(self, *, youtube, packs, selected_ids=None, hours, per_pack, config, known_video_ids=None, known_titles=None,
            minimum_duration_seconds=None, maximum_duration_seconds=None, ranking_mode="potential", discovery_scope="auto",
            search_strength="standard", now=None, progress=None, cancelled=None):
        if ranking_mode not in {"hot", "potential"} or discovery_scope not in {"auto", "manual"} or search_strength not in {"quick", "standard", "deep"}:
            raise ValueError("智能发现模式参数无效")
        def notify(message, value):
            if cancelled: cancelled()
            if progress: progress(message, value)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
        domains = [d for d in packs if d.get("enabled", True)]
        manual_focus = list(selected_ids or []) if discovery_scope == "manual" else []
        profile = LearningService(self.root).rebuild(); history = self.performance.history()
        notify("Discovery Next：自动分配领域与搜索预算", 5)
        from .query_planner import QueryPlanner
        planner = QueryPlanner(config, client=self.query_client) if self.query_client is not None else None
        strategy = build_strategy(profile, domains, history, Taxonomy(self.root).formats, config=config, mode=ranking_mode,
                                  strength=search_strength, manual_focus=manual_focus, planner=planner, now=now)
        strategy["query_plan"] = generate_queries(strategy, domains, Taxonomy(self.root).formats, strategy["query_budget"], ranking_mode=ranking_mode)
        atomic_json(self.root / "data/learning/discovery_strategy.json", strategy)
        seen = self.performance.seen_ids() | set(known_video_ids or ()); known = set(known_video_ids or ())
        known_titles = {str(value).casefold().strip() for value in known_titles or []}
        resources, records, warnings = RecallExecutor(youtube, self.performance, config).execute(
            strategy["query_plan"], hours=hours, now=now, seen=seen, notify=notify,
            recall_budget=strategy.get("recall_budget", {}), search_budget=strategy["search_budget"])
        minimum = minimum_duration_seconds or int(config.get("discovery_min_duration_seconds", 300))
        maximum = maximum_duration_seconds or int(config.get("discovery_max_duration_seconds", 10800))
        def threshold(name, fallback): return float(config.get(name + "_by_window", {}).get(str(hours), config.get(name, fallback)))
        hot_views, hot_vph = threshold("discovery_hot_view_count", 100000), threshold("discovery_hot_views_per_hour", 5000)
        excluded, rows = Counter(), []
        for video_id, entry in resources.items():
            row = self._candidate(video_id, entry, domains, profile, hours=hours, now=now, hot_views=hot_views, hot_vph=hot_vph)
            text = row["title"] + " " + row["description"]; reason = ""
            if video_id in known or row["title"].casefold().strip() in known_titles: reason = "known_video"
            elif not minimum <= row["duration_seconds"] <= maximum: reason = "duration"
            elif not 0 <= row["age_hours"] <= hours: reason = "outside_window"
            elif not row["embeddable"]: reason = "unavailable"
            elif any(contains(text, term) for term in config.get("hard_exclude_phrases", [])): reason = "risk_phrase"
            elif config.get("exclude_shorts", True) and row["duration_seconds"] <= int(config.get("shorts_max_duration_seconds", 180)) and "#shorts" in text.casefold(): reason = "shorts"
            elif config.get("english_only", True) and not assess_language(entry["item"].get("snippet", {}), row["has_caption"], config.get("language_markers", []))["is_english"]: reason = "non_english"
            if reason: excluded[reason] += 1; continue
            penalty = sum(min(20, 10 * float(weight)) for term, weight in profile.get("negative_preferences", {}).items() if contains(text, term))
            if profile.get("channel_preferences", {}).get(row["channel_id"], 0) < 0: penalty += 30
            row["negative_preference_penalty"] = penalty; row["seen_in_previous_search"] = video_id in seen
            rows.append(row)
        domain_counts = Counter(d for row in rows for d in row["matched_pack_ids"])
        for row in rows: row["domain_pool_count"] = min([domain_counts[d] for d in row["matched_pack_ids"]] or [len(rows)])
        # Near-title duplicates are not always discarded outright; they receive a diversity
        # penalty so exact-ID dedup remains authoritative while near-duplicate content is
        # still demoted instead of consuming the full AI budget.
        normalized_rows = {}
        for row in rows:
            key = normalize_query(row["title"])
            normalized_rows.setdefault(key, []).append(row)
        for group in normalized_rows.values():
            if len(group) > 1:
                for row in group[1:]:
                    row["duplicate_penalty"] = row.get("duplicate_penalty", 0) + 18
                    row["semantic_uniqueness"] = min(row.get("semantic_uniqueness", .7), .45)
        notify("Discovery Next：多样化分桶预选", 42)
        configured_ai = int(config.get("discovery_llm", {}).get("metadata_max_candidates", strategy["ai_candidate_budget"]))
        preselection_budget = min(len(rows), configured_ai, strategy["ai_candidate_budget"])
        fallback_domain = max((key for key in strategy["domain_allocation"] if key != "exploration"),
                              key=lambda key: strategy["domain_allocation"].get(key, 0), default=None)
        fallback_domain = fallback_domain or (domains[0]["id"] if domains else "")
        def primary_domain(row):
            matches = row.get("matched_pack_ids") or row.get("matched_domains")
            return matches[0] if matches else fallback_domain
        preselector = CandidatePreselector(config)
        preselected, bucket_counts = preselector.select(rows, preselection_budget, domain_limit=max(per_pack, preselector.domain_limit))
        preselected_ids = {row["video_id"] for row in preselected}
        preselected_domain_counts = Counter(primary_domain(row) for row in preselected)
        for domain in [d["id"] for d in domains]:
            if preselected_domain_counts[domain] >= per_pack or len(preselected) >= preselection_budget:
                continue
            remaining = sorted(
                (row for row in rows if row["video_id"] not in preselected_ids and primary_domain(row) == domain),
                key=lambda row: row.get("views_per_hour", 0),
                reverse=True,
            )
            for row in remaining:
                if preselected_domain_counts[domain] >= per_pack or len(preselected) >= preselection_budget:
                    break
                preselected.append(row)
                preselected_ids.add(row["video_id"])
                preselected_domain_counts[domain] += 1
        analyzer = self.analyzer or VideoAnalyzer(config, domains); scored = 0
        if config.get("discovery_llm", {}).get("enabled", False):
            for index, row in enumerate(preselected):
                notify(f"Discovery Next：Qwen 内容评价 {index + 1}/{len(preselected)}", 48 + int(35 * index / max(1, len(preselected))))
                try:
                    analysis = VideoAnalysis.model_validate(analyzer.analyze(public_metadata(row, "discovery_next"))).model_dump()
                    if analysis["video_id"] != row["video_id"]: raise ValueError("候选分析 ID 不匹配")
                    matches = [d["name"] for d in analysis["domains"] if d["score"] >= .5 and d["name"] in {x["id"] for x in domains}]
                    if matches: row["matched_pack_ids"] = matches
                    traits = analysis["content_traits"]; preferred = profile.get("preferred_content_traits", {})
                    weights = {key: 1 + float(preferred.get(key, 0)) for key in traits}
                    score = 100 * sum(value * weights[key] for key, value in traits.items()) / max(.001, sum(weights.values()))
                    row.update(ai_potential_score=round(score, 2), opportunity_score=round(score, 2), novelty=traits["novelty"],
                               localization_value=traits["localization_value"],
                               story_strength=traits["story_strength"], visual_payoff=traits["visual_payoff"],
                               knowledge_value=traits["knowledge_value"], result_payoff=traits["result_payoff"],
                               llm_status="scored",
                               selection_reason="；".join(analysis["positive_signals"]) or "结构化内容分析")
                    scored += 1
                except Exception as exc:
                    warnings.append(f"本地 AI 分析不可用：{type(exc).__name__}；其余候选使用公开元数据特征")
                    break
        ranker = CandidateRanker(config)
        for row in preselected:
            row["pack_id"] = primary_domain(row)
            row["discovery_pack_id"] = row["pack_id"]
        selected = ranker.rank_by_domain(
            preselected,
            per_pack,
            ranking_mode,
            backfill_channel_limit=int(config.get("discovery_backfill_max_per_channel", 4)),
            domain_order=[d["id"] for d in domains],
        )
        for row in selected:
            row["discovery_pack_id"] = row["pack_id"]; row["collision_status"] = "曾召回，尚未下载" if row["seen_in_previous_search"] else "未下载候选"
            row["selection_tier"] = "preferred"
            attrs = resources[row["video_id"]]["attributions"]
            row["attribution_run_id"] = max(attrs, key=lambda a: next((r.get("performance_prior", 0) for r in strategy["query_plan"] if r.get("query") == a["query"]), 0))["run_id"]
        eligible = {row["video_id"] for row in rows}; selected_ids_set = {row["video_id"] for row in preselected}
        high_quality = {row["video_id"] for row in preselected if row["llm_status"] == "scored" and row["ai_potential_score"] >= 65}
        shown_by_run = {row["attribution_run_id"]: row["video_id"] for row in selected}
        diagnostics = []
        for record, hits in records:
            if record["status"] == "recalled": record["status"] = "complete"
            hit_set = set(hits); shown = {video for run, video in shown_by_run.items() if run == record["run_id"]}
            resource_hits = {video for video in hit_set if video in resources}
            record.update(eligible_count=len(hit_set & eligible), ai_selected_count=len(hit_set & selected_ids_set),
                          ai_accept_count=len(hit_set & high_quality), ai_high_quality_count=len(hit_set & high_quality), shown_count=len(shown))
            record["novel_candidate_count"] = len(resource_hits - seen - set(known_video_ids or ()))
            record["unique_channel_count"] = len({
                resources[video]["item"].get("snippet", {}).get("channelId", "")
                for video in resource_hits
                if resources[video]["item"].get("snippet", {}).get("channelId", "")
            })
            self.performance.save(record, hits, shown); diagnostics.append(dict(record))
        by_domain = {d["id"]: [] for d in domains}
        for row in selected: by_domain.setdefault(row["pack_id"], []).append(row)
        result_counts_by_pack = {key: len(value) for key, value in by_domain.items()}
        result_shortfalls_by_pack = {d["id"]: max(0, per_pack - len(by_domain.get(d["id"], []))) for d in domains}
        incomplete_labels = [d["label"] for d in domains if result_shortfalls_by_pack.get(d["id"], 0)]
        if incomplete_labels:
            warnings.append(
                "以下领域在安全、语言、时长、质量与去重后仍未达到每领域目标候选数："
                + "、".join(incomplete_labels)
                + "；可扩大时间范围、调整领域实体/内容形式，或降低每领域目标候选数"
            )
        recall_sources = Counter(source for row in rows for source in row["matched_recall_sources"])
        final_domains = Counter(row["pack_id"] for row in selected); final_channels = Counter(row["channel_id"] or row["channel_title"] for row in selected)
        final_intents = Counter(intent for row in selected for intent in row["matched_intents"])
        notify("Discovery Next：保存策略与全链路指标", 100)
        return {"generated_at": now.isoformat(), "hours": hours, "per_pack": per_pack, "results": selected,
                "groups": [{"id": d["id"], "label": d["label"], "description": d["description"], "results": by_domain.get(d["id"], [])} for d in domains if by_domain.get(d["id"]) or d["id"] in set(selected_ids or [])],
                "summary": {"selection_policy_version": 8, "schema_version": 2, "recall_architecture": "discovery_next", "architecture_phase": 2, "ranking_mode": ranking_mode,
                    "discovery_scope": discovery_scope, "search_strength": search_strength, "sample_size": profile["sample_size"],
                    "domain_allocation": strategy["domain_allocation"], "domain_budget": strategy["domain_budget"], "exploration_ratio": strategy["exploration_ratio"],
                    "domain_allocation_reasons": strategy.get("allocation_reasons", {}),
                    "recall_budget": strategy.get("recall_budget", {}), "recall_budget_weights": strategy.get("recall_budget_weights", {}),
                    "query_plan": strategy["query_plan"], "query_diagnostics": diagnostics, "recall_source_distribution": dict(recall_sources),
                    "candidate_flow": {"raw": sum(r.get("returned_count", 0) for r, _ in records), "deduplicated": len(resources), "hard_filtered": len(rows),
                                       "preselected": len(preselected), "ai_analyzed": scored, "final_shown": len(selected)},
                    "ai_budget_distribution": bucket_counts, "final_distribution": {"domains": dict(final_domains), "channels": dict(final_channels),
                        "intents": dict(final_intents), "semantic_clusters": len({row["semantic_cluster"] for row in selected})},
                    "warnings": warnings, "search_request_count": sum(int(record.get("calls", 1)) for record, _ in records),
                    "search_request_limit": strategy["search_budget"],
                    "raw_candidate_count": len(resources), "eligible_count": len(rows), "llm_scored_count": scored,
                    "llm_candidate_count": len(preselected), "result_count": len(selected), "unique_result_count": len(selected),
                    "selected_pack_count": len(by_domain), "minimum_duration_seconds": minimum, "maximum_duration_seconds": maximum,
                    "result_target_per_pack": per_pack, "result_limit_per_pack": per_pack,
                    "result_shortfalls_by_pack": result_shortfalls_by_pack,
                    "excluded": dict(excluded), "result_counts_by_pack": {key: len(value) for key, value in by_domain.items()},
                    "recalled_counts_by_pack": dict(domain_counts)}}
