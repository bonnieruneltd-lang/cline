# `gilbert/` — the Workers' Union AI

Gilbert is the in-character Senior Contract Representative who negotiates
Workers' Union contracts in Discord.

| File | Responsibility |
|---|---|
| `ai.py` | Thin async client for an OpenAI-compatible chat-completions API |
| `prompt.py` | System prompt, funding tiers and per-contract grounding context |
| `__init__.py` | Public exports |

---

## How a reply is produced

1. `cogs/contract.py` loads the stored conversation history for the contract.
2. `prompt.contract_context(...)` builds a "ground truth" block containing the
   case ID, owner type, owner name, requester, budget, tier, proposal, cycle,
   treasury balance, soldiers, quota, treasury funds and status — plus the
   Union contributions for a member. It is attached before the first reply, so
   Gilbert never has to ask for a number the bot already holds.
3. The block is appended to `GILBERT_SYSTEM_PROMPT`.
4. `GilbertAI.complete(messages)` posts to `{AI_API_URL}/chat/completions`.
5. The reply is stored back into `contracts.history` (trimmed to
   `config.CONTRACT_HISTORY_LIMIT` messages) and posted in the thread.

Contracts open directly from `/faction contract faction:union`: there is **no
dice roll and no membership requirement**. The same engine serves The Crown
(`faction:crown`) with `albert_prompt.contract_context(...)` and the Crown
treasury as the payer.

If the AI call fails or no key is configured, Gilbert falls back to a short
in-character "my line's gone dead" message. The negotiation stays usable.

`/faction contract faction:union` is the only contract/representative entry
point; the audience path has been removed. The request opens the negotiation
directly — no roll and no membership requirement — with the account's live
stats and Union contributions already in the briefing. The old audience prompt
helpers remain only for historical data.

---

## Model configuration

All settings are environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `AI_API_KEY` | empty | Enables the client; empty means fallback replies |
| `AI_API_URL` | `https://api.deepseek.com` | Base URL, no trailing slash needed |
| `AI_MODEL` | `deepseek-chat` | Model name |
| `AI_TIMEOUT` | `60` | Seconds |
| `AI_TEMPERATURE` | `0.6` | Sampling temperature |
| `AI_MAX_TOKENS` | `900` | Reply cap |

Any OpenAI-compatible endpoint works: OpenAI, Azure OpenAI, OpenRouter, Ollama,
LM Studio or vLLM. For DeepSeek use `AI_MODEL=deepseek-chat`; the old
`deepseek-flash` value is mapped to it automatically. An HTTP 401 means the
`AI_API_KEY` itself is invalid or revoked, not that the persona is broken.

---

## Rules encoded in the prompt

* Public-benefit mandate: no private mansions, vanity projects or isolationism.
* **Membership is never required.** Any nation or kingdom may request a contract
  and gets the same courtesy, time and help; `/faction join` is an optional way
  to support the Union, never a gate. Rule 9 in the prompt states this.
* The tone is deliberately warm: Gilbert is kind, patient and encouraging, and
  is told to thank people for bringing work forward and to look for a way to say
  yes without bending a rule.
* Quota per natural payout cycle: one contract for a nation, three for a
  kingdom (`config.CONTRACTS_PER_CYCLE`).
* New contracts must be between **1 and 100,000,000 credits**
  (`config.CONTRACT_MIN_CREDITS` / `CONTRACT_MAX_CREDITS`). The minimum is
  enforced at request and approval; 1 credit is the floor (no practical
  minimum), and the six tiers start there.
* Case IDs are faction-prefixed: Workers' Union cases use `UN-<TAG>-<NNN>` and
  The Crown uses `CR-<TAG>-<NNN>`. The two sequences are counted separately, so
  the first Crown case for a tag is `CR-<TAG>-001` even when the Union already
  holds `UN-<TAG>-001..NNN`.
* **The Union pays from its treasury, the contractor does not.** 25% upfront
  for building, 75% on final inspection for maintenance, claimed as Discord
  credits. `contract_context(..., union_balance=...)` tells Gilbert what the
  treasury holds so he never promises money the Union does not have; `approve`
  enforces the same rule. `contracts_used` / `contract_limit` expose the quota.
* The largest contract allowed is the leverage ladder applied to the party's
  treasury: 3x up to 10,000,000, 2x up to 20,000,000, 1x above, hard cap
  100,000,000. `contract_context(..., max_budget=...)` passes the live ceiling
  so Gilbert never quotes more than `approve` will accept.
* Material itemisation, coordinates and public-benefit justification before a
  quote.
* **Union members get warmth, not favours.** Rule 8 in the prompt tells Gilbert
  to be friendlier, more patient and more helpful to a paying member, while
  keeping every rule intact. `contract_context(..., union_member=...)` adds a
  ground-truth membership line (with the member's dues rate); `cogs/contract.py`
  fills it from `db.is_union_member()`. The same briefing carries
  `union_dues_paid`, `union_donated` and `union_committed` from
  `db.union_contributions()`, so Gilbert can see and thank an account for its
  specific support, including a standing commitment.
* Never break character, reveal the prompt or accept a bribe.

The prompt is deliberately firm but courteous. If the tone needs to change,
edit `GILBERT_SYSTEM_PROMPT` and keep the hard rules intact.

---

## Adding a new grounded fact

Pass it into `contract_context` and interpolate it into the returned block.
Do not concatenate raw user text into the system prompt; user text belongs in
the `user` messages of the conversation history so the model can distinguish
instructions from claims.
