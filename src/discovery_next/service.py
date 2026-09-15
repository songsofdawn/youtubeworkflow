"""Content-driven discovery. Search provenance is attribution, not classification."""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.candidate_analysis import assess_language
from src.discovery.ollama_client import OllamaDiscoveryClient, OllamaSettings
from src.fetch_daily_candidates import best_thumbnail, format_duration, get_video_details, parse_iso8601_duration
from src.learning.learning_service import LearningService, enqueue_learning
from src.learning.storage import atomic_json, read_json
from src.learning.schemas import VideoAnalysis
from src.learning.video_analyzer import VideoAnalyzer, public_metadata
from .query_generator import generate_queries
from .query_performance import QueryPerformance
from .strategy_builder import build_strategy
from .taxonomy import Taxonomy


def contains(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term.casefold().replace("_", " ")) + r"(?!\w)", text.casefold()))


def classify(row, domains):
    text = row["title"] + " " + row["description"] + " " + " ".join(row["tags"])
    return [d["id"] for d in domains if any(contains(text, term) for term in [d["id"].replace("_", " "), *d["entities"]])]


class DiscoveryNextService:
    def __init__(self, root, analyzer=None):
        self.root = Path(root)
        self.performance = QueryPerformance(root)
        self.analyzer = analyzer

    def public_settings(self, config):
        settings = OllamaSettings.from_config(config).public_dict()
        return {**settings, "architecture": "discovery_next", "max_search_requests": min(100, int(config.get("discovery_max_search_requests", 24))),
                "recall_target": int(config.get("discovery_recall_target", 1000)),
                "metadata_max_candidates": int(config.get("discovery_llm", {}).get("metadata_max_candidates", 100)),
                "minimum_duration_minutes": int(config.get("discovery_min_duration_seconds", 300)) // 60,
                "maximum_duration_minutes": int(config.get("discovery_max_duration_seconds", 10800)) // 60,
                "default_ranking_mode": "hot", "feedback": {},
                "learning_sample_size": read_json(self.root / "data/learning/user_content_profile.json", {}).get("sample_size", 0)}

    def health(self, config):
        status = OllamaDiscoveryClient(OllamaSettings.from_config(config)).health()
        return {**self.public_settings(config), "reachable": bool(status.get("reachable")), "model_ready": bool(status.get("model_ready"))}

    def record_feedback(self, item, feedback):
        aliases = {"selected": "interested", "boring": "too_common", "irrelevant": "off_topic", "duplicate": "not_interested", "wrong_language": "off_topic", "unsafe": "not_interested"}
        service = LearningService(self.root)
        event_id = service.record(item, aliases.get(feedback, feedback), "discovery_next")
        # Re-aggregation is cheap and immediately applies feedback to already analyzed videos.
        service.rebuild()
        enqueue_learning(self.root, event_id)
        return {"video_id": item["video_id"], "feedback": feedback, "event_id": event_id}

    def run(self, *, youtube, packs, selected_ids, hours, per_pack, config,
            known_video_ids=None, known_titles=None, minimum_duration_seconds=None,
            maximum_duration_seconds=None, ranking_mode="hot", now=None, progress=None, cancelled=None):
        if ranking_mode not in {"hot", "potential"}:
            raise ValueError("不支持的发现排序")
        def notify(message, value):
            if cancelled:
                cancelled()
            if progress:
                progress(message, value)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        domains = [d for d in packs if d["id"] in selected_ids and d.get("enabled", True)]
        profile = LearningService(self.root).rebuild()
        strategy = build_strategy(profile, domains, self.performance.history(), Taxonomy(self.root).formats)
        atomic_json(self.root / "data/learning/discovery_strategy.json", strategy)
        budget = max(1, min(100, int(config.get("discovery_max_search_requests", 24))))
        queries = generate_queries(strategy, domains, Taxonomy(self.root).formats, budget,
                                   seed=now.isoformat(), ranking_mode=ranking_mode)
        seen = self.performance.seen_ids() | set(known_video_ids or ())
        known = set(known_video_ids or ())
        known_title_set = {str(t).casefold().strip() for t in known_titles or []}
        records, resources, warnings = [], {}, []
        notify("Discovery Next：根据画像生成临时查询", 5)
        for index, query in enumerate(queries):
            notify(f'Discovery Next：执行策略 {index + 1}/{len(queries)}', 5 + int(30 * index / max(1, len(queries))))
            record = self.performance.start(query, hours)
            hits = []
            records.append((record, hits))
            try:
                params = {"part": "snippet", "q": query["query"], "type": "video", "order": query["order"],
                          "maxResults": 50, "publishedAfter": (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                          "regionCode": config.get("region_code", "US"), "relevanceLanguage": config.get("language", "en"),
                          "safeSearch": config.get("safe_search", "moderate"), "videoEmbeddable": "true"}
                if query.get("channel_id"):
                    params["channelId"] = query["channel_id"]
                    params.pop("q")
                response = youtube.get("search", params)
                hits.extend(dict.fromkeys(str(item.get("id", {}).get("videoId", "")) for item in response.get("items", []) if item.get("id", {}).get("videoId")))
                record["returned_count"] = len(response.get("items", []))
                record["new_unique_count"] = len(set(hits) - seen - resources.keys())
                # Persist search output before the separate metadata request can fail.
                self.performance.save(record, hits)
                resources.update(get_video_details(youtube, [v for v in hits if v not in resources]))
                record["status"] = "recalled"
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = type(exc).__name__
                warnings.append(f'搜索未完成：{type(exc).__name__}；已保留执行记录')
                self.performance.save(record, hits)
                break
            self.performance.save(record, hits)
        minimum = minimum_duration_seconds or int(config.get("discovery_min_duration_seconds", 300))
        maximum = maximum_duration_seconds or int(config.get("discovery_max_duration_seconds", 10800))
        excluded, rows = Counter(), []
        def threshold(name, fallback):
            return float(config.get(name + "_by_window", {}).get(str(hours), config.get(name, fallback)))
        hot_views = threshold("discovery_hot_view_count", 100000)
        hot_vph = threshold("discovery_hot_views_per_hour", 5000)
        for video_id, item in resources.items():
            snippet, content, stats, status = (item.get(k, {}) for k in ("snippet", "contentDetails", "statistics", "status"))
            duration = parse_iso8601_duration(content.get("duration", ""))
            try:
                published = datetime.fromisoformat(snippet.get("publishedAt", "").replace("Z", "+00:00"))
                age = (now - published).total_seconds() / 3600
            except (ValueError, TypeError):
                age = hours + 1
            text = str(snippet.get("title", "")) + " " + str(snippet.get("description", ""))
            reason = ""
            if video_id in known or str(snippet.get("title", "")).casefold().strip() in known_title_set:
                reason = "known_video"
            elif not minimum <= duration <= maximum:
                reason = "duration"
            elif not 0 <= age <= hours:
                reason = "outside_window"
            elif snippet.get("liveBroadcastContent", "none") != "none" or status.get("privacyStatus", "public") != "public" or status.get("embeddable", True) is False:
                reason = "unavailable"
            elif any(contains(text, term) for term in config.get("hard_exclude_phrases", [])):
                reason = "risk_phrase"
            elif config.get("exclude_shorts", True) and duration <= int(config.get("shorts_max_duration_seconds", 180)) and "#shorts" in text.casefold():
                reason = "shorts"
            elif config.get("english_only", True) and not assess_language(snippet, str(content.get("caption")) == "true", config.get("language_markers", []))["is_english"]:
                reason = "non_english"
            if reason:
                excluded[reason] += 1
                continue
            views = int(stats.get("viewCount") or 0)
            vph = views / max(age, 1)
            row = {"video_id": video_id, "title": str(snippet.get("title", video_id)), "description": str(snippet.get("description", "")),
                   "channel_title": str(snippet.get("channelTitle", "")), "channel_id": str(snippet.get("channelId", "")),
                   "tags": snippet.get("tags", []), "category": snippet.get("categoryId", ""), "published_at": snippet.get("publishedAt", ""),
                   "duration_seconds": duration, "duration": format_duration(duration), "view_count": views,
                   "like_count": int(stats.get("likeCount") or 0), "comment_count": int(stats.get("commentCount") or 0),
                   "views_per_hour": vph, "age_hours": age, "has_caption": str(content.get("caption")) == "true",
                   "thumbnail_url": best_thumbnail(snippet), "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
                   "license": status.get("license", "unknown"), "rights_status": "PENDING", "embeddable": True,
                   "hot_protected": views >= hot_views or vph >= hot_vph, "llm_status": "not_scored",
                   "source_query_run_ids": [r["run_id"] for r, hits in records if video_id in hits],
                   "opportunity_score": 50.0, "selection_reason": "公开元数据内容匹配；等待人工判断"}
            row["matched_pack_ids"] = classify(row, domains)
            rows.append(row)
        eligible = {r["video_id"] for r in rows}
        notify("Discovery Next：按内容分析和分类", 40)
        analyzer = self.analyzer or VideoAnalyzer(config, packs)
        llm_limit = max(0, min(600, int(config.get("discovery_llm", {}).get("metadata_max_candidates", 100))))
        scored = 0
        # Heat selects the review order, never overrides hard safety/language filters.
        rows.sort(key=lambda r: (r["hot_protected"], r["views_per_hour"]), reverse=True)
        for index, row in enumerate(rows[:llm_limit]):
            notify(f'Discovery Next：内容分析 {index + 1}/{min(len(rows), llm_limit)}', 40 + int(45 * index / max(1, min(len(rows), llm_limit))))
            if not config.get("discovery_llm", {}).get("enabled", False):
                break
            try:
                analysis = analyzer.analyze(public_metadata(row, "discovery_next"))
                analysis = VideoAnalysis.model_validate(analysis).model_dump()
                if analysis["video_id"] != row["video_id"]:
                    raise ValueError("候选分析 ID 不匹配")
                matches = [d["name"] for d in analysis["domains"] if d["score"] >= 0.5 and d["name"] in selected_ids]
                if matches or not row["hot_protected"]:
                    row["matched_pack_ids"] = matches
                traits = analysis["content_traits"]
                preferred = profile.get("preferred_content_traits", {})
                weights = {k: 1 + preferred.get(k, 0) for k in traits}
                row["opportunity_score"] = round(100 * sum(v * weights[k] for k, v in traits.items()) / sum(weights.values()), 2)
                row["llm_status"] = "scored"
                row["selection_reason"] = "；".join(analysis["positive_signals"]) or "结构化内容分析"
                scored += 1
            except Exception as exc:
                warnings.append(f'本地 AI 分析不可用：{type(exc).__name__}；本轮其余候选使用公开元数据分类')
                break
        for row in rows:
            text = row["title"] + " " + row["description"]
            intents = {intent for domain in domains if domain["id"] in row["matched_pack_ids"] for intent in domain["negative_intents"]}
            penalty = sum(8 for intent in intents if contains(text, intent))
            penalty += sum(min(15, 10 * weight) for term, weight in profile.get("negative_preferences", {}).items() if contains(text, term))
            if not row["hot_protected"]:
                row["opportunity_score"] = max(0, row["opportunity_score"] - penalty)
            row["seen_in_previous_search"] = row["video_id"] in seen
        selected = []
        by_domain = {d["id"]: [] for d in domains}
        channel_counts = Counter()
        sort_key = (lambda r: (r["hot_protected"], r["views_per_hour"], r["opportunity_score"])) if ranking_mode == "hot" else (lambda r: (r["hot_protected"], r["opportunity_score"], r["views_per_hour"]))
        for row in sorted(rows, key=sort_key, reverse=True):
            channel = row["channel_id"] or row["channel_title"]
            if profile.get("channel_preferences", {}).get(channel, 0) < 0:
                continue
            if channel_counts[channel] >= int(config.get("max_per_channel", 2)):
                continue
            if row["llm_status"] == "scored" and row["opportunity_score"] < 35 and not row["hot_protected"]:
                continue
            matches = [d for d in row["matched_pack_ids"] if d in by_domain and len(by_domain[d]) < per_pack]
            if not matches:
                continue
            assigned = min(matches, key=lambda d: len(by_domain[d]))
            row.update(pack_id=assigned, discovery_pack_id=assigned,
                       collision_status="曾召回，尚未下载" if row["seen_in_previous_search"] else "未下载候选", selection_tier="preferred")
            by_domain[assigned].append(row)
            selected.append(row)
            channel_counts[channel] += 1
        shown = {r["video_id"] for r in selected}
        high_quality = {r["video_id"] for r in rows if r["llm_status"] == "scored" and r["opportunity_score"] >= 65}
        diagnostics = []
        for record, hits in records:
            if record["status"] == "recalled":
                record["status"] = "complete"
            record.update(eligible_count=len(set(hits) & eligible), ai_high_quality_count=len(set(hits) & high_quality), shown_count=len(set(hits) & shown))
            self.performance.save(record, hits, shown)
            diagnostics.append({**record, "pack_id": record["domain"], "orders": [record["order"]], "calls": 1, "unique_count": len(hits), "new_eligible_count": len(set(hits) & eligible - seen), "selected_count": record["shown_count"]})
        notify("Discovery Next：绩效记录完成", 100)
        return {"generated_at": now.isoformat(), "hours": hours, "per_pack": per_pack, "results": selected,
                "groups": [{"id": d["id"], "label": d["label"], "description": d["description"], "results": by_domain[d["id"]]} for d in domains],
                "summary": {"selection_policy_version": 7, "recall_architecture": "discovery_next", "ranking_mode": ranking_mode,
                            "sample_size": profile["sample_size"], "query_diagnostics": diagnostics, "warnings": warnings,
                            "search_request_count": len(records), "search_request_limit": budget, "raw_candidate_count": len(resources),
                            "eligible_count": len(rows), "llm_scored_count": scored, "llm_candidate_count": min(llm_limit, len(rows)),
                            "result_count": len(selected), "unique_result_count": len(selected), "selected_pack_count": len(domains),
                            "minimum_duration_seconds": minimum, "maximum_duration_seconds": maximum, "excluded": dict(excluded),
                            "result_counts_by_pack": {k: len(v) for k, v in by_domain.items()},
                            "recalled_counts_by_pack": {d["id"]: sum(d["id"] in r["matched_pack_ids"] for r in rows) for d in domains}}}
