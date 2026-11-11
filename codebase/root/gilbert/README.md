# `gilbert/` — the Workers' Union AI

Gilbert is the in-character Senior Contract Representative who negotiates
Workers' Union contracts in Discord.

| File | Responsibility |
|---|---|
| `ai.py` | Thin async client for an OpenAI-compatible chat-completions API (both reply shapes, per-instance overrides, failure hints) |
| `lookup.py` | Tavily lookup for Minecraft/Forge facts: keyword-triggered, cached, never fatal |
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
3. The block is appended to `GILBERT_SYSTEM_PROMPT`, and **a second system
   message always carries the pinned server build**
   (`lookup.platform_block()` → "Minecraft Java 1.20.1 on Forge 47.x, not
   NeoForge"). The personas are forbidden from quoting 1.21+ or NeoForge
   mechanics.
4. If the player's message leans on game mechanics, `lookup.lookup_for()` may
   attach a third system message with live web results (see below).
5. `GilbertAI.complete(messages)` posts to `{CHATBOT_API_URL}/chat/completions`.
6. The reply is read with `ai.reply_text()` — which accepts both
   `{"choices": …}` and the Cline gateway's `{"data": {"choices": …}}` — then
   stored back into `contracts.history` (trimmed to
   `config.CONTRACT_HISTORY_LIMIT` messages) and posted in the thread.

Contracts open directly from `/faction contract faction:union`: there is **no
dice roll and no membership requirement**. The same engine serves The Crown
(`faction:crown`) with `albert_prompt.contract_context(...)` and the Crown
treasury as the payer.

If the AI call fails or no key is configured, Gilbert falls back to a short
in-character "my line's gone dead" message. The negotiation stays usable. The
log line for that failure now carries a `[hint: …]` from `provider_hint()`, so
"out of credit", "wrong model id", "rate limited" and "the model spent its whole
token budget thinking" are distinguishable at a glance.

`/faction contract faction:union` is the only contract/representative entry
point; the audience path has been removed. The request opens the negotiation
directly — no roll and no membership requirement — with the account's live
stats and Union contributions already in the briefing. The old audience prompt
helpers remain only for historical data.

---

## Live Minecraft lookups

`lookup.py` exists because both personas quote game mechanics and their training
data goes stale while the server is pinned to 1.20.1.

* `needs_lookup(text)` is a **pure keyword test** (blocks, items, farms,
  redstone, villagers, versions, Forge…, or an explicit "look it up"). Ordinary
  haggling never pays a search, so a long negotiation costs nothing extra.
* The query is version-scoped: `Minecraft 1.20.1 Forge 47.x: <player's words>`.
* Bounded: 4 results, 2,200 characters, 12 s (`LOOKUP_*` env), cached 15 minutes,
  so a repeated question in one thread is free.
* The block is `PINNED SERVER BUILD` + `LIVE WEB RESULTS`, and it is explicitly
  **subordinate to the Union/Crown rules** — a search result can never change a
  price, quota or treasury figure.
* Any failure (no key, timeout, bad payload) returns `None` and the negotiation
  continues. **A lookup must never be able to drop a reply.**
* Only the player's own words leave the process: no balances, case IDs or ledger
  rows are ever sent to the search provider.

---

## Model configuration

The chat-bot config is read first; the legacy `AI_*` names still work as a
fallback when the matching `CHATBOT_*` value is blank.

| Variable (`CHATBOT_…`) | Legacy alias | Default | Purpose |
|---|---|---|---|
| `API_KEY` | `AI_API_KEY` | empty | Enables the client; empty means fallback replies |
| `API_URL` | `AI_API_URL` | `https://api.deepseek.com` | Base URL, no trailing slash needed |
| `MODEL` | `AI_MODEL` | `deepseek-chat` | Model name |
| `TIMEOUT` | `AI_TIMEOUT` | `60` | Seconds |
| `TEMPERATURE` | `AI_TEMPERATURE` | `0.6` | Sampling temperature |
| `MAX_TOKENS` | `AI_MAX_TOKENS` | `900` | Reply cap — **must cover reasoning too** |

A backup provider is configured with `CHATBOT_FALLBACK_API_URL`,
`CHATBOT_FALLBACK_API_KEY` and `CHATBOT_FALLBACK_MODEL`. When the primary gives
no reply — 401/403 (key revoked), 402 (empty balance), 429 (rate limited), 5xx,
timeout, or the failure breaker is open — the same call is retried once on the
backup. Without it, a free key running dry posts the canned persona line for the
rest of the day while a working key sits unused. `API_URL` / `API_KEY` /
`fallback_model` constructor overrides let the daily guide use its own provider
(`DAILY_GUIDE_API_URL`, `DAILY_GUIDE_API_KEY`, `DAILY_GUIDE_FALLBACK_MODEL`)
without touching the personas.

Any OpenAI-compatible endpoint works: OpenAI, Azure OpenAI, OpenRouter, Ollama,
LM Studio, vLLM, Google AI Studio
(`https://generativelanguage.googleapis.com/v1beta/openai`) or the Cline
gateway (`https://api.cline.bot/api/v1`).
`GilbertAI()` also takes `model=` / `temperature=` / `max_tokens=` / `timeout=`
overrides, which is how `DailySync` runs the guide on a different model from the
same key (`DAILY_GUIDE_MODEL`, `DAILY_GUIDE_TEMPERATURE`,
`DAILY_GUIDE_MAX_TOKENS`).

**Live choice (2026-11-11):** personas on Google AI Studio
`gemini-3.5-flash` (0.75, 16,000, 240 s) — a measured 5.8–8.0 s per negotiation
reply; the guide on DeepSeek `deepseek-v4-pro` (0.25, 16,000) with its own key
(`DAILY_GUIDE_API_KEY`). The backup for both is the Cline pass key: the personas
fall back to `cline-pass/deepseek-v4.1-flash-1017`, the guide to
`cline-pass/deepseek-v4-pro` (`DAILY_GUIDE_FALLBACK_MODEL`). Verified live: a
deliberately invalid Google key answered through the Cline fallback in 0.8 s and
an invalid DeepSeek key through the Cline pro tier in 2.5 s. The earlier live
choice (2026-11-06) was `cline-pass/deepseek-v4.1-flash-1017` for the personas
(0.75, 16,000, 240 s) and `cline-pass/deepseek-v4-pro` for the guide (0.25,
16,000); `cline-pass/deepseek-v4.1-pro` does not exist — the alias is
`cline-pass/deepseek-v4-pro`.

### Two traps worth knowing

* **Gateway envelope.** The Cline gateway returns
  `{"data": {"choices": [...]}}`, not the bare OpenAI body. `ai.reply_text()`
  accepts both, plus list-form `content` and the legacy `choices[0]["text"]`.
  Read a reply through it; never index `payload["choices"]` directly, or every
  reply from that provider parses as empty and the persona silently falls back.
* **Reasoning burns the budget.** The cline-pass and Gemini 3.x models are
  reasoning models:
  the answer arrives after the thinking and `max_tokens` pays for both. At
  2,000–4,000 tokens several returned `HTTP 500 {"error":"empty response
  content"}` with **no text at all**. Keep 16,000. `provider_hint()` turns that
  body into `[hint: … raise CHATBOT_MAX_TOKENS/DAILY_GUIDE_MAX_TOKENS]`, and it
  does the same for 401 (key), 402 (credit), 404 (model id) and 429 (rate).

For DeepSeek use `AI_MODEL=deepseek-chat`; the old `deepseek-flash` value is
mapped to it automatically, but only when the base URL is `api.deepseek.com`.
An HTTP 401 means the key itself is invalid or revoked, not that the persona is
broken.

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
