# README — optimized read-first version

> **Current state (2026-11-11):** 119 commands / 17 cogs, all 24 checks green
> on this checkout. **AI:** personas (Gilbert + Albert, `/poker` names) on Google
> AI Studio `gemini-3.5-flash`; the daily guide on DeepSeek `deepseek-chat` with
> its own key and `DAILY_GUIDE_TIMEOUT=600` (the pro tier took 206 s for 12k chars
> and kept hitting the 240 s persona timeout — a guide that says
> `_Generated … without the AI model._` timed out, the key is fine); the Cline
> pass key is the backup for either (`CHATBOT_FALLBACK_*`). Persona replies are
> now checked with `looks_out_of_character()`: one in-character retry, then a
> `CHARACTER_DEFLECTION` instead of a leaked AI/model/provider name; both prompts
> say only the record's status is real and only commands change it. **Also uploaded
> 2026-11-10 (stamp `20261110-164655`, 11 files), restart pending:** `/casino withdraw` reserves the worst-case payout of open
> blackjack/poker hands and `/casino balance` shows the held line; account names
> are unique ignoring case; name/leaderboard indexes, a boot `PRAGMA quick_check`
> and a WAL checkpoint on close. Casino cards are simplified — one headline,
> three figures (Bet / Payout / Wallet) and one spectator line; the house stack
> lives on `/casino balance`. The **Forge mod is implemented** (`forge-mod/`, its
> sources mirrored into this checkout on 2026-11-10, Minecraft 1.20.1 / Forge
> 47.x, builds `royalcourt-redeem-1.0.0.jar`), and redemption is gated: an admin
> must run `/server setip` before any shop key can be claimed in-game.

This is a condensed, scannable summary of `README.md` (~3,250 lines → ~60 lines).
Read this first; `README.md` is the reference for every rule below.

## What this bot does
A Discord economy bot for a Minecraft server (Java 1.20.1 / Forge 47.x only — nothing from 1.21+ exists here).

Three economy account types that **never mix**: `nation`, `kingdom`, `individual`.

- Nations / kingdoms: petition creation approved by admins; bi-weekly (14-day) allowances paid to treasuries; leaders draw from kingdom balances only with kingdom-leader approval.
- Individuals: personal wallets, hard-capped at **100,000,000 credits**. `/daily` pays 2,000 credits every 24h (streak +10%/claim to a 400% ceiling / 8,000; −20%/missed day to a 60% floor). Every payout cycle charges a **5% net-worth tax** on the whole wallet.
- Money moves **only** between accounts inside `account_transfers`, one atomic path. Source permission (leader or admin) re-checked under the transaction lock. No raw `delete_nation()`/`delete_kingdom()` calls from commands.

## Economy rules that must not be broken (README §7)

1. **Integer credits only** — never floats for money.
2. **No money printing** — every credit moves between two accounts in one transaction; wins are funded by previous house losses (Casino / Union / Crown).
3. **Ledger reconciles** — every debit/credit writes both `log_tx` rows in the same transaction; replaying `transactions` by `id` reproduces every `balance_after` and stored balance.
4. **Contract approval is atomic** — treasury must hold the whole budget; no credits move until `_pay_claim()` (flag + debit + credit + both ledger rows in one transaction). Short treasury rolls back and retries.
5. **Per-cycle quota** (nation 1, kingdom 3) is re-checked *inside* `create_contract()`'s insert transaction — never trust a pre-check.
6. **Union commitment hook** (`commit_balance_to_union`) runs inside *every* credit to a committed account — payout, contract claim, deposit, transfer destination, draw. `_is_contract_space()` is not the guard.
7. **Individual cap** — a win that would cross 100M is refused *before* the draw, never rolled back.
8. **Two payout paths are separate**: `/payout cash` taxes and pays but never touches `last_cycle_at`/`next_cycle_at` or the contract cycle. `/payout default` runs the full natural cycle (allowances, dues/commitments, tax, `advance_contract_cycle()`, next timestamp).
9. **Contracts never charge the contracting party.** Union pays 25% upfront / 75% on release from its own treasury; membership is never required to request.
10. **Crown tax** = 100% of the regular-soldier payout line, after a 30-day grace. Crown supplier contracts buy one catalog item each (legacy paths only).

