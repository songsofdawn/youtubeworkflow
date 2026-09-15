"""Compatibility adapter for callers of the phase-one generator."""


def generate_queries(strategy, domains, formats, budget, *, seed=None, ranking_mode="hot"):
    output = []
    for row in strategy.get("query_plan", [])[:budget]:
        output.append({**row, "entity": row.get("entity", row.get("query", "")),
                       "format": row.get("format", row.get("intent", "core_topic")),
                       "order": "viewCount" if ranking_mode == "hot" and row.get("recall_source") == "exploitation" else row.get("order", "relevance")})
    if len(output) < budget:
        seen = {row["query"].casefold() for row in output}
        for domain in domains:
            for entity in domain.get("entities", []):
                for format_id in domain.get("formats", []):
                    query = f"{entity} {formats.get(format_id, format_id)}".strip()
                    if query.casefold() in seen:
                        continue
                    output.append({"domain": domain["id"], "entity": entity, "format": format_id, "intent": format_id,
                                   "query": query, "order": "relevance", "generator": "compatibility_fallback",
                                   "recall_source": "coverage", "normalized_query": query.casefold(), "semantic_cluster": query.casefold()})
                    seen.add(query.casefold())
                    if len(output) >= budget:
                        return output
    return output
