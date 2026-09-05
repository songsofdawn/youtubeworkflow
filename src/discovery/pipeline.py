from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from src.candidate_analysis import assess_language, calculate_interest
from src.fetch_daily_candidates import (
    SearchQuotaExceeded,
    best_thumbnail,
    format_duration,
    get_video_details,
    parse_iso8601_duration,
)

from .ollama_client import (
    PROMPT_VERSION,
    VISUAL_PROMPT_VERSION,
    OllamaDiscoveryClient,
    OllamaDiscoveryError,
    OllamaSettings,
)
from .store import DiscoveryStore


HISTORY_VERSION = 1
POSITIVE_FEEDBACK = {"interested", "selected"}
NEGATIVE_FEEDBACK = {"boring", "irrelevant", "duplicate", "wrong_language", "unsafe"}
ProgressCallback = Callable[[str, int], None]
CancellationCallback = Callable[[], None]


def _published_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _window_threshold(config: dict[str, Any], name: str, hours: int, default: float) -> float:
    configured = config.get(f"{name}_by_window")
    if isinstance(configured, dict):
        value = configured.get(str(hours), configured.get(hours))
        if value is not None:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                pass
    try:
        return max(0.0, float(config.get(name, default)))
    except (TypeError, ValueError):
        return max(0.0, float(default))


def _rank_percentiles(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    ordered = sorted(rows, key=lambda item: float(item.get(key) or 0), reverse=True)
    if not ordered:
        return {}
    if len(ordered) == 1:
        return {str(ordered[0]["video_id"]): 100.0}
    return {
        str(row["video_id"]): 100.0 * (len(ordered) - index - 1) / (len(ordered) - 1)
        for index, row in enumerate(ordered)
    }


def _title_tokens(value: object) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-z0-9\s]", " ", str(value or "").casefold()).split()
        if len(token) > 2
    }


def _titles_are_similar(left: object, right: object) -> bool:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(
        None,
        " ".join(sorted(left_tokens)),
        " ".join(sorted(right_tokens)),
    ).ratio()
    return jaccard >= 0.68 or sequence >= 0.82


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, float(value)))


def _cache_key(*values: object) -> str:
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _interest_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(config)
    merged.setdefault(
        "interest_phrases",
        {
            "strong": ["i tried", "i tested", "i built", "100 days", "what happens if", "before and after"],
            "medium": ["experiment", "challenge", "comparison", "explained", "unexpected", "from scratch"],
            "normal": ["results", "project", "review", "guide", "workflow"],
        },
    )
    merged.setdefault("interest_phrase_weights", {"strong": 6, "medium": 4, "normal": 2})
    merged.setdefault("topic_interest_phrases", {})
    merged.setdefault("topic_penalty_phrases", {})
    merged.setdefault(
        "boring_penalty_phrases",
        ["compilation", "slideshow", "no commentary", "relaxing music", "stock footage", "promo"],
    )
    merged.setdefault("boring_penalty_per_hit", 5)
    return merged


