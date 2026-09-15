"""Adapter that normalizes the v2 query plan for recall execution."""


def generate_queries(strategy, domains, formats, budget, *, seed=None, ranking_mode="potential"):
    output = []
    for row in strategy.get("query_plan", [])[:budget]:
        output.append({**row, "entity": row.get("entity", row.get("query", "")),
                       "format": row.get("format", row.get("intent", "core_topic")),
                       "order": "viewCount" if ranking_mode == "hot" and row.get("recall_source") == "exploitation" else row.get("order", "relevance")})
    return output
