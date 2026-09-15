import random


def generate_queries(strategy, domains, formats, budget, *, seed=None, ranking_mode="hot"):
    """Weighted combinations with reserved exploration; no persistent query pool."""
    rng = random.Random(seed)
    by_id = {d["id"]: d for d in domains}
    candidates = list(strategy["priority_combinations"])
    exploration = list(strategy["exploration_targets"])
    output, seen = [], set()
    while candidates and len(output) < budget:
        pool = exploration if len(output) % 5 == 4 and exploration else candidates
        choice = rng.choices(pool, weights=[c["weight"] for c in pool], k=1)[0]
        candidates.remove(choice)
        if choice in exploration:
            exploration.remove(choice)
        domain = by_id[choice["domain"]]
        # Domain ID is a semantic label, never a primary search query.
        label = domain["id"].replace("_", " ")
        entity = choice["entity"]
        query = " ".join([label if label.casefold() not in entity.casefold() else "", entity, formats[choice["format"]]]).strip()
        if len(query) > 120 or query.casefold() in seen:
            continue
        seen.add(query.casefold())
        order = "viewCount" if ranking_mode == "hot" and len(output) % 3 == 0 else "relevance"
        output.append({**choice, "query": query, "order": order, "generator": "template"})
    # Recent concepts are ephemeral and must earn their own performance record.
    for concept in sorted(strategy.get("query_seeds", []), key=lambda c: -c["weight"])[:max(0, budget // 5)]:
        index = next((i for i, item in enumerate(output) if item["domain"] == concept["domain"] and item["generator"] == "template"), None)
        if index is not None:
            query = f'{output[index]["entity"]} {concept["concept"]}'[:120]
            if query.casefold() not in seen:
                output[index] = {**output[index], "query": query, "generator": "learned_concept"}
                seen.add(query.casefold())
    # Candidate channels share the same global budget and performance system.
    for index, channel in enumerate(strategy.get("candidate_channels", [])[:budget // 6]):
        if index < len(output):
            output[-1-index] = {**output[-1-index], "query": "", "channel_id": channel["channel_id"], "order": "date", "generator": "channel"}
    return output
