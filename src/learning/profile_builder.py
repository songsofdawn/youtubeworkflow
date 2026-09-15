from collections import Counter, defaultdict
from datetime import datetime, timezone

from .schemas import EVENT_WEIGHTS, VideoAnalysis
from .storage import read_json


def build_profile(root, events):
    """Rebuild from valid video JSON + latest explicit event; never count retries."""
    by_video = defaultdict(list)
    for event in events:
        by_video[event["video_id"]].append(event)
    counts = {k: Counter() for k in ("domain", "topic", "entity", "format", "negative", "channel")}
    traits, recent, combinations, suggestions = Counter(), Counter(), {}, {}
    samples = 0
    total_weight = 0.0
    for video_id, history in by_video.items():
        record = read_json(root / "data/learning/videos" / f"{video_id}.json")
        if not record:
            continue
        analysis = VideoAnalysis.model_validate(record["analysis"]).model_dump()
        explicit = [e for e in history if e["kind"] != "download"]
        latest = max(explicit or history, key=lambda e: (e["created_at"], e["event_id"]))
        weight = EVENT_WEIGHTS[latest["kind"]]
        samples += 1
        channel = str(record["metadata"].get("channel_id") or "")
        if channel:
            counts["channel"][channel] += weight
        if weight < 0:
            for term in analysis["entities"] + analysis["topics"] + analysis["formats"]:
                counts["negative"][term.casefold()] += abs(weight)
            continue
        total_weight += weight
        for domain in analysis["domains"]:
            counts["domain"][domain["name"]] += weight * domain["score"]
            joint = combinations.setdefault(domain["name"], {"entities": Counter(), "formats": Counter(), "search_concepts": Counter()})
            for dimension in ("entities", "formats", "search_concepts"):
                for term in analysis[dimension]:
                    joint[dimension][term.casefold()] += weight * domain["score"]
        for name in ("topic", "entity", "format"):
            key = "entities" if name == "entity" else name + "s"
            for term in set(analysis[key]):
                counts[name][term.casefold()] += weight
        for trait, value in analysis["content_traits"].items():
            traits[trait] += value * weight
        age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(latest["created_at"])).total_seconds() / 86400)
        for concept in set(analysis["search_concepts"] + analysis["topics"]):
            recent[concept.casefold()] += weight / (1 + age / 30)
        for suggestion in analysis["taxonomy_suggestions"]:
            name = suggestion["name"]
            entry = suggestions.setdefault(name, {**suggestion, "evidence_video_ids": []})
            entry["evidence_video_ids"].append(video_id)
            entry["evidence_count"] = len(entry["evidence_video_ids"])
    profile = {"version": 1, "sample_size": samples,
               **{name + "_preferences": dict(counter.most_common()) for name, counter in counts.items()},
               "preferred_content_traits": {key: round(value / max(total_weight, 0.001), 4) for key, value in traits.items()},
               "emerging_interests": [{"concept": key, "weight": round(value, 4)} for key, value in recent.most_common(20)],
               "domain_dimensions": combinations}
    return profile, {"version": 1, "taxonomy_suggestions": list(suggestions.values())}
