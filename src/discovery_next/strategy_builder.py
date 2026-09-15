"""Build the complete, inspectable plan for one discovery round."""
from .domain_allocator import DomainAllocator, allocate_counts
from .query_planner import QueryPlanner

STRENGTHS = {"quick": {"search_budget": 24, "ai_candidate_budget": 60},
             "standard": {"search_budget": 48, "ai_candidate_budget": 120},
             "deep": {"search_budget": 96, "ai_candidate_budget": 240}}


def build_strategy(profile, domains, history, shared_formats=None, *, config=None, mode="potential", strength="standard",
                   manual_focus=None, planner=None, now=None):
    config = config or {}
    strength_cfg = {**STRENGTHS.get(strength, STRENGTHS["standard"]),
                    **config.get("discovery_next", {}).get("strengths", {}).get(strength, {})}
    search_budget = max(1, min(int(config.get("discovery_max_search_requests", 100)), int(strength_cfg["search_budget"])))
    allocation_result = DomainAllocator(config).allocate(profile, domains, history, manual_focus=manual_focus)
    allocation = allocation_result["domain_allocation"]
    query_plan = (planner or QueryPlanner(config)).plan(profile, domains, allocation, history, search_budget, now=now)
    source_weights = config.get("discovery_next", {}).get("recall_source_budget", {
        "exploitation": .22, "learned_concept": .18, "emerging": .14, "preferred_channel": .10,
        "ai_query": .20, "coverage": .07, "exploration": .06, "cross_domain": .03})
    return {"version": 2, "schema_version": 2, "mode": mode, "strength": strength,
            "sample_size": profile.get("sample_size", 0), "search_budget": search_budget,
            "ai_candidate_budget": int(strength_cfg["ai_candidate_budget"]), **allocation_result,
            "domain_budget": allocate_counts({k: v for k, v in allocation.items() if k != "exploration"}, search_budget),
            "recall_budget": allocate_counts(source_weights, search_budget),
            "preselection_budget": config.get("discovery_next", {}).get("candidate_bucket_ratios", {}),
            "query_plan": query_plan,
            "priority_combinations": [{"domain": q["domain"], "entity": q.get("query", ""), "format": q.get("intent", ""),
                "weight": 1 + q.get("performance_prior", 0) + float(profile.get("domain_preferences", {}).get(q["domain"], 0))} for q in query_plan],
            "exploration_targets": [q for q in query_plan if q.get("recall_source") in {"exploration", "coverage", "cross_domain"}],
            "query_seeds": [], "candidate_channels": []}