## The negotiators' memory (do not regress)
- Every turn is stored in `albert_memory` (`add_persona_turn`), keyed by
  `contract_id`; **nothing is pruned**. `contracts.history` is only the legacy
  40-turn compatibility window.
- `gilbert/memory.py` builds the prompt: newest 16 turns verbatim + one
  `EARLIER IN THIS NEGOTIATION` digest of older turns + 8 lines of
  `PRIOR EXCHANGES WITH THIS ACCOUNT` from earlier cases. Pure arithmetic — never
  replace it with a summarisation provider call on the reply path.
- `GilbertAI.complete()` has a failure breaker (`CHATBOT_BREAKER_SECONDS`, 20 s)
  and jittered backoff; `clean_reply()` strips an echoed persona label.

## Commands
- **119 commands / 17 cogs** (pinned by `checks/check_cogs.py`). The four new
  nodes are the `/server` group (`setip`/`show`/`clear`) that gate the Forge mod.
- `/faction contract` takes exactly `faction`, `account`, `proposal`, `budget`. Retired names (`crown`, `union`, `audience`, `contract request`, …) must not return.
- Shop keys are single-use `RC-XXXXXX-XXXXXX`, DM'd to buyer; `markredeemed` is once-only and never rewrites an existing redemption UUID.
- `/give` is treasury→member only; there is no "individual source" direction.
- `/contract invite` brings a helper into a negotiation (thread membership, or an
  overwrite on the channel fallback) and the persona answers them; close/claim/
  release/reopen stay with the requester and Union Admins. Several negotiations
  run at once: locks are per channel with per-contract history.

## Deployment (README §8)
1. `python3 checks/run_all.py` + `compileall` + `pyflakes` — upload nothing red.
2. Check remote mtime before editing (newer remote = someone uploaded over you).
3. Back up `royal_bank.db`, `royal_bank.db-wal`, `royal_bank.db-shm` together (WAL needs all three).
4. Upload with `.bak-YYYYMMDD-HHMMSS` backups; SHA-256-compare every uploaded file.
5. Pull the deployed `.py` tree into an empty dir and re-run `run_all.py` against *that* copy (catches forgotten uploads).
6. Restart from the hosting panel; migrations run at startup.
7. Never upload `.env`, `*.db`, `*.db-wal`, `*.db-shm` or `__pycache__`.

## Known gaps (README §9)
- **Forge mod:** implemented, and its sources are in this checkout since
  2026-11-10 (`forge-mod/src/main/java/…`, Gradle files, `mods.toml`; built jar and
  `build/` ignored). Target pinned to 1.20.1 / Forge 47.x. `POST /api/preview`
  exists and is non-consuming; the only in-game confirmation is the Claim button.
- **No backup pruning:** backups accumulate (559 on the host); keep 3 newest per file plus the live build's. Prune only on an explicit decision.
- **Turso mirror is append-only:** deleted-member ledger rows survive remotely (681 local vs 685 mirrored). Owner decision needed — do not change silently.
- **Code-level defects from that list are all closed:** the `/casino withdraw`
  open-hand reserve and the case-insensitive name race were fixed on 2026-11-10,
  and the kingdom nation-cap check became transactional on 2026-11-07. The
  remaining items in `BUG-FIXES.md` are deliberate product decisions, not bugs.

## Money paths (where credits flow)
| Path | Source | Destination | Locked in one transaction? |
|------|--------|-------------|---------------------------|
| Transfer | any account | any account | `account_transfers` |
| Payout (natural) | house accounts | nations/kingdoms/individuals + Union + tax | yes |
| Payout (cash) | house accounts | nations/kingdoms + tax | yes |
| Shop purchase | treasury | (key DM'd) | `shop/checkout.py` |
| Kit purchase | treasury | (key DM'd, shulker fee) | `shop/checkout.py` |
| Contract claim | Union/Crown treasury | claim account | `_pay_claim()` |
| Coinflip / All-in / Slots / Roulette / Blackjack / Poker | wallet ↔ Casino | house stack | `casino.begin_wager` / `finish_wager` |
| Fishing catch | Fishing Guild | individual wallet | guarded |
| Bank deposit/withdraw | admin crosses in-game↔Discord | treasury / wallet | admin only |
