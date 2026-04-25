"""Context compaction system — three-layer architecture.

Layer 1 (micro_compact):  Clear old tool_result content before API calls.
                          No LLM call, just string replacement.

Layer 2 (auto_compact):   LLM summarizes conversation when token usage
                          exceeds threshold. Replaces history with summary.

Layer 3 (reactive_compact): Truncate oldest message groups when API returns
                            prompt_too_long. Last-resort fallback.
"""
