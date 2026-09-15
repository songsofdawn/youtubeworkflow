from .query_performance import utility


def build_strategy(profile, domains, history, shared_formats=None):
    combinations = []
    negatives = profile.get("negative_preferences", {})
    recent = {item["concept"]: item["weight"] for item in profile.get("emerging_interests", [])}
    for domain in domains:
        dimensions = profile.get("domain_dimensions", {}).get(domain["id"], {})
        entities = list(dict.fromkeys(domain["entities"] + list(dimensions.get("entities", {}))))
        forms = list(dict.fromkeys(domain["formats"] + [f for f in dimensions.get("formats", {}) if f in (shared_formats or {})]))
        for entity in entities:
            for form in forms:
                weight = (1 + profile.get("domain_preferences", {}).get(domain["id"], 0)
                          + dimensions.get("entities", {}).get(entity.casefold(), 0)
                          + profile.get("format_preferences", {}).get(form, 0)
                          + utility(history, domain["id"], entity, form)
                          - negatives.get(entity.casefold(), 0) - negatives.get(form, 0))
                combinations.append({"domain": domain["id"], "entity": entity, "format": form, "weight": max(0.05, round(weight, 4))})
    combinations.sort(key=lambda c: (-c["weight"], c["domain"], c["entity"], c["format"]))
    return {"version": 1, "sample_size": profile.get("sample_size", 0),
            "priority_combinations": combinations,
            "exploration_targets": [c for c in combinations if not any((r["domain"], r["entity"], r["format"]) == (c["domain"], c["entity"], c["format"]) for r in history)],
            "query_seeds": [{"domain": d["id"], "concept": concept, "weight": weight * recent.get(concept, 0.1)}
                            for d in domains for concept, weight in profile.get("domain_dimensions", {}).get(d["id"], {}).get("search_concepts", {}).items()],
            "candidate_channels": [{"channel_id": c, "weight": w} for c, w in profile.get("channel_preferences", {}).items() if w > 0],
            "negative_intents": sorted({v for d in domains for v in d["negative_intents"]})}
