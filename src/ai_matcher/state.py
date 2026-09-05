from typing import TypedDict, Any


class ReconciliationState(TypedDict):
    """
    The state object passed between nodes in the LangGraph workflow.
    Tracks everything needed to reconcile a batch of payments.
    """
    config: Any
    open_invoices: dict[str, dict]
    unreconciled_payments: list[dict]
    current_payment_index: int
    current_match_proposals: list[dict]
    reconciliation_results: list[dict]
    pending_reviews: list[dict]
    match_memory_rules: list[dict]
    purchase_orders_context: list[dict]  # retained for nodes.py compatibility; unused in AR-only extract
    tenant_id: Any
    original_balances: dict[str, float]
    in_run_allocated: dict[str, float]
