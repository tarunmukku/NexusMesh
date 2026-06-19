"""NexusMesh Guard — Agent 3 (Fraud Detection + Network Intelligence + Feedback Loop).

Rule-based ensemble (P001-P008) + Šubelj (2011) shared-entity ring detection + an SIU
feedback loop, fused into a 0-100 fraud score and a traffic-light tier per claim.
"""

from agents.fraud_agent.tools import (
    FRAUD_TOOLS,
    load_fraud_db,
    load_ring_relationships,
    load_investigation_history,
    load_ofac_names,
    match_individual_patterns,
    detect_fraud_rings,
    network_patterns_for_claim,
    history_precedents,
    compute_fraud_score,
    assign_tier,
    score_claim,
    score_batch,
    score_batch_tool,
    score_local_batch_tool,
    detect_fraud_rings_tool,
    read_investigation_history_tool,
    query_fraud_db_tool,
    check_ofac_screen_tool,
    compute_fraud_score_tool,
)
from agents.fraud_agent.prompt import FRAUD_SYSTEM_PROMPT

__all__ = [
    "FRAUD_TOOLS",
    "FRAUD_SYSTEM_PROMPT",
    "load_fraud_db",
    "load_ring_relationships",
    "load_investigation_history",
    "load_ofac_names",
    "match_individual_patterns",
    "detect_fraud_rings",
    "network_patterns_for_claim",
    "history_precedents",
    "compute_fraud_score",
    "assign_tier",
    "score_claim",
    "score_batch",
    "score_batch_tool",
    "score_local_batch_tool",
    "detect_fraud_rings_tool",
    "read_investigation_history_tool",
    "query_fraud_db_tool",
    "check_ofac_screen_tool",
    "compute_fraud_score_tool",
]
