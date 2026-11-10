# Agent guide — optimized read-first version

> **Current state (2026-11-10):** 119 commands / 17 cogs, all 24 checks green
> on this checkout. Casino cards are simplified — one headline, three figures
> (Bet / Payout / Wallet) and one spectator line; the house stack lives on
> `/casino balance` — **uploaded, needs a panel restart**. The **Forge mod is
> implemented** (`forge-mod/`, Minecraft 1.20.1 / Forge 47.x, builds
> `royalcourt-redeem-1.0.0.jar`), and redemption is gated: an admin must run
> `/server setip` before any shop key can be claimed in-game. Persona memory,
> the provider failure breaker and the parallel briefing reads are **local, not
> uploaded**.

This is a condensed, scannable summary of `AGENTS.md` (~84 lines against ~1,180).
Read this first; the full `AGENTS.md` is the reference for everything below.

## What this repo is
Local mirror of the live **Royal Court** Discord bot (economy, contracts, gambling,
shop/kits, factions). Runs from the SFTP account `bl_7746.e75c1364` on
`15.235.11.50:2022`. This checkout is a **working copy, not a deployment origin**.

## Quick start
```
python3 -m pip install -r requirements.txt   # discord.py, aiosqlite, python-dotenv, aiohttp
python3 checks/run_all.py                    # whole suite: expect 23/23
python3 -m compileall -q .                   # syntax
python3 -m pyflakes .                        # unused / undefined names
```
- Secrets (`SFTP_HOST/PORT/User/PASS`, `DISCORD_TOKEN`, `TAVILY_API_KEY`, …) are
  injected per session, **never** persisted here. Recover from `~/.bash_history`
  into a `chmod 600` file such as `/tmp/.sftp_env` and `source` it.
- Never upload `.env`, `*.db`, `*.db-wal`, `*.db-shm` or `__pycache__`.
- `paramiko` is needed for SFTP only, not a bot dependency.

## Command surface (pinned)
- **119 commands / 17 cogs**, asserted by `checks/check_cogs.py`. The new
  four-node `/server` group (`setip`/`show`/`clear`) gates Forge-mod redemption.
- Only these account kinds exist: `nation`, `kingdom`, `individual`. No
  "individual source" anywhere — `/give` is treasury→member only.
- `/contract invite` adds a helper to a negotiation and the persona answers them
  (`Thread.fetch_member`, never `Thread.permissions_for`); several negotiations
  run concurrently, one lock per channel.
- `/faction` is the only faction command; `/faction contract` exposes exactly
  `faction`, `account`, `proposal`, `budget`. Retired names (`crown`, `union`,
  `audience`, `contract request`, …) must not come back.

## Hard invariants (do not break)
1. **No money printing.** Every credit moves between two accounts inside one
   transaction; a win is funded by previous losses via the Casino/Union/Crown
   house balance. Union inflows are only `/faction fund`, `/faction donate`,
   member dues, and approved commitments.
2. **Ledger reconciles.** Every debit/credit writes both ledger rows in the same
   transaction; replaying `transactions` in `id` order must reproduce every
   `balance_after` and the stored balance for nations, kingdoms, individuals,
   Casino, Union, Crown and Fishing Guild.
3. **No silent burns.** `/nation delete` / `/kingdom delete` use
   `Database.delete_account()` (re-reads row, writes negative ledger row,
   `balance_after = 0`) — never call `delete_nation()`/`delete_kingdom()`
   directly from a command.
4. **Individual cap = 100,000,000.** A win that would cross it is refused
   *before* the draw, never rolled back after. `credit_individual_strict` raises
   `CapExceeded` instead of trimming.
5. **Contract approval is atomic.** `approve_contract()` flips status + budget
   + claim account inside one transaction; the command's pre-read is feedback
   only. Reject/cancel are final (`rejected`, `cancelled` are not in
   `REOPENABLE_STATUSES`).
6. **Per-cycle quota is checked inside the insert transaction** (`create_contract`
   `quota_limit`); never rely on the command's pre-check.
7. **Union commitment hook runs inside the same transaction as every credit to
   a committed account** (payout, contract claim, deposit, transfer destination,
   approved draw) — `commit_balance_to_union()` asserts `_in_tx`.
8. **Tax runs inside the payout transaction** (`Payout._charge_union_dues` plus
   `individual_tax`); payout and tax never diverge.

## Persona memory (new 2026-11-10)
- Every turn is appended to `albert_memory` (`add_persona_turn`, `contract_id`
  keyed) and **never pruned**; `contracts.history` is only the 40-turn legacy
  compatibility window.
- `gilbert/memory.py` builds the prompt: 16 turns verbatim, older turns as an
  `EARLIER IN THIS NEGOTIATION` digest, plus `PRIOR EXCHANGES WITH THIS ACCOUNT`
  from earlier cases. Pure arithmetic — never a provider call on the reply path.
- `GilbertAI.complete()` opens a failure breaker after 3 hard failures
  (`CHATBOT_BREAKER_SECONDS`) and jitters its backoff; `clean_reply()` strips an
  echoed persona label. `_agent_reply`/`_crown_contract_context` gather their
  independent reads on the WAL read pool — keep new reads inside those gathers.

## Money paths to know
- Transfers: `account_transfers` is the single atomic path for all nine
  directions (nation/kingdom/individual × source/destination). Source permission
  (leader or admin) re-checked under the transaction lock.
- Payouts: natural cycle = cash allowances + Union dues/commitments +
  individual 5% net-worth tax + contract-cycle advance + next 14-day timestamp.
  `/payout cash` taxes too and never advances the cycle or timestamps.
- Shop/kits: `shop/checkout.py` debits the treasury and writes the order
  atomically; `RC-XXXXXX-XXXXXX` key DM'd to buyer; `markredeemed` is once-only
  and does not rewrite an existing redemption UUID. Kits are always shulker-
  packaged; Union 50% discount on eligible categories replaces (not stacks with)
  the treasury discount.

## Where to find detail
- `README.md` §7 economy rules, §10 contract payout model, §12 maintenance log.
- `BUG-FIXES.md` — every defect and which check covers it; table maps area→check.
- Full `AGENTS.md` — nation/kingdom creation/rename/delete plumbing, contract
  claim/escrow/reopen, Crown tax/grace, daily sync/Turso mirror, known gaps.

## Known gaps (don't build these unless decided)
- Forge mod is README only (`forge-mod/`); target pinned to **Minecraft 1.20.1 /
  Forge 47.x**. Contract-key redemption is retired (`410 no_in_game_currency`).
- No `POST /api/preview` for shop keys; `/api/redeem` redeems in the same call.
- No schema/backup pruning; retention rule is in `AGENTS.md` "Deploying" and
  README §12 item 9.
