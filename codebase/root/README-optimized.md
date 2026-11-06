# README — optimized read-first version

This is a condensed, scannable summary of `README.md` (3,182 lines → ~110 lines).
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

## Commands
- **114 commands / 16 cogs** (pinned by `checks/check_cogs.py`).
- `/faction contract` takes exactly `faction`, `account`, `proposal`, `budget`. Retired names (`crown`, `union`, `audience`, `contract request`, …) must not return.
- Shop keys are single-use `RC-XXXXXX-XXXXXX`, DM'd to buyer; `markredeemed` is once-only and never rewrites an existing redemption UUID.
- `/give` is treasury→member only; there is no "individual source" direction.

## Deployment (README §8)
1. `python3 checks/run_all.py` + `compileall` + `pyflakes` — upload nothing red.
2. Check remote mtime before editing (newer remote = someone uploaded over you).
3. Back up `royal_bank.db`, `royal_bank.db-wal`, `royal_bank.db-shm` together (WAL needs all three).
4. Upload with `.bak-YYYYMMDD-HHMMSS` backups; SHA-256-compare every uploaded file.
5. Pull the deployed `.py` tree into an empty dir and re-run `run_all.py` against *that* copy (catches forgotten uploads).
6. Restart from the hosting panel; migrations run at startup.
7. Never upload `.env`, `*.db`, `*.db-wal`, `*.db-shm` or `__pycache__`.

## Known gaps (README §9)
- **Forge mod:** `forge-mod/` is a placeholder README only; target pinned to 1.20.1 / Forge 47.x. No `POST /api/preview` yet — `/api/redeem` redeems in the same call (a disconnect between POST and item grant burns the key).
- **No backup pruning:** backups accumulate (559 on the host); keep 3 newest per file plus the live build's. Prune only on an explicit decision.
- **Turso mirror is append-only:** deleted-member ledger rows survive remotely (681 local vs 685 mirrored). Owner decision needed — do not change silently.
- **Two remaining code-level defects** are documented in BUG-FIXES.md (§"Known remaining issues"): `#/casino withdraw` can strand an open hand's stake; kingdom nation-cap check is not transactional.

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
