"""Gilbert configuration and prompt checks. No network or Discord login."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from gilbert.ai import GilbertAI  # noqa: E402
from gilbert.prompt import (  # noqa: E402
    GILBERT_SYSTEM_PROMPT,
    classify_tier,
    contract_context,
)
import albert_prompt  # noqa: E402
import config  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# The shared chat-bot config (CHATBOT_*) is read first.
with patch.dict(
    os.environ,
    {
        "CHATBOT_API_URL": "https://api.deepseek.com/",
        "CHATBOT_MODEL": "deepseek-flash",
        "CHATBOT_API_KEY": "test-key",
        "CHATBOT_TIMEOUT": "12",
        "CHATBOT_MAX_TOKENS": "64",
        "AI_API_URL": "",
        "AI_MODEL": "",
        "AI_API_KEY": "",
        "AI_TIMEOUT": "",
        "AI_MAX_TOKENS": "",
    },
    clear=False,
):
    ai = GilbertAI()
    check("deepseek-flash is aliased to deepseek-chat", ai.model == "deepseek-chat", ai.model)
    check("base URL has no trailing slash", ai.base_url == "https://api.deepseek.com", ai.base_url)
    check("key marks the client enabled", ai.enabled is True)
    check("timeout is parsed", ai.timeout == 12.0, ai.timeout)
    check("token cap is parsed", ai.max_tokens == 64, ai.max_tokens)

# The legacy AI_* names still work as a fallback.
with patch.dict(
    os.environ,
    {
        "CHATBOT_API_URL": "",
        "CHATBOT_MODEL": "",
        "CHATBOT_API_KEY": "",
        "CHATBOT_TIMEOUT": "",
        "CHATBOT_MAX_TOKENS": "",
        "AI_API_URL": "https://legacy.test/",
        "AI_MODEL": "legacy-model",
        "AI_API_KEY": "legacy-key",
    },
    clear=False,
):
    ai = GilbertAI()
    check("legacy AI_* fallback: URL", ai.base_url == "https://legacy.test", ai.base_url)
    check("legacy AI_* fallback: model", ai.model == "legacy-model", ai.model)
    check("legacy AI_* fallback: key", ai.enabled is True)

# CHATBOT_* wins over AI_* when both are set.
with patch.dict(
    os.environ,
    {"CHATBOT_MODEL": "chatbot-model", "AI_MODEL": "legacy-model"},
    clear=False,
):
    ai = GilbertAI()
    check("CHATBOT_MODEL beats AI_MODEL", ai.model == "chatbot-model", ai.model)

with patch.dict(
    os.environ,
    {
        "CHATBOT_API_URL": "https://example.test/v1/chat/completions",
        "CHATBOT_MODEL": "custom-model",
        "CHATBOT_API_KEY": "",
        "AI_API_URL": "",
        "AI_MODEL": "",
        "AI_API_KEY": "",
    },
    clear=False,
):
    ai = GilbertAI()
    check("full chat-completions URL is normalised", ai.base_url == "https://example.test/v1", ai.base_url)
    check("empty key means disabled", ai.enabled is False)
    check("custom model is preserved", ai.model == "custom-model", ai.model)

check("micro tier floor is the contract minimum",
      classify_tier(config.CONTRACT_MIN_CREDITS) == "Micro-Utility Tier",
      classify_tier(config.CONTRACT_MIN_CREDITS))
check("below contract minimum", "Below" in classify_tier(config.CONTRACT_MIN_CREDITS - 1),
      classify_tier(config.CONTRACT_MIN_CREDITS - 1))
context = contract_context(
    case_id="UN-TEST-001",
    nation_name="Test",
    requester_name="Tester",
    budget=1_000_000,
    proposal="Build a public road",
    cycle=1,
    nation_balance=5_000_000,
    status="negotiating",
    owner_type="nation",
    max_budget=15_000_000,
    union_balance=20_000_000,
    contracts_used=0,
    contract_limit=1,
    union_member=True,
    union_dues_paid=1_000,
    union_donated=2_000,
    union_committed=3_000,
    soldiers=12,
)
for needle in ("UN-TEST-001", "Test", "public road", "5,000,000", "20,000,000", "PAYING MEMBER"):
    check(f"context contains {needle!r}", needle in context)
check("Gilbert's briefing preloads soldiers",
      "Soldiers fielded by this nation: 12" in context)

check("Gilbert is asked to be kind", "kind" in GILBERT_SYSTEM_PROMPT.lower())
check("Gilbert is asked to be encouraging",
      "encouraging" in GILBERT_SYSTEM_PROMPT.lower())
check("Gilbert's rules say membership is never required",
      "Membership is never required" in GILBERT_SYSTEM_PROMPT)
check("Gilbert's tier table starts at the 1-credit floor",
      "Micro-Utility Tier (1 to 499,999 credits)" in GILBERT_SYSTEM_PROMPT)
check("Gilbert is told 1 credit is the floor",
      "1 credit is the floor" in GILBERT_SYSTEM_PROMPT)
check("Gilbert knows the Crown CR case prefix",
      "CR-[NATION OR KINGDOM]-[ID]" in GILBERT_SYSTEM_PROMPT)
check("Albert is asked to be warm", "warm" in albert_prompt.ALBERT_SYSTEM_PROMPT.lower())
albert_flat = " ".join(albert_prompt.ALBERT_SYSTEM_PROMPT.split())
check("Albert's rules open contracts to non-members",
      "regardless of faction or membership" in albert_flat)
check("Albert's rules fund from the Crown treasury",
      "Crown buys supplies" in albert_flat)

albert_context = albert_prompt.contract_context(
    case_id="CR-TEST-001",
    owner_type="nation",
    account_name="Test",
    requester_name="Tester",
    budget=1_000_000,
    proposal="Build a public well",
    tier=classify_tier(1_000_000),
    cycle=2,
    account_balance=5_000_000,
    crown_balance=20_000_000,
    crown_reserved=1_500_000,
    contracts_used=0,
    contract_limit=1,
    soldiers=12,
    faction=None,
    status=None,
    donations=5_000,
    supply_units=0,
    tax_active=False,
    tax_reason="Not a Crown member",
)
for needle in (
    "CR-TEST-001", "Build a public well", "1,000,000 credits",
    "20,000,000 credits", "Soldiers: 12", "Membership is never required",
):
    check(f"Albert's briefing contains {needle!r}", needle in albert_context)
check("Albert's briefing covers supply collection",
      "collection point" in albert_context and "goods" in albert_context)

print("\nFAILURES:", len(fails))
for failure in fails:
    print(" -", failure)
sys.exit(1 if fails else 0)
