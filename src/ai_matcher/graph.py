from __future__ import annotations
"""
Graph topology for AR cash-application reconciliation.

Pipeline (single-tenant extract):
  init → rule_matcher → memory_matcher → ai_matcher
       → overpayment_classifier → verify → END

Advance classification is omitted here (future work). The node function remains
in nodes.py as a no-op for source fidelity.
"""
from langgraph.graph import StateGraph, END
from .state import ReconciliationState
from .nodes import (
    node_init,
    node_rule_matcher,
    node_memory_matcher,
    node_ai_semantic_matcher,
    node_overpayment_classifier,
    node_verification_and_hitl,
)


def build_graph():
    workflow = StateGraph(ReconciliationState)

    workflow.add_node("init", node_init)
    workflow.add_node("rule_matcher", node_rule_matcher)
    workflow.add_node("memory_matcher", node_memory_matcher)
    workflow.add_node("ai_matcher", node_ai_semantic_matcher)
    workflow.add_node("overpayment_classifier", node_overpayment_classifier)
    workflow.add_node("verify", node_verification_and_hitl)

    workflow.set_entry_point("init")
    workflow.add_edge("init", "rule_matcher")
    workflow.add_edge("rule_matcher", "memory_matcher")
    workflow.add_edge("memory_matcher", "ai_matcher")
    workflow.add_edge("ai_matcher", "overpayment_classifier")
    workflow.add_edge("overpayment_classifier", "verify")
    workflow.add_edge("verify", END)

    return workflow.compile()
