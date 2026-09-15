"""Execute planned recall sources and retain full provenance."""
from datetime import timedelta

from src.fetch_daily_candidates import get_video_details


class RecallExecutor:
    def __init__(self, youtube, performance, config):
        self.youtube, self.performance, self.config = youtube, performance, config

    def execute(self, plan, *, hours, now, seen, notify=None, recall_budget=None):
        resources, records, warnings = {}, [], []
        recall_budget = recall_budget or {}
        for index, query in enumerate(plan):
            if notify:
                notify(f"Discovery Next：多路召回 {index + 1}/{len(plan)}", 12 + int(25 * index / max(1, len(plan))))
            record = self.performance.start(query, hours)
            record["recall_budget_allocated"] = int(recall_budget.get(query.get("recall_source", "exploitation"), 0))
            hits = []
            records.append((record, hits))
            try:
                params = {"part": "snippet", "q": query.get("query", ""), "type": "video", "order": query.get("order", "relevance"),
                          "maxResults": 50, "publishedAfter": (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                          "regionCode": self.config.get("region_code", "US"), "relevanceLanguage": self.config.get("language", "en"),
                          "safeSearch": self.config.get("safe_search", "moderate"), "videoEmbeddable": "true"}
                if query.get("channel_id"):
                    params["channelId"] = query["channel_id"]
                    params.pop("q")
                response = self.youtube.get("search", params)
                hits.extend(dict.fromkeys(str(item.get("id", {}).get("videoId", "")) for item in response.get("items", []) if item.get("id", {}).get("videoId")))
                record["returned_count"] = len(response.get("items", []))
                record["new_unique_count"] = len(set(hits) - seen - resources.keys())
                record["duplicate_count"] = record["returned_count"] - record["new_unique_count"]
                self.performance.save(record, hits)
                details = get_video_details(self.youtube, [video for video in hits if video not in resources])
                for video_id, value in details.items():
                    entry = resources.setdefault(video_id, {"item": value, "attributions": []})
                    entry["attributions"].append({"run_id": record["run_id"], "query": query.get("query", ""),
                        "domain": query.get("domain", ""), "intent": query.get("intent", ""),
                        "recall_source": query.get("recall_source", ""), "run_at": record.get("run_at", "")})
                record["status"] = "recalled"
            except Exception as exc:
                record["status"], record["error"] = "failed", type(exc).__name__
                warnings.append(f"搜索未完成：{type(exc).__name__}；已保留执行记录")
                self.performance.save(record, hits)
                break
            self.performance.save(record, hits)
        return resources, records, warnings