class DiscoveryPipeline:
    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.history_path = self.project_root / "work" / "discovery_history.json"
        self.store = DiscoveryStore(
            self.project_root / "work" / "discovery" / "discovery.sqlite3"
        )
        self._llm_settings: OllamaSettings | None = None
        self._llm_client: OllamaDiscoveryClient | None = None

    def _client(self, settings: OllamaSettings) -> OllamaDiscoveryClient:
        if self._llm_client is None or self._llm_settings != settings:
            self._llm_settings = settings
            self._llm_client = OllamaDiscoveryClient(settings)
        return self._llm_client

    def public_settings(self, config: dict[str, Any]) -> dict[str, Any]:
        settings = OllamaSettings.from_config(config)
        raw_llm = config.get("discovery_llm")
        llm_config = raw_llm if isinstance(raw_llm, dict) else {}
        return {
            **settings.public_dict(),
            "recall_target": max(50, min(int(config.get("discovery_recall_target", 1000)), 5000)),
            "max_search_requests": max(
                1,
                min(int(config.get("discovery_max_search_requests", 100)), 100),
            ),
            "metadata_max_candidates": max(
                10,
                min(int(llm_config.get("metadata_max_candidates", 100)), 600),
            ),
            "minimum_duration_minutes": max(
                1,
                min(
                    int(
                        config.get(
                            "discovery_min_duration_seconds",
                            config.get("min_duration_seconds", 300),
                        )
                    )
                    // 60,
                    180,
                ),
            ),
            "maximum_duration_minutes": max(
                1,
                int(config.get("discovery_max_duration_seconds", 10800)) // 60,
            ),
            "default_ranking_mode": str(
                config.get("discovery_default_ranking_mode", "hot")
            ),
            "feedback": self.store.feedback_summary(),
        }

    def health(self, config: dict[str, Any]) -> dict[str, Any]:
        settings = OllamaSettings.from_config(config)
        result = self._client(settings).health()
        result.update(self.public_settings(config))
        return result

    def record_feedback(self, item: dict[str, Any], feedback: str) -> dict[str, Any]:
        return self.store.record_feedback(item, feedback)

    def _load_history(self, now_utc: datetime, retention_days: int) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema_version") != HISTORY_VERSION:
            return {}
        cutoff = now_utc - timedelta(days=max(1, retention_days))
        output: dict[str, dict[str, Any]] = {}
        for raw in payload.get("videos", []):
            if not isinstance(raw, dict):
                continue
            video_id = str(raw.get("video_id") or "")
            seen_at = _published_datetime(raw.get("last_seen_at"))
            if video_id and seen_at and seen_at >= cutoff:
                output[video_id] = raw
        return output

    def _save_history(
        self,
        previous: dict[str, dict[str, Any]],
        rows: list[dict[str, Any]],
        now_utc: datetime,
        maximum_entries: int,
    ) -> None:
        saved = dict(previous)
        timestamp = now_utc.isoformat().replace("+00:00", "Z")
        updated_video_ids: set[str] = set()
        for row in rows:
            video_id = str(row.get("video_id") or "")
            if not video_id or video_id in updated_video_ids:
                continue
            updated_video_ids.add(video_id)
            existing = saved.get(video_id, {})
            saved[video_id] = {
                "video_id": video_id,
                "title": str(row.get("title") or video_id),
                "first_seen_at": str(existing.get("first_seen_at") or timestamp),
                "last_seen_at": timestamp,
                "seen_count": int(existing.get("seen_count") or 0) + 1,
            }
        ordered = sorted(
            saved.values(),
            key=lambda item: str(item.get("last_seen_at") or ""),
            reverse=True,
        )[: max(100, int(maximum_entries))]
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_name(f".{self.history_path.name}.tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": HISTORY_VERSION, "updated_at": timestamp, "videos": ordered},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.history_path)

    @staticmethod
    def _notify(callback: ProgressCallback | None, step: str, progress: int) -> None:
        if callback:
            callback(step, max(0, min(int(progress), 100)))

    @staticmethod
    def _cancel(callback: CancellationCallback | None) -> None:
        if callback:
            callback()

    def _search_once(
        self,
        youtube: Any,
        *,
        pack: dict[str, Any],
        query: str,
        order: str,
        pool_size: int,
        published_after: str,
        published_before: str,
        config: dict[str, Any],
        hits: dict[str, list[dict[str, Any]]],
        ordered_ids: list[str],
        recent_titles: dict[str, list[str]],
        lane: str = "standard",
        page_token: str | None = None,
        page_index: int = 0,
        video_duration: str | None = None,
    ) -> tuple[str | None, int, set[str]]:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": pool_size,
            "order": order,
            "publishedAfter": published_after,
            "publishedBefore": published_before,
            "regionCode": str(config.get("region_code", "US")),
            "relevanceLanguage": str(config.get("language", "en")),
            "safeSearch": str(config.get("safe_search", "moderate")),
            "videoEmbeddable": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if video_duration:
            params["videoDuration"] = video_duration
        before_count = len(ordered_ids)
        page_video_ids: set[str] = set()
        payload = youtube.get(
            "search",
            params,
        )
        for rank, item in enumerate(payload.get("items", []), 1):
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("id", {}).get("videoId") or "")
            if not video_id:
                continue
            page_video_ids.add(video_id)
            if video_id not in hits:
                hits[video_id] = []
                ordered_ids.append(video_id)
            source = {
                "pack_id": str(pack["id"]),
                "search_rank": page_index * pool_size + rank,
                "search_order": order,
                "query": query,
                "recall_lane": lane,
                "search_page": page_index + 1,
                "video_duration_filter": video_duration or "any",
            }
            if source not in hits[video_id]:
                hits[video_id].append(source)
            title = str(item.get("snippet", {}).get("title") or "").strip()
            titles = recent_titles.setdefault(str(pack["id"]), [])
            if title and title not in titles and len(titles) < 12:
                titles.append(title)
        next_page_token = str(payload.get("nextPageToken") or "").strip() or None
        return next_page_token, len(ordered_ids) - before_count, page_video_ids

    def _metadata_evaluations(
        self,
        llm: OllamaDiscoveryClient,
        rows: list[dict[str, Any]],
        settings: OllamaSettings,
        preferences: dict[str, list[dict[str, str]]],
        warnings: list[str],
        progress: ProgressCallback | None,
        cancelled: CancellationCallback | None,
    ) -> None:
        pending: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            key = _cache_key(
                PROMPT_VERSION,
                settings.model,
                row["video_id"],
                row["title"],
                row.get("description"),
                row.get("tags"),
                preferences,
            )
            cached = self.store.get_evaluation(key)
            if cached:
                row["llm_evaluation"] = cached
                row["llm_cache_hit"] = True
            else:
                pending.append((row, key))
        batch_size = settings.metadata_batch_size
        total = max(1, len(pending))
        degraded = False

        def evaluate_pairs(batch_pairs: list[tuple[dict[str, Any], str]]) -> None:
            nonlocal degraded
            if not batch_pairs:
                return
            self._cancel(cancelled)
            batch = [pair[0] for pair in batch_pairs]
            try:
                evaluated = llm.evaluate_metadata(batch, preferences)
            except OllamaDiscoveryError as exc:
                if len(batch_pairs) > 1:
                    degraded = True
                    middle = len(batch_pairs) // 2
                    evaluate_pairs(batch_pairs[:middle])
                    evaluate_pairs(batch_pairs[middle:])
                else:
                    warnings.append(
                        f"本地 AI 无法评审 {batch[0]['video_id']}：{exc}"
                    )
                return
            missing: list[tuple[dict[str, Any], str]] = []
            for row, key in batch_pairs:
                value = evaluated.get(str(row["video_id"]))
                if not value:
                    missing.append((row, key))
                    continue
                row["llm_evaluation"] = value
                row["llm_cache_hit"] = False
                self.store.put_evaluation(key, str(row["video_id"]), "metadata", value)
            if missing and len(batch_pairs) > 1:
                degraded = True
                middle = max(1, len(missing) // 2)
                evaluate_pairs(missing[:middle])
                evaluate_pairs(missing[middle:])
            elif missing:
                warnings.append(f"本地 AI 未返回候选 {missing[0][0]['video_id']} 的评分")

        for start in range(0, len(pending), batch_size):
            self._cancel(cancelled)
            batch_pairs = pending[start : start + batch_size]
            batch = [pair[0] for pair in batch_pairs]
            self._notify(
                progress,
                f"本地 AI 正在评审候选 {min(start + len(batch), total)}/{total}",
                45 + int(25 * min(start + len(batch), total) / total),
            )
            evaluate_pairs(batch_pairs)
        if degraded:
            warnings.append("本地 AI 有批量响应不完整，已自动拆小重试")

    def _embedding_vectors(
        self,
        llm: OllamaDiscoveryClient,
        settings: OllamaSettings,
        texts: list[str],
    ) -> dict[str, list[float]]:
        output: dict[str, list[float]] = {}
        pending_texts: list[str] = []
        pending_keys: list[str] = []
        for text in dict.fromkeys(texts):
            key = _cache_key("embedding_v1", settings.embedding_model, text)
            cached = self.store.get_embedding(key)
            if cached:
                output[text] = cached
            else:
                pending_texts.append(text)
                pending_keys.append(key)
        for start in range(0, len(pending_texts), 64):
            batch = pending_texts[start : start + 64]
            vectors = llm.embed(batch)
            for text, key, vector in zip(
                batch,
                pending_keys[start : start + 64],
                vectors,
            ):
                output[text] = vector
                self.store.put_embedding(key, settings.embedding_model, vector)
        return output

    @staticmethod
    def _embedding_text(row: dict[str, Any]) -> str:
        return " | ".join(
            value
            for value in (
                str(row.get("title") or "").strip(),
                str(row.get("channel_title") or "").strip(),
                str(row.get("description") or "").strip()[:800],
            )
            if value
        )

    def run(
        self,
        *,
        youtube: Any,
        packs: list[dict[str, Any]],
        selected_ids: list[str],
        hours: int,
        per_pack: int,
        config: dict[str, Any],
        known_video_ids: set[str] | None = None,
        known_titles: list[str] | None = None,
        minimum_duration_seconds: int | None = None,
        maximum_duration_seconds: int | None = None,
        ranking_mode: str = "hot",
        now: datetime | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancellationCallback | None = None,
    ) -> dict[str, Any]:
        self._notify(progress, "正在准备智能发现", 2)
        self._cancel(cancelled)
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        pack_by_id = {str(pack["id"]): pack for pack in packs}
        selected_packs = [pack_by_id[pack_id] for pack_id in selected_ids]
        ranking_mode = str(ranking_mode or "hot").strip().casefold()
        if ranking_mode not in {"hot", "potential"}:
            raise ValueError("智能发现排序模式只支持 hot 或 potential")
        settings = OllamaSettings.from_config(config)
        llm = self._client(settings)
        warnings: list[str] = []
        preferences = self.store.preference_examples()
        llm_health = llm.health()
        llm_ready = bool(settings.enabled and llm_health.get("model_ready"))
        if settings.enabled and not llm_ready:
            warnings.append("本地 Ollama 或发现模型不可用，已回退到规则评分")

        history = self._load_history(
            now_utc,
            int(config.get("discovery_history_retention_days", 90)),
        )
        configured_minimum_duration = int(
            config.get(
                "discovery_min_duration_seconds",
                config.get("min_duration_seconds", 60),
            )
        )
        minimum_duration = int(
            minimum_duration_seconds
            if minimum_duration_seconds is not None
            else configured_minimum_duration
        )
        if not 60 <= minimum_duration <= 10800:
            raise ValueError("智能发现候选最小时长必须在 1 到 180 分钟之间")
        configured_maximum_duration = int(
            config.get("discovery_max_duration_seconds", 10800)
        )
        maximum_duration = int(
            maximum_duration_seconds
            if maximum_duration_seconds is not None
            else configured_maximum_duration
        )
        if not 60 <= maximum_duration <= configured_maximum_duration:
            raise ValueError("智能发现候选最大时长超出配置允许范围")
        if minimum_duration > maximum_duration:
            raise ValueError("智能发现候选最小时长不能大于候选最大时长")
        # V4: one broad search stream; exact duration is filtered after videos.list.
        duration_filters: list[str | None] = [None]
        published_after = (now_utc - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        published_before = now_utc.isoformat().replace("+00:00", "Z")
        pool_size = min(
            50,
            max(
                int(config.get("discovery_search_results_per_pack", 50)),
                per_pack * 5,
                20,
            ),
        )
        raw_orders = config.get(
            "discovery_recall_orders",
            ["viewCount", "date", "relevance"],
        )
        recall_orders = [
            str(value)
            for value in raw_orders
            if str(value) in {"viewCount", "date", "relevance"}
        ] if isinstance(raw_orders, list) else ["viewCount", "date", "relevance"]
        recall_orders = list(dict.fromkeys(recall_orders)) or [
            "viewCount",
            "date",
            "relevance",
        ]
        requested_recall_target = max(
            1,
            min(int(config.get("discovery_recall_target", 1000)), 5000),
        )
        per_pack_recall_target = max(
            per_pack * int(config.get("discovery_recall_candidates_per_result", 10)),
            math.ceil(requested_recall_target / max(1, len(selected_packs))),
        )
        recall_target = per_pack_recall_target * len(selected_packs)
        max_requests = max(
            len(selected_packs),
            min(int(config.get("discovery_max_search_requests", 100)), 100),
        )
        max_pages_per_stream = max(
            1,
            min(int(config.get("discovery_max_pages_per_stream", 3)), 10),
        )
        hits: dict[str, list[dict[str, Any]]] = {}
        ordered_ids: list[str] = []
        recent_titles: dict[str, list[str]] = {}
        recalled_by_pack: dict[str, set[str]] = {
            str(pack["id"]): set() for pack in selected_packs
        }
        request_count = 0
        search_requests_by_pack: Counter[str] = Counter()
        zero_result_searches_by_pack: Counter[str] = Counter()
        quota_exhausted = False
        initial_streams: dict[str, dict[str, Any]] = {}

        self._notify(
            progress,
            f"正在深度召回候选，每领域目标 {per_pack_recall_target} 条",
            6,
        )
        max_supplemental_queries = max(
            0,
            min(
                int(config.get("discovery_max_supplemental_queries", 3)),
                5,
            ),
        )
        pack_query_plan: dict[str, dict[str, Any]] = {}
        hot_lane_queries: dict[str, str] = {}
        for pack in selected_packs:
            pack_id = str(pack["id"])
            query_parts = [
                value.strip()
                for value in str(pack.get("query") or "").split("|")
                if value.strip()
            ]
            if not query_parts:
                raise ValueError(f"领域 {pack_id} 缺少主搜索词")
            primary_query = query_parts[0]
            supplemental_queries = query_parts[1 : 1 + max_supplemental_queries]
            pack_query_plan[pack_id] = {
                "primary": primary_query,
                "supplemental": supplemental_queries,
            }
            hot_lane_queries[pack_id] = primary_query

        adaptive_page2_tokens: dict[tuple[str, str], str | None] = {}
        for hot_order in ("viewCount", "date"):
            for pack in selected_packs:
                if request_count >= max_requests:
                    break
                self._cancel(cancelled)
                pack_id = str(pack["id"])
                hot_query = hot_lane_queries.get(pack_id, "")
                if not hot_query:
                    continue
                request_count += 1
                search_requests_by_pack[pack_id] += 1
                try:
                    next_page_token, _, page_video_ids = self._search_once(
                        youtube,
                        pack=pack,
                        query=hot_query,
                        order=hot_order,
                        pool_size=pool_size,
                        published_after=published_after,
                        published_before=published_before,
                        config=config,
                        hits=hits,
                        ordered_ids=ordered_ids,
                        recent_titles=recent_titles,
                        lane="hot",
                        video_duration=duration_filters[0],
                    )
                except SearchQuotaExceeded:
                    quota_exhausted = True
                    if not ordered_ids:
                        raise
                    warnings.append(
                        "YouTube API 配额在热门召回通道中耗尽，已使用当前召回结果继续筛选"
                    )
                    break
                adaptive_page2_tokens[(pack_id, hot_order)] = next_page_token
                if not page_video_ids:
                    zero_result_searches_by_pack[pack_id] += 1
                recalled_by_pack[pack_id].update(page_video_ids)
            if quota_exhausted or request_count >= max_requests:
                break

        for index, pack in enumerate(selected_packs):
            if quota_exhausted or request_count >= max_requests:
                break
            self._cancel(cancelled)
            pack_id = str(pack["id"])
            primary_query = str(pack_query_plan[pack_id]["primary"])
            request_count += 1
            search_requests_by_pack[pack_id] += 1
            try:
                next_page_token, _, page_video_ids = self._search_once(
                    youtube,
                    pack=pack,
                    query=primary_query,
                    order="relevance",
                    pool_size=pool_size,
                    published_after=published_after,
                    published_before=published_before,
                    config=config,
                    hits=hits,
                    ordered_ids=ordered_ids,
                    recent_titles=recent_titles,
                    video_duration=duration_filters[0],
                )
            except SearchQuotaExceeded:
                if not ordered_ids:
                    raise
                quota_exhausted = True
                warnings.append("YouTube 今日搜索调用配额已用尽，继续处理已经召回的候选")
                break
            if not page_video_ids:
                zero_result_searches_by_pack[pack_id] += 1
            recalled_by_pack[pack_id].update(page_video_ids)
            initial_streams[pack_id] = {
                "query": primary_query,
                "order": "relevance",
                "video_duration": duration_filters[0],
                "page_token": next_page_token,
                "page_index": 1,
                "pages_fetched": 1,
                "exhausted": not next_page_token or max_pages_per_stream <= 1,
            }
            self._notify(
                progress,
                f"初步召回领域 {index + 1}/{len(selected_packs)} · 已找到 {len(ordered_ids)} 条",
                6 + int(6 * (index + 1) / max(1, len(selected_packs))),
            )

        # V4: deterministic recall; LLM no longer plans search phrases.
        planned_queries: dict[str, list[str] | str] = {}
        if False and llm_ready and settings.query_planning_enabled and not quota_exhausted:
            self._notify(progress, "本地 AI 正在规划补充搜索词", 13)
            try:
                planned_queries = llm.plan_queries(selected_packs, recent_titles, preferences)
            except OllamaDiscoveryError as exc:
                warnings.append(str(exc))

        streams_by_pack: dict[str, list[dict[str, Any]]] = {}
        stream_cursors: Counter[str] = Counter()
        for pack in selected_packs:
            pack_id = str(pack["id"])
            streams: list[dict[str, Any]] = []
            seen_streams: set[tuple[str, str, str]] = set()

            def add_stream(
                query: str,
                order: str,
                video_duration: str | None,
                initial: dict[str, Any] | None = None,
            ) -> None:
                normalized_query = " ".join(str(query).split())
                key = (normalized_query.casefold(), order, video_duration or "any")
                if not normalized_query or key in seen_streams:
                    return
                seen_streams.add(key)
                streams.append(
                    dict(initial)
                    if initial is not None
                    else {
                        "query": normalized_query,
                        "order": order,
                        "video_duration": video_duration,
                        "page_token": None,
                        "page_index": 0,
                        "pages_fetched": 0,
                        "exhausted": False,
                    }
                )

            query_plan = pack_query_plan[pack_id]
            supplemental_queries = [
                str(value)
                for value in query_plan.get("supplemental", [])
                if str(value).strip()
            ]
            supplemental_orders = config.get(
                "discovery_supplemental_search_orders",
                ["viewCount"],
            )
            supplemental_order = (
                str(supplemental_orders[0])
                if isinstance(supplemental_orders, list) and supplemental_orders
                else "viewCount"
            )
            for query in supplemental_queries:
                add_stream(query, supplemental_order, None)
            streams_by_pack[pack_id] = streams

        while (
            not quota_exhausted
            and request_count < max_requests
            and any(
                len(recalled_by_pack[pack_id]) < per_pack_recall_target
                for pack_id in recalled_by_pack
            )
        ):
            made_request = False
            for pack in selected_packs:
                if request_count >= max_requests:
                    break
                pack_id = str(pack["id"])
                if len(recalled_by_pack[pack_id]) >= per_pack_recall_target:
                    continue
                streams = streams_by_pack.get(pack_id, [])
                if not streams:
                    continue
                selected_stream: dict[str, Any] | None = None
                for _ in range(len(streams)):
                    index = stream_cursors[pack_id] % len(streams)
                    stream_cursors[pack_id] += 1
                    candidate_stream = streams[index]
                    if not candidate_stream["exhausted"]:
                        selected_stream = candidate_stream
                        break
                if selected_stream is None:
                    continue
                self._cancel(cancelled)
                request_count += 1
                search_requests_by_pack[pack_id] += 1
                try:
                    next_page_token, _, page_video_ids = self._search_once(
                        youtube,
                        pack=pack,
                        query=str(selected_stream["query"]),
                        order=str(selected_stream["order"]),
                        pool_size=pool_size,
                        published_after=published_after,
                        published_before=published_before,
                        config=config,
                        hits=hits,
                        ordered_ids=ordered_ids,
                        recent_titles=recent_titles,
                        page_token=(
                            str(selected_stream["page_token"])
                            if selected_stream["page_token"]
                            else None
                        ),
                        page_index=int(selected_stream["page_index"]),
                        video_duration=(
                            str(selected_stream["video_duration"])
                            if selected_stream["video_duration"]
                            else None
                        ),
                    )
                except SearchQuotaExceeded:
                    if not ordered_ids:
                        raise
                    quota_exhausted = True
                    warnings.append("YouTube 今日搜索调用配额已用尽，继续处理已经召回的候选")
                    break
                if not page_video_ids:
                    zero_result_searches_by_pack[pack_id] += 1
                recalled_by_pack[pack_id].update(page_video_ids)
                selected_stream["pages_fetched"] = int(selected_stream["pages_fetched"]) + 1
                selected_stream["page_index"] = int(selected_stream["page_index"]) + 1
                selected_stream["page_token"] = next_page_token
                selected_stream["exhausted"] = (
                    not next_page_token
                    or int(selected_stream["pages_fetched"]) >= max_pages_per_stream
                )
                made_request = True
                covered_recall = sum(
                    min(len(video_ids), per_pack_recall_target)
                    for video_ids in recalled_by_pack.values()
                )
                recall_progress = max(
                    covered_recall / max(1, recall_target),
                    request_count / max(1, max_requests),
                )
                self._notify(
                    progress,
                    f"深度召回 {covered_recall}/{recall_target} 条 · 搜索调用 {request_count}/{max_requests}",
                    14 + int(12 * min(recall_progress, 1.0)),
                )
            if not made_request:
                break

        recall_shortfalls = {
            pack_id: max(0, per_pack_recall_target - len(video_ids))
            for pack_id, video_ids in recalled_by_pack.items()
        }
        if any(recall_shortfalls.values()) and not quota_exhausted:
            warnings.append(
                "部分领域未达到独立召回目标；可能已达到搜索调用预算或 YouTube 可用结果不足"
            )

        self._cancel(cancelled)
        self._notify(progress, f"正在读取 {len(ordered_ids)} 个视频的详细信息", 28)
        resources = get_video_details(youtube, ordered_ids)

        # V4.1 adaptive page-2 recall.
        adaptive_enabled = bool(
            config.get("discovery_adaptive_page2_enabled", True)
        )
        adaptive_min_unique = max(
            1,
            int(config.get("discovery_adaptive_min_unique_candidates", 80)),
        )
        adaptive_min_qualified = max(
            1,
            int(config.get("discovery_adaptive_min_qualified_candidates", 30)),
        )
        raw_adaptive_orders = config.get(
            "discovery_adaptive_page2_orders",
            ["viewCount", "date"],
        )
        adaptive_orders = [
            str(value)
            for value in raw_adaptive_orders
            if str(value) in {"viewCount", "date"}
        ] if isinstance(raw_adaptive_orders, list) else ["viewCount", "date"]
        adaptive_orders = list(dict.fromkeys(adaptive_orders)) or [
            "viewCount",
            "date",
        ]
        adaptive_max_extra = max(
            0,
            min(
                int(
                    config.get(
                        "discovery_adaptive_max_extra_calls_per_pack",
                        2,
                    )
                ),
                len(adaptive_orders),
            ),
        )
        adaptive_extra_calls_by_pack: Counter[str] = Counter()
        adaptive_triggered_packs: list[str] = []
        adaptive_before: dict[str, dict[str, int]] = {}
        adaptive_after: dict[str, dict[str, int]] = {}

        adaptive_known = {
            str(value)
            for value in known_video_ids or set()
        }
        adaptive_hard_excludes = [
            str(value).casefold()
            for value in config.get("hard_exclude_phrases", [])
        ]
        adaptive_strict_english = bool(config.get("english_only", True))
        adaptive_min_views = _window_threshold(
            config,
            "discovery_min_view_count",
            hours,
            300,
        )
        adaptive_min_vph = _window_threshold(
            config,
            "discovery_min_views_per_hour",
            hours,
            20,
        )
        adaptive_expansion_views = max(
            1.0,
            adaptive_min_views
            * float(
                config.get(
                    "discovery_popularity_expansion_view_ratio",
                    0.4,
                )
            ),
        )
        adaptive_expansion_vph = max(
            0.1,
            adaptive_min_vph
            * float(
                config.get(
                    "discovery_popularity_expansion_vph_ratio",
                    0.5,
                )
            ),
        )
        adaptive_reserve_views = max(
            1.0,
            adaptive_min_views
            * float(
                config.get(
                    "discovery_popularity_reserve_view_ratio",
                    0.1,
                )
            ),
        )
        adaptive_reserve_vph = max(
            0.1,
            adaptive_min_vph
            * float(
                config.get(
                    "discovery_popularity_reserve_vph_ratio",
                    0.15,
                )
            ),
        )
        adaptive_popularity_mode = str(
            config.get("discovery_popularity_filter_mode") or "hard"
        ).casefold()

        def adaptive_candidate_qualified(video_id: str) -> bool:
            if video_id in adaptive_known:
                return False
            item = resources.get(video_id)
            if not item:
                return False
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            statistics = item.get("statistics", {})
            status = item.get("status", {})
            published = _published_datetime(snippet.get("publishedAt"))
            if published is None:
                return False
            age_hours = max(
                0.0,
                (now_utc - published).total_seconds() / 3600,
            )
            if age_hours > hours + 0.05:
                return False
            if (
                snippet.get("liveBroadcastContent", "none") != "none"
                or str(status.get("privacyStatus") or "public") != "public"
                or not bool(status.get("embeddable", True))
            ):
                return False
            duration_seconds = parse_iso8601_duration(
                str(content.get("duration", ""))
            )
            if not minimum_duration <= duration_seconds <= maximum_duration:
                return False
            title = str(snippet.get("title") or video_id)
            description = str(snippet.get("description") or "")
            searchable = f"{title} {description}".casefold()
            is_short = (
                duration_seconds
                <= int(config.get("shorts_max_duration_seconds", 180))
                and (
                    "#shorts" in searchable
                    or " shorts " in f" {searchable} "
                )
            )
            if bool(config.get("exclude_shorts", True)) and is_short:
                return False
            if any(
                phrase in searchable
                for phrase in adaptive_hard_excludes
            ):
                return False
            has_caption = str(
                content.get("caption", "")
            ).casefold() == "true"
            if adaptive_strict_english:
                language = assess_language(
                    snippet,
                    has_caption,
                    [
                        str(value)
                        for value in config.get("language_markers", [])
                    ],
                )
                if not language["is_english"]:
                    return False
            view_count = int(statistics.get("viewCount") or 0)
            views_per_hour = view_count / max(age_hours, 1.0)
            strict_pass = (
                view_count >= adaptive_min_views
                and views_per_hour >= adaptive_min_vph
            )
            expansion_pass = (
                view_count >= adaptive_expansion_views
                and views_per_hour >= adaptive_expansion_vph
            )
            reserve_pass = (
                view_count >= adaptive_reserve_views
                and views_per_hour >= adaptive_reserve_vph
            )
            if adaptive_popularity_mode == "hard":
                return strict_pass
            if adaptive_popularity_mode == "balanced":
                return strict_pass or expansion_pass or reserve_pass
            return True

        def adaptive_pack_counts(pack_id: str) -> dict[str, int]:
            ids = recalled_by_pack.get(pack_id, set())
            return {
                "unique": len(ids),
                "qualified": sum(
                    adaptive_candidate_qualified(video_id)
                    for video_id in ids
                ),
            }

        if adaptive_enabled and not quota_exhausted:
            self._notify(
                progress,
                "正在检查是否需要自适应补充第二页",
                29,
            )
            for pack in selected_packs:
                if request_count >= max_requests:
                    break
                pack_id = str(pack["id"])
                before_counts = adaptive_pack_counts(pack_id)
                adaptive_before[pack_id] = dict(before_counts)

                needs_more = (
                    before_counts["unique"] < adaptive_min_unique
                    or before_counts["qualified"] < adaptive_min_qualified
                )
                if not needs_more:
                    adaptive_after[pack_id] = dict(before_counts)
                    continue

                adaptive_triggered_packs.append(pack_id)
                primary_query = str(
                    pack_query_plan[pack_id]["primary"]
                )

                for order in adaptive_orders[:adaptive_max_extra]:
                    if request_count >= max_requests:
                        break
                    page_token = adaptive_page2_tokens.get(
                        (pack_id, order)
                    )
                    if not page_token:
                        continue

                    self._cancel(cancelled)
                    request_count += 1
                    search_requests_by_pack[pack_id] += 1
                    adaptive_extra_calls_by_pack[pack_id] += 1
                    try:
                        _, _, page_video_ids = self._search_once(
                            youtube,
                            pack=pack,
                            query=primary_query,
                            order=order,
                            pool_size=pool_size,
                            published_after=published_after,
                            published_before=published_before,
                            config=config,
                            hits=hits,
                            ordered_ids=ordered_ids,
                            recent_titles=recent_titles,
                            page_token=page_token,
                            page_index=1,
                            lane="adaptive_page2",
                            video_duration=None,
                        )
                    except SearchQuotaExceeded:
                        quota_exhausted = True
                        warnings.append(
                            "YouTube 今日搜索调用配额在自适应补页时用尽，"
                            "继续处理已召回候选"
                        )
                        break

                    recalled_by_pack[pack_id].update(page_video_ids)
                    new_ids = [
                        video_id
                        for video_id in page_video_ids
                        if video_id not in resources
                    ]
                    if new_ids:
                        resources.update(
                            get_video_details(youtube, new_ids)
                        )

                    current_counts = adaptive_pack_counts(pack_id)
                    adaptive_after[pack_id] = dict(current_counts)

                    if (
                        current_counts["unique"] >= adaptive_min_unique
                        and current_counts["qualified"]
                        >= adaptive_min_qualified
                    ):
                        break

                adaptive_after.setdefault(
                    pack_id,
                    adaptive_pack_counts(pack_id),
                )
                if quota_exhausted:
                    break

            if adaptive_triggered_packs:
                extra_total = sum(
                    adaptive_extra_calls_by_pack.values()
                )
                self._notify(
                    progress,
                    (
                        "自适应补页完成 · "
                        f"{len(adaptive_triggered_packs)} 个领域触发 · "
                        f"额外搜索 {extra_total} 次"
                    ),
                    31,
                )
        known = {str(value) for value in known_video_ids or set()}
        local_titles = [str(value) for value in known_titles or [] if str(value).strip()]
        hard_excludes = [str(value).casefold() for value in config.get("hard_exclude_phrases", [])]
        min_views = _window_threshold(config, "discovery_min_view_count", hours, 300)
        min_vph = _window_threshold(config, "discovery_min_views_per_hour", hours, 20)
        expansion_min_views = max(
            1.0,
            min_views * float(config.get("discovery_popularity_expansion_view_ratio", 0.4)),
        )
        expansion_min_vph = max(
            0.1,
            min_vph * float(config.get("discovery_popularity_expansion_vph_ratio", 0.5)),
        )
        reserve_min_views = max(
            1.0,
            min_views * float(config.get("discovery_popularity_reserve_view_ratio", 0.1)),
        )
        reserve_min_vph = max(
            0.1,
            min_vph * float(config.get("discovery_popularity_reserve_vph_ratio", 0.15)),
        )
        popularity_mode = str(config.get("discovery_popularity_filter_mode") or "hard").casefold()
        hot_min_views = int(
            _window_threshold(
                config,
                "discovery_hot_view_count",
                hours,
                100000,
            )
        )
        hot_min_vph = float(
            _window_threshold(
                config,
                "discovery_hot_views_per_hour",
                hours,
                5000.0,
            )
        )
        strict_english = bool(config.get("english_only", True))
        interest_config = _interest_config(config)
        excluded: Counter[str] = Counter()
        rows: list[dict[str, Any]] = []

        for video_id in ordered_ids:
            self._cancel(cancelled)
            if video_id in known:
                excluded["known_video"] += 1
                continue
            item = resources.get(video_id)
            if not item:
                excluded["details_missing"] += 1
                continue
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            statistics = item.get("statistics", {})
            status = item.get("status", {})
            published = _published_datetime(snippet.get("publishedAt"))
            duration_seconds = parse_iso8601_duration(str(content.get("duration", "")))
            title = str(snippet.get("title") or video_id)
            description = str(snippet.get("description") or "")
            searchable = f"{title} {description}".casefold()
            age_hours = (
                max(0.0, (now_utc - published).total_seconds() / 3600)
                if published
                else float(hours) + 1
            )
            is_short = duration_seconds <= int(config.get("shorts_max_duration_seconds", 180)) and (
                "#shorts" in searchable or " shorts " in f" {searchable} "
            )
            hard_filter_reason = ""
            if published is None or age_hours > hours + 0.05:
                hard_filter_reason = "outside_window"
            elif (
                snippet.get("liveBroadcastContent", "none") != "none"
                or str(status.get("privacyStatus") or "public") != "public"
                or not bool(status.get("embeddable", True))
            ):
                hard_filter_reason = "unavailable"
            elif not minimum_duration <= duration_seconds <= maximum_duration:
                hard_filter_reason = "duration"
            elif bool(config.get("exclude_shorts", True)) and is_short:
                hard_filter_reason = "shorts"
            elif any(phrase in searchable for phrase in hard_excludes):
                hard_filter_reason = "risk_phrase"
            if hard_filter_reason:
                excluded["hard_filter"] += 1
                excluded[hard_filter_reason] += 1
                continue
            has_caption = str(content.get("caption", "")).casefold() == "true"
            language = assess_language(
                snippet,
                has_caption,
                [str(value) for value in config.get("language_markers", [])],
            )
            if strict_english and not language["is_english"]:
                excluded["non_english"] += 1
                continue
            view_count = int(statistics.get("viewCount") or 0)
            like_count = int(statistics.get("likeCount") or 0)
            comment_count = int(statistics.get("commentCount") or 0)
            views_per_hour = view_count / max(age_hours, 1.0)
            hot_protected = (
                view_count >= hot_min_views
                or views_per_hour >= hot_min_vph
            )
            hot_protection_reason = ""
            if hot_protected:
                reasons: list[str] = []
                if view_count >= hot_min_views:
                    reasons.append(f"播放量 {view_count:,} ≥ {hot_min_views:,}")
                if views_per_hour >= hot_min_vph:
                    reasons.append(f"VPH {views_per_hour:,.0f} ≥ {hot_min_vph:,.0f}")
                hot_protection_reason = "；".join(reasons)
            heat_floor_pass = view_count >= min_views and views_per_hour >= min_vph
            expansion_heat_pass = (
                view_count >= expansion_min_views
                and views_per_hour >= expansion_min_vph
            )
            reserve_heat_pass = (
                view_count >= reserve_min_views
                and views_per_hour >= reserve_min_vph
            )
            if popularity_mode == "hard" and not heat_floor_pass and not hot_protected:
                excluded["low_popularity"] += 1
                continue
            if popularity_mode == "balanced" and not (
                heat_floor_pass
                or expansion_heat_pass
                or reserve_heat_pass
                or hot_protected
            ):
                excluded["low_popularity"] += 1
                continue

            pack_scores: list[tuple[float, int, str]] = []
            matched_ids: list[str] = []
            for hit in hits[video_id]:
                pack_id = str(hit["pack_id"])
                if pack_id not in matched_ids:
                    matched_ids.append(pack_id)
                pack = pack_by_id[pack_id]
                title_text = title.casefold()
                keyword_score = sum(
                    3.0 if keyword in title_text else 1.0 if keyword in searchable else 0.0
                    for keyword in pack["keywords"]
                )
                pack_scores.append((keyword_score, -int(hit["search_rank"]), pack_id))
            primary_pack = max(pack_scores)[2]
            keyword_score = max(pack_scores)[0]
            interest = calculate_interest(searchable, primary_pack, interest_config)
            engagement_rate = (like_count + comment_count * 3) / max(view_count, 1)
            row = {
                "video_id": video_id,
                "title": title,
                "description": description,
                "tags": [str(value) for value in (snippet.get("tags") or []) if str(value)],
                "channel_title": str(snippet.get("channelTitle") or ""),
                "published_at": published.isoformat().replace("+00:00", "Z"),
                "age_hours": round(age_hours, 2),
                "duration": format_duration(duration_seconds),
                "duration_seconds": duration_seconds,
                "view_count": view_count,
                "like_count": like_count,
                "comment_count": comment_count,
                "views_per_hour": round(views_per_hour, 2),
                "engagement_rate": round(engagement_rate, 6),
                "has_caption": has_caption,
                "license": str(status.get("license") or "unknown"),
                "embeddable": bool(status.get("embeddable", False)),
                "thumbnail_url": best_thumbnail(snippet),
                "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
                "rights_status": "PENDING",
                "pack_id": primary_pack,
                "pack_label": str(pack_by_id[primary_pack]["label"]),
                "matched_pack_ids": matched_ids,
                "search_rank": min(int(hit["search_rank"]) for hit in hits[video_id]),
                "search_source_details": list(hits[video_id]),
                "hot_lane_hit": any(
                    str(source.get("recall_lane") or "") == "hot"
                    for source in hits[video_id]
                ),
                "hot_protected": hot_protected,
                "hot_protection_reason": hot_protection_reason,
                "seen_in_previous_search": video_id in history,
                "heat_floor_pass": heat_floor_pass,
                "heat_tier": (
                    "strict"
                    if heat_floor_pass
                    else "expanded"
                    if expansion_heat_pass
                    else "reserve"
                    if reserve_heat_pass and popularity_mode == "balanced"
                    else "soft"
                ),
                "topic_relevance_score": round(_clamp(keyword_score * 12), 1),
                **{key: value for key, value in language.items() if key != "is_english"},
                **interest,
            }
            rows.append(row)

        self._notify(progress, f"规则粗筛完成，保留 {len(rows)} 条", 38)
        vph_percentiles = _rank_percentiles(rows, "views_per_hour")
        view_percentiles = _rank_percentiles(rows, "view_count")
        confidence_views = max(
            1,
            int(config.get("discovery_engagement_confidence_views", 500)),
        )
        for row in rows:
            freshness = _clamp(100.0 * (1.0 - float(row["age_hours"]) / hours))
            raw_engagement = _clamp(1000.0 * math.sqrt(float(row["engagement_rate"])))
            engagement_confidence = math.sqrt(
                min(1.0, float(row["view_count"]) / confidence_views)
            )
            engagement = raw_engagement * engagement_confidence
            velocity = _clamp(20.0 * math.log10(float(row["views_per_hour"]) + 1.0))
            hot_score = (
                vph_percentiles.get(str(row["video_id"]), 0) * 0.35
                + velocity * 0.25
                + view_percentiles.get(str(row["video_id"]), 0) * 0.15
                + freshness * 0.15
                + engagement * 0.1
            )
            row["growth_score"] = round(velocity, 1)
            row["engagement_score"] = round(engagement, 1)
            row["engagement_confidence"] = round(engagement_confidence, 4)
            row["freshness_score"] = round(freshness, 1)
            row["hot_score"] = round(hot_score, 1)
            row["editorial_prefilter_score"] = round(
                _clamp(
                    hot_score * 0.70
                    + float(row["topic_relevance_score"]) * 0.15
                    + _clamp(float(row["interest_score"]) * 5) * 0.10
                    + (100.0 if row["has_caption"] else 35.0) * 0.05
                    - min(12.0, float(row["boring_penalty"])),
                ),
                1,
            )

        metadata_rows: list[dict[str, Any]] = []
        if llm_ready and rows:
            # The wide recall can contain hundreds of videos. Give every selected
            # topic a fair shortlist before asking the slower local model, while
            # retaining the remaining rows as rule-ranked backfill candidates.
            raw_llm_config = config.get("discovery_llm")
            llm_config = raw_llm_config if isinstance(raw_llm_config, dict) else {}
            per_pack_limit = max(
                per_pack,
                min(
                    int(llm_config.get("metadata_max_candidates", 100)),
                    600,
                ),
            )
            overall_limit = per_pack_limit * len(selected_ids)
            queues = {
                pack_id: sorted(
                    (
                        row
                        for row in rows
                        if pack_id in row["matched_pack_ids"]
                    ),
                    key=lambda item: float(item["editorial_prefilter_score"]),
                    reverse=True,
                )[:per_pack_limit]
                for pack_id in selected_ids
            }
            seen_metadata_ids: set[str] = set()
            while len(metadata_rows) < overall_limit:
                added = False
                for pack_id in selected_ids:
                    queue = queues[pack_id]
                    while queue:
                        candidate = queue.pop(0)
                        video_id = str(candidate["video_id"])
                        if video_id in seen_metadata_ids:
                            continue
                        metadata_rows.append(candidate)
                        seen_metadata_ids.add(video_id)
                        added = True
                        break
                    if len(metadata_rows) >= overall_limit:
                        break
                if not added:
                    break
            self._metadata_evaluations(
                llm,
                metadata_rows,
                settings,
                preferences,
                warnings,
                progress,
                cancelled,
            )

        feedback_rows = self.store.feedback_rows(limit=500)
        feedback_by_video = {str(item["video_id"]): str(item["feedback"]) for item in feedback_rows}
        embedding_vectors: dict[str, list[float]] = {}
        if (
            llm_ready
            and settings.embedding_enabled
            and bool(llm_health.get("embedding_ready"))
            and rows
        ):
            self._notify(progress, "正在进行语义去重与偏好匹配", 72)
            texts = [self._embedding_text(row) for row in rows]
            texts.extend(str(item["title"]) for item in feedback_rows if str(item["title"]).strip())
            texts.extend(local_titles)
            try:
                embedding_vectors = self._embedding_vectors(llm, settings, texts)
            except OllamaDiscoveryError as exc:
                warnings.append(str(exc))
        elif settings.enabled and settings.embedding_enabled and not llm_health.get("embedding_ready"):
            warnings.append("未找到 embedding 模型，语义去重已回退到标题相似度")

        positive_vectors = [
            embedding_vectors[str(item["title"])]
            for item in feedback_rows
            if item["feedback"] in POSITIVE_FEEDBACK and str(item["title"]) in embedding_vectors
        ]
        negative_vectors = [
            embedding_vectors[str(item["title"])]
            for item in feedback_rows
            if item["feedback"] in NEGATIVE_FEEDBACK and str(item["title"]) in embedding_vectors
        ]
        for row in rows:
            evaluation = row.get("llm_evaluation")
            if isinstance(evaluation, dict):
                qualitative = (
                    float(evaluation["topic_fit"]) * 0.15
                    + float(evaluation["interestingness"]) * 0.2
                    + float(evaluation["novelty"]) * 0.12
                    + float(evaluation["story_payoff"]) * 0.16
                    + float(evaluation["visual_potential"]) * 0.1
                    + float(evaluation["localization_value"]) * 0.17
                    + float(evaluation["language_confidence"]) * 0.1
                    - float(evaluation["clickbait_risk"]) * 0.18
                )
                row["llm_status"] = "scored"
                row["llm_reason"] = str(evaluation["reason_zh"])
                row["llm_verdict"] = str(evaluation["verdict"])
                row["llm_confidence"] = float(evaluation["confidence"])
            else:
                qualitative = (
                    float(row["topic_relevance_score"]) * 0.35
                    + _clamp(float(row["interest_score"]) * 5) * 0.35
                    + (85.0 if row["language_confidence"] in {"high", "medium"} else 35.0) * 0.2
                    + (100.0 if row["has_caption"] else 35.0) * 0.1
                )
                row["llm_status"] = "fallback"
                row["llm_reason"] = "本地 AI 未返回有效评分，使用主题、兴趣词和热度规则排序。"
                row["llm_verdict"] = "maybe"
                row["llm_confidence"] = 0.0
            row["qualitative_score"] = round(_clamp(qualitative), 1)
            text = self._embedding_text(row)
            vector = embedding_vectors.get(text, [])
            if feedback_by_video.get(str(row["video_id"])) in POSITIVE_FEEDBACK:
                preference = 100.0
            elif feedback_by_video.get(str(row["video_id"])) in NEGATIVE_FEEDBACK:
                preference = 0.0
            elif vector and (positive_vectors or negative_vectors):
                positive = max((_cosine(vector, value) for value in positive_vectors), default=0.0)
                negative = max((_cosine(vector, value) for value in negative_vectors), default=0.0)
                preference = _clamp(50.0 + positive * 45.0 - negative * 55.0)
            else:
                preference = 50.0
            row["preference_score"] = round(preference, 1)
            if ranking_mode == "hot":
                preliminary = (
                    float(row["hot_score"]) * 0.70
                    + float(row["qualitative_score"]) * 0.20
                    + float(row["topic_relevance_score"]) * 0.10
                )
            else:
                preliminary = (
                    float(row["qualitative_score"]) * 0.52
                    + float(row["growth_score"]) * 0.15
                    + float(row["engagement_score"]) * 0.07
                    + float(row["freshness_score"]) * 0.06
                    + preference * 0.13
                    + (100.0 if row["has_caption"] else 35.0) * 0.04
                    + float(row["topic_relevance_score"]) * 0.03
                )
            preliminary -= min(20.0, float(row["boring_penalty"]))
            if row["heat_tier"] == "reserve":
                preliminary -= float(
                    config.get("discovery_reserve_popularity_penalty", 12)
                )
            elif not row["heat_floor_pass"]:
                preliminary -= float(config.get("discovery_soft_popularity_penalty", 5))
            if row["llm_verdict"] == "reject" and not row.get("hot_protected"):
                preliminary -= 12.0 if ranking_mode == "hot" else 22.0
            row["preliminary_score"] = round(_clamp(preliminary), 1)

        if llm_ready and settings.visual_enabled and settings.visual_top_n > 0 and rows:
            visual_candidates = sorted(
                rows,
                key=lambda item: float(item["preliminary_score"]),
                reverse=True,
            )[: settings.visual_top_n]
            total_visual = len(visual_candidates)
            for start in range(0, total_visual, settings.visual_batch_size):
                self._cancel(cancelled)
                batch = visual_candidates[start : start + settings.visual_batch_size]
                pending: list[tuple[dict[str, Any], str]] = []
                for row in batch:
                    key = _cache_key(
                        VISUAL_PROMPT_VERSION,
                        settings.model,
                        row["video_id"],
                        row["title"],
                        row["thumbnail_url"],
                    )
                    cached = self.store.get_evaluation(key)
                    if cached:
                        row["visual_evaluation"] = cached
                    else:
                        pending.append((row, key))
                if pending:
                    self._notify(
                        progress,
                        f"本地 AI 正在复评缩略图 {min(start + len(batch), total_visual)}/{total_visual}",
                        76 + int(12 * min(start + len(batch), total_visual) / max(1, total_visual)),
                    )
                    try:
                        values = llm.evaluate_visual([item[0] for item in pending])
                    except OllamaDiscoveryError as exc:
                        warnings.append(str(exc))
                        continue
                    for row, key in pending:
                        value = values.get(str(row["video_id"]))
                        if value:
                            row["visual_evaluation"] = value
                            self.store.put_evaluation(key, str(row["video_id"]), "visual", value)

        for row in rows:
            visual = row.get("visual_evaluation")
            if isinstance(visual, dict):
                visual_score = (
                    float(visual["visual_potential"]) * 0.5
                    + float(visual["title_thumbnail_consistency"]) * 0.35
                    + (100.0 - float(visual["thumbnail_spam_risk"])) * 0.15
                )
                row["visual_score"] = round(_clamp(visual_score), 1)
                row["visual_reason"] = str(visual.get("reason_zh") or "")
                if ranking_mode == "hot":
                    final = (
                        float(row["preliminary_score"]) * 0.94
                        + row["visual_score"] * 0.06
                    )
                else:
                    final = (
                        float(row["preliminary_score"]) * 0.88
                        + row["visual_score"] * 0.12
                    )
            else:
                row["visual_score"] = None
                row["visual_reason"] = ""
                final = float(row["preliminary_score"])
            if ranking_mode == "hot" and row.get("hot_protected"):
                final = max(final, float(row["hot_score"]))
            row["opportunity_score"] = round(_clamp(final), 1)
            row["selection_reason"] = str(row["llm_reason"])
            if row.get("hot_protected"):
                row["selection_reason"] = (
                    "热门保送（"
                    + str(row.get("hot_protection_reason") or "达到热度阈值")
                    + "）："
                    + row["selection_reason"]
                )

        self._notify(progress, "正在执行多样性重排与配额补位", 90)
        representatives: list[dict[str, Any]] = []
        semantic_threshold = float(config.get("discovery_semantic_similarity_threshold", 0.9))
        feedback_map = self.store.feedback_by_video()
        history_penalty = float(config.get("discovery_history_repeat_penalty", 8))
        for row in sorted(rows, key=lambda item: float(item["opportunity_score"]), reverse=True):
            row_vector = embedding_vectors.get(self._embedding_text(row), [])
            local_similar = next(
                (
                    title
                    for title in local_titles
                    if _titles_are_similar(row["title"], title)
                    or (
                        row_vector
                        and embedding_vectors.get(title)
                        and _cosine(row_vector, embedding_vectors[title]) >= semantic_threshold
                    )
                ),
                None,
            )
            similar: dict[str, Any] | None = None
            for representative in representatives:
                representative_vector = embedding_vectors.get(self._embedding_text(representative), [])
                if (
                    row_vector
                    and representative_vector
                    and _cosine(row_vector, representative_vector) >= semantic_threshold
                ) or _titles_are_similar(row["title"], representative["title"]):
                    similar = representative
                    break
            feedback = feedback_map.get(str(row["video_id"]), "")
            if local_similar:
                row["suppressed"] = False
                row["similar_candidate"] = True
                row["similar_to_video_id"] = "local"
                row["collision_status"] = "本地已有相似题材"
                row["opportunity_score"] = round(_clamp(row["opportunity_score"] - 30), 1)
            elif feedback == "selected":
                row["suppressed"] = True
                row["similar_candidate"] = True
                row["similar_to_video_id"] = "selected"
                row["collision_status"] = "此前已选择或下载"
                row["opportunity_score"] = round(_clamp(row["opportunity_score"] - 35), 1)
            elif feedback in NEGATIVE_FEEDBACK:
                row["suppressed"] = True
                row["similar_candidate"] = True
                row["similar_to_video_id"] = "feedback"
                row["collision_status"] = "你已将此视频标记为不合适"
                row["opportunity_score"] = round(_clamp(row["opportunity_score"] - 45), 1)
            elif similar:
                row["suppressed"] = False
                row["similar_candidate"] = True
                row["similar_to_video_id"] = str(similar["video_id"])
                row["collision_status"] = "本批次存在相似题材"
                row["opportunity_score"] = round(_clamp(row["opportunity_score"] - 15), 1)
            else:
                row["suppressed"] = False
                row["similar_candidate"] = False
                row["similar_to_video_id"] = ""
                if feedback == "interested":
                    row["collision_status"] = "你曾标记为感兴趣"
                elif row["seen_in_previous_search"]:
                    row["collision_status"] = "曾展示，已轻微降权"
                    row["opportunity_score"] = round(
                        _clamp(row["opportunity_score"] - history_penalty),
                        1,
                    )
                else:
                    row["collision_status"] = "新候选"
                representatives.append(row)
            row["semantic_group"] = (
                str(similar["video_id"]) if similar else str(row["video_id"])
            )
            row["score_breakdown"] = {
                "qualitative": row["qualitative_score"],
                "preference": row["preference_score"],
                "growth": row["growth_score"],
                "engagement": row["engagement_score"],
                "freshness": row["freshness_score"],
                "visual": row["visual_score"],
                "boring_penalty": row["boring_penalty"],
            }

        minimum_opportunity_score = _clamp(
            float(config.get("discovery_min_opportunity_score", 50))
        )
        expansion_minimum_score = _clamp(
            float(config.get("discovery_expansion_min_opportunity_score", 60))
        )
        reserve_minimum_score = _clamp(
            float(config.get("discovery_reserve_min_opportunity_score", 35))
        )
        exclude_llm_rejects = bool(config.get("discovery_exclude_llm_rejects", True))
        preferred_rows: list[dict[str, Any]] = []
        reserve_rows: list[dict[str, Any]] = []
        for row in rows:
            if row.get("suppressed"):
                excluded["feedback_suppressed"] += 1
                continue
            if row.get("similar_candidate"):
                excluded["similar_candidate"] += 1
                continue
            rejection_reason = ""
            if row.get("heat_tier") == "reserve":
                rejection_reason = "reserve_popularity"
            elif (
                exclude_llm_rejects
                and row.get("llm_status") == "scored"
                and row.get("llm_verdict") == "reject"
                and not row.get("hot_protected")
            ):
                rejection_reason = "llm_reject"
            elif (
                row.get("heat_tier") == "expanded"
                and not row.get("hot_protected")
                and (
                    row.get("llm_status") != "scored"
                    or row.get("llm_verdict") != "keep"
                )
            ):
                rejection_reason = "expansion_quality"
            required_score = (
                expansion_minimum_score
                if row.get("heat_tier") == "expanded"
                else minimum_opportunity_score
            )
            if (
                not rejection_reason
                and not row.get("hot_protected")
                and float(row["opportunity_score"]) < required_score
            ):
                rejection_reason = "low_opportunity_score"
            if not rejection_reason:
                row["selection_tier"] = "preferred"
                preferred_rows.append(row)
                continue
            if float(row["opportunity_score"]) >= reserve_minimum_score:
                row["selection_tier"] = "reserve"
                row["reserve_reason"] = rejection_reason
                row["selection_reason"] = (
                    "补量备选（未达到优选门槛）：" + str(row["selection_reason"])
                )
                reserve_rows.append(row)
                continue
            excluded[rejection_reason] += 1
        selection_rows = [*preferred_rows, *reserve_rows]

        candidates_by_pack = {
            pack_id: sorted(
                [row for row in selection_rows if pack_id in row["matched_pack_ids"]],
                key=lambda item: (
                    0
                    if ranking_mode == "hot" and item.get("hot_protected")
                    else 1,
                    0 if item.get("selection_tier") == "preferred" else 1,
                    (
                        0
                        if item.get("llm_status") == "scored"
                        and item.get("llm_verdict") != "reject"
                        else 1
                        if item.get("llm_status") != "scored"
                        else 2
                    ),
                    bool(item["similar_candidate"]),
                    -float(
                        item["hot_score"]
                        if ranking_mode == "hot"
                        else item["opportunity_score"]
                    ),
                ),
            )
            for pack_id in selected_ids
        }
        assigned: dict[str, list[dict[str, Any]]] = {pack_id: [] for pack_id in selected_ids}
        globally_used_ids: set[str] = set()
        used_ids_by_pack: dict[str, set[str]] = {pack_id: set() for pack_id in selected_ids}
        channel_counts: dict[str, Counter[str]] = {
            pack_id: Counter() for pack_id in selected_ids
        }
        semantic_counts: dict[str, Counter[str]] = {
            pack_id: Counter() for pack_id in selected_ids
        }
        max_channel = max(1, int(config.get("max_per_channel", 2)))
        max_event = max(1, int(config.get("max_per_event", 1)))

        def add_candidate(
            pack_id: str,
            *,
            hot_only: bool = False,
        ) -> bool:
            for candidate in candidates_by_pack[pack_id]:
                if hot_only and not candidate.get("hot_protected"):
                    continue
                video_id = str(candidate["video_id"])
                channel = str(candidate["channel_title"]).casefold()
                semantic = str(candidate["semantic_group"])
                if video_id in used_ids_by_pack[pack_id]:
                    continue
                if video_id in globally_used_ids:
                    continue
                if (
                    channel_counts[pack_id][channel] >= max_channel
                    or semantic_counts[pack_id][semantic] >= max_event
                ):
                    continue
                copy = dict(candidate)
                copy["pack_id"] = pack_id
                copy["pack_label"] = str(pack_by_id[pack_id]["label"])
                assigned[pack_id].append(copy)
                used_ids_by_pack[pack_id].add(video_id)
                globally_used_ids.add(video_id)
                channel_counts[pack_id][channel] += 1
                semantic_counts[pack_id][semantic] += 1
                return True
            return False

        pack_fill_order = sorted(
            selected_ids,
            key=lambda pack_id: (
                len(candidates_by_pack[pack_id]),
                selected_ids.index(pack_id),
            ),
        )
        hot_lane_min_per_pack = max(
            0,
            min(
                per_pack,
                int(config.get("discovery_hot_lane_min_per_pack", 3)),
            ),
        )
        for _ in range(hot_lane_min_per_pack):
            for pack_id in pack_fill_order:
                if len(assigned[pack_id]) < per_pack:
                    add_candidate(pack_id, hot_only=True)
        for _ in range(per_pack):
            for pack_id in pack_fill_order:
                if len(assigned[pack_id]) < per_pack:
                    add_candidate(pack_id)

        groups = [
            {
                "id": pack_id,
                "label": str(pack_by_id[pack_id]["label"]),
                "description": str(pack_by_id[pack_id]["description"]),
                "results": assigned[pack_id],
            }
            for pack_id in selected_ids
        ]
        flattened = [row for pack_id in selected_ids for row in assigned[pack_id]]
        result_counts_by_pack = {
            pack_id: len(assigned[pack_id]) for pack_id in selected_ids
        }
        result_shortfalls_by_pack = {
            pack_id: max(0, per_pack - result_counts_by_pack[pack_id])
            for pack_id in selected_ids
        }
        eligible_counts_by_pack = {
            pack_id: sum(pack_id in row["matched_pack_ids"] for row in rows)
            for pack_id in selected_ids
        }
        selection_eligible_counts_by_pack = {
            pack_id: sum(pack_id in row["matched_pack_ids"] for row in selection_rows)
            for pack_id in selected_ids
        }
        preferred_eligible_counts_by_pack = {
            pack_id: sum(pack_id in row["matched_pack_ids"] for row in preferred_rows)
            for pack_id in selected_ids
        }
        self._save_history(
            history,
            flattened,
            now_utc,
            int(config.get("discovery_history_max_entries", 5000)),
        )
        self._notify(progress, "智能发现完成", 100)
        llm_scored = sum(row.get("llm_status") == "scored" for row in rows)
        visual_scored = sum(row.get("visual_score") is not None for row in rows)
        return {
            "generated_at": now_utc.isoformat().replace("+00:00", "Z"),
            "hours": hours,
            "per_pack": per_pack,
            "groups": groups,
            "results": flattened,
            "summary": {
                "selection_policy_version": 5,
                "ranking_mode": ranking_mode,
                "recall_architecture": "broad_primary_v4_1_adaptive",
                "expected_search_calls_per_pack": int(
                    config.get("discovery_expected_search_calls_per_pack", 6)
                ),
                "maximum_search_calls_per_pack": int(
                    config.get("discovery_max_search_calls_per_pack", 8)
                ),
                "adaptive_page2_enabled": adaptive_enabled,
                "adaptive_min_unique_candidates": adaptive_min_unique,
                "adaptive_min_qualified_candidates": adaptive_min_qualified,
                "adaptive_triggered_packs": list(adaptive_triggered_packs),
                "adaptive_extra_calls_total": sum(
                    adaptive_extra_calls_by_pack.values()
                ),
                "adaptive_extra_calls_by_pack": dict(
                    adaptive_extra_calls_by_pack
                ),
                "adaptive_counts_before": adaptive_before,
                "adaptive_counts_after": adaptive_after,
                "primary_queries": {
                    pack_id: str(plan.get("primary") or "")
                    for pack_id, plan in pack_query_plan.items()
                },
                "supplemental_queries": {
                    pack_id: list(plan.get("supplemental") or [])
                    for pack_id, plan in pack_query_plan.items()
                },
                "recall_orders": recall_orders,
                "hot_lane_queries": hot_lane_queries,
                "hot_minimum_view_count": hot_min_views,
                "hot_minimum_views_per_hour": hot_min_vph,
                "hot_protected_eligible_count": sum(
                    bool(row.get("hot_protected")) for row in rows
                ),
                "hot_protected_result_count": sum(
                    bool(row.get("hot_protected")) for row in flattened
                ),
                "selected_pack_count": len(selected_ids),
                "search_request_count": request_count,
                "search_request_limit": max_requests,
                "search_requests_by_pack": {
                    pack_id: int(search_requests_by_pack[pack_id])
                    for pack_id in selected_ids
                },
                "zero_result_searches_by_pack": {
                    pack_id: int(zero_result_searches_by_pack[pack_id])
                    for pack_id in selected_ids
                },
                "search_pool_size_per_pack": pool_size,
                "recall_target": recall_target,
                "requested_recall_target": requested_recall_target,
                "recall_target_per_pack": per_pack_recall_target,
                "recall_target_reached": not any(recall_shortfalls.values()),
                "recalled_counts_by_pack": {
                    pack_id: len(video_ids)
                    for pack_id, video_ids in recalled_by_pack.items()
                },
                "recall_shortfalls_by_pack": recall_shortfalls,
                "search_quota_exhausted": quota_exhausted,
                "raw_candidate_count": len(ordered_ids),
                "eligible_count": len(rows),
                "eligible_counts_by_pack": eligible_counts_by_pack,
                "selection_eligible_count": len(selection_rows),
                "selection_eligible_counts_by_pack": selection_eligible_counts_by_pack,
                "preferred_eligible_count": len(preferred_rows),
                "preferred_eligible_counts_by_pack": preferred_eligible_counts_by_pack,
                "reserve_eligible_count": len(reserve_rows),
                "result_count": len(flattened),
                "unique_result_count": len(
                    {str(row["video_id"]) for row in flattened}
                ),
                "result_target_per_pack": per_pack,
                "result_limit_per_pack": per_pack,
                "result_counts_by_pack": result_counts_by_pack,
                "result_shortfalls_by_pack": result_shortfalls_by_pack,
                "complete_pack_count": sum(
                    shortfall == 0 for shortfall in result_shortfalls_by_pack.values()
                ),
                "excluded": dict(excluded),
                "history_repeat_count": sum(bool(row["seen_in_previous_search"]) for row in flattened),
                "minimum_view_count": min_views,
                "minimum_views_per_hour": min_vph,
                "expansion_minimum_view_count": expansion_min_views,
                "expansion_minimum_views_per_hour": expansion_min_vph,
                "reserve_minimum_view_count": reserve_min_views,
                "reserve_minimum_views_per_hour": reserve_min_vph,
                "minimum_opportunity_score": minimum_opportunity_score,
                "expansion_minimum_opportunity_score": expansion_minimum_score,
                "reserve_minimum_opportunity_score": reserve_minimum_score,
                "exclude_llm_rejects": exclude_llm_rejects,
                "minimum_duration_seconds": minimum_duration,
                "maximum_duration_seconds": maximum_duration,
                "popularity_filter_mode": popularity_mode,
                "expanded_result_count": sum(
                    row.get("heat_tier") == "expanded" for row in flattened
                ),
                "reserve_result_count": sum(
                    row.get("selection_tier") == "reserve" for row in flattened
                ),
                "llm_enabled": settings.enabled,
                "llm_ready": llm_ready,
                "llm_model": settings.model,
                "llm_candidate_count": len(metadata_rows),
                "llm_scored_count": llm_scored,
                "visual_scored_count": visual_scored,
                "embedding_used": bool(embedding_vectors),
                "planned_query_count": sum(
                    len(value) if isinstance(value, list) else bool(str(value).strip())
                    for value in planned_queries.values()
                ),
                "feedback_count": self.store.feedback_summary()["total"],
                "warnings": list(dict.fromkeys(warnings)),
                "collision_scope": "已下载视频、明确反馈、语义相似题材与轻量展示历史",
            },
        }


__all__ = ["DiscoveryPipeline"]
