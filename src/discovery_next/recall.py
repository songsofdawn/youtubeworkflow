"""Execute planned recall sources with adaptive page 2/3 and full provenance."""
from __future__ import annotations

from datetime import timedelta

from src.fetch_daily_candidates import get_video_details


class RecallExecutor:
    def __init__(self, youtube, performance, config):
        self.youtube, self.performance, self.config = youtube, performance, config

    def _search_page(self, query, *, page_token, now, hours):
        params = {"part": "snippet", "q": query.get("query", ""), "type": "video", "order": query.get("order", "relevance"),
                  "maxResults": 50, "publishedAfter": (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                  "regionCode": self.config.get("region_code", "US"), "relevanceLanguage": self.config.get("language", "en"),
                  "safeSearch": self.config.get("safe_search", "moderate")}
        if query.get("channel_id"):
            params["channelId"] = query["channel_id"]
            params.pop("q")
        if page_token:
            params["pageToken"] = page_token
        response = self.youtube.get("search", params)
        items = list(response.get("items", []))
        ids = list(dict.fromkeys(str(item.get("id", {}).get("videoId", "")) for item in items if item.get("id", {}).get("videoId")))
        return items, ids, response.get("nextPageToken")

    @staticmethod
    def _should_continue(state, page_number, remaining_budget, thresholds):
        if remaining_budget <= 0 or not state.get("page_token"):
            return False
        if page_number == 2:
            minimum_new = thresholds.get("page2_min_new_unique", 8)
            maximum_duplicate = thresholds.get("page2_max_duplicate_rate", 0.7)
        else:
            minimum_new = thresholds.get("page3_min_new_unique", 6)
            maximum_duplicate = thresholds.get("page3_max_duplicate_rate", 0.7)
        returned = max(1, int(state.get("last_returned", 0)))
        new_unique = max(0, int(state.get("last_new_unique", 0)))
        duplicate_rate = 1 - new_unique / returned
        return new_unique >= minimum_new and duplicate_rate <= maximum_duplicate

    def execute(self, plan, *, hours, now, seen, notify=None, recall_budget=None, search_budget=None):
        resources, records, warnings = {}, [], []
        recall_budget = recall_budget or {}
        config_next = self.config.get("discovery_next", {})
        pagination = config_next.get("adaptive_pagination", {})
        max_pages = 1 if not pagination.get("enabled", False) else min(3, max(1, int(self.config.get("discovery_max_pages_per_stream", 3))))
        thresholds = {key: pagination.get(key, default) for key, default in {
            "page2_min_new_unique": 8,
            "page2_max_duplicate_rate": 0.7,
            "page3_min_new_unique": 6,
            "page3_max_duplicate_rate": 0.7,
        }.items()}
        search_budget = max(1, int(search_budget or len(plan)))
        used_requests = 0
        run_seen = set(seen or ())
        states = []

        def new_state(query, record):
            record["recall_budget_allocated"] = int(recall_budget.get(query.get("recall_source", "exploitation"), 0))
            return {"query": query, "record": record, "page_token": None, "pages": 0,
                    "total_returned": 0, "new_unique_ids": set(), "all_ids": set(), "hits": [],
                    "last_returned": 0, "last_new_unique": 0, "status": "running"}

        def record_page(state, items, ids, page_token):
            page_unique = set(ids)
            state["pages"] += 1
            state["total_returned"] += len(items)
            state["all_ids"].update(page_unique)
            state["page_token"] = page_token
            state["last_returned"] = len(items)
            new_ids = page_unique - run_seen
            state["new_unique_ids"].update(new_ids)
            state["last_new_unique"] = len(new_ids)
            state["hits"] = list(state["all_ids"])
            run_seen.update(page_unique)

        # Every query gets page 1 before any query may spend extra page budget.
        aborted = False
        for index, query in enumerate(plan):
            if used_requests >= search_budget:
                break
            if notify:
                notify(f"Discovery Next：多路召回 {index + 1}/{len(plan)}", 12 + int(25 * index / max(1, len(plan))))
            record = self.performance.start(query, hours)
            state = new_state(query, record)
            states.append(state)
            try:
                items, ids, page_token = self._search_page(query, page_token=None, now=now, hours=hours)
                used_requests += 1
                record_page(state, items, ids, page_token)
            except Exception as exc:
                record["status"], record["error"] = "failed", type(exc).__name__
                warnings.append(f"搜索未完成：{type(exc).__name__}；已保留执行记录")
                self.performance.save(record, state["hits"])
                aborted = True
                break

        if not aborted:
            for state in states:
                if used_requests >= search_budget:
                    break
                while state["pages"] < max_pages and state["page_token"] and used_requests < search_budget:
                    page_number = state["pages"] + 1
                    if not self._should_continue(state, page_number, search_budget - used_requests, thresholds):
                        break
                    try:
                        items, ids, page_token = self._search_page(
                            state["query"], page_token=state["page_token"], now=now, hours=hours,
                        )
                        used_requests += 1
                        record_page(state, items, ids, page_token)
                    except Exception as exc:
                        record = state["record"]
                        record["status"], record["error"] = "failed", type(exc).__name__
                        warnings.append(f"搜索未完成：{type(exc).__name__}；已保留执行记录")
                        aborted = True
                        break
                if aborted:
                    break

        for state in states:
            record = state["record"]
            record["returned_count"] = state["total_returned"]
            record["new_unique_count"] = len(state["new_unique_ids"])
            record["duplicate_count"] = state["total_returned"] - record["new_unique_count"]
            record["calls"] = state["pages"]
            record["pages_fetched"] = state["pages"]
            if record["status"] != "failed":
                try:
                    details = get_video_details(self.youtube, [video for video in state["all_ids"] if video not in resources])
                except Exception as exc:
                    record["status"], record["error"] = "failed", type(exc).__name__
                    warnings.append(f"视频详情获取失败：{type(exc).__name__}；已保留搜索记录")
                    self.performance.save(record, state["hits"])
                    records.append((record, state["hits"]))
                    continue
                for video_id, value in details.items():
                    entry = resources.setdefault(video_id, {"item": value, "attributions": []})
                    entry["attributions"].append({"run_id": record["run_id"], "query": state["query"].get("query", ""),
                        "domain": state["query"].get("domain", ""), "intent": state["query"].get("intent", ""),
                        "recall_source": state["query"].get("recall_source", ""), "run_at": record.get("run_at", "")})
            if record["status"] != "failed":
                record["status"] = "recalled"
            self.performance.save(record, state["hits"])
            records.append((record, state["hits"]))
        return resources, records, warnings
