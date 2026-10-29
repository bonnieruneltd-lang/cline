"""Workers' Union — Gilbert the Contract Representative."""

from gilbert.prompt import (
    GILBERT_SYSTEM_PROMPT,
    classify_tier,
    contract_context,
    union_audience_context,
    union_audience_fallback,
)

__all__ = [
    "GILBERT_SYSTEM_PROMPT",
    "classify_tier",
    "contract_context",
    "union_audience_context",
    "union_audience_fallback",
]
