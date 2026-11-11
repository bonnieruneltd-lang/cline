# BUG-FIXES — optimized read-first version

This is a condensed, scannable summary of `BUG-FIXES.md` (~1,750 lines → ~45 lines).
Read this first; `BUG-FIXES.md` is the reference for full symptom/cause/impact/fix/test entries.

## How to use this file
When you touch an area, read the entry for that area first: the "Known remaining issues" section tells you what is **known and deliberate** — do not "fix" a design decision or miss a real defect.

Format per entry: Symptom / Cause / Impact / Fix / Test (symbol names to grep for).

## Defects already fixed (covered by checks)
| Area | Defect | Fix | Check |
|------|--------|-----|-------|
| Casino solvency | `/casino withdraw` ignored the worst-case payout of open blackjack/poker hands, so draining the house mid-hand could strand a player's stake | `open_hand_stakes()` + `casino.reserve_for_open_hands()`, read and refused inside the withdraw transaction; `/casino balance` shows the held line | `check_casino.py` |
| Account names | `UNIQUE (guild_id, name)` is case-sensitive while every lookup is `COLLATE NOCASE`, so `Foo`/`foo` could coexist and one was unreachable | `_create_account()` tests the name inside the insert, `rename_account()` inside its transaction, both raising `NameTaken`; commands answer "Name Taken" | `check_creation_race.py` |
| Query cost | Name lookups scanned the guild (BINARY index cannot serve `COLLATE NOCASE`) and `/leaderboard` sorted in a temp B-tree | `idx_*_name_nocase` + `idx_*_balance` + `idx_*_guild`; measured 7.3×/7.9×/9.0× at 20k rows; `quick_check` at boot, WAL checkpoint on close | `check_database.py` |
| Money core | Transaction rollback, debit guard, transfer atomicity, `setbalance` fresh-delta atomicity, ledger reconciliation | Atomic transactions + guards | `check_transactions.py`, `check_database.py`, `check_full_transfers.py` |
| Load / concurrency | One connection + one lock serialised every read behind the writer; two admins could overflow a six-nation kingdom | WAL `query_only` read pool (writer stays single), shared+bounded AI client, 3s autocomplete cache, atomic `assign_nation_to_kingdom()` | `check_load.py`, `check_database.py` |
| Contracts | Leverage ladder, claim idempotency, double click, concurrent claim, 75%-until-release, closed contract, legacy refund, atomic quota, global case IDs, approval re-read, short treasury | Status flip + funding in one transaction; quota inside insert | `check_contracts.py` |
| Draws | Approval race, ex-member re-check, one-shot status | Re-read under lock; status guard | `check_bank.py` |
| Shop | Pricing maths, member vs non-member, mixed cart, shulker fee, double redemption | Atomic checkout + redeem | `check_shop.py` |
| Persona memory | `contracts.history` was a 40-message window that destroyed older turns; `albert_memory` had no caller, so no cross-case recall | Full transcript in `albert_memory` (`add_persona_turn`), `gilbert/memory.py` bounded window (16 verbatim + digest), prior-case recall, legacy backfill | `check_database.py`, `check_contracts.py`, `check_gilbert.py` |
| AI resilience | Every waiting negotiation ran its own retry ladder on a dead endpoint; echoed `Gilbert:` labels leaked into replies; a primary key that ran dry silently posted the canned line while a working backup key sat unused | Per-endpoint failure breaker (3 failures / 20s), jittered backoff, `clean_reply()`, parallel briefing reads, `CHATBOT_FALLBACK_*` retried once on any primary failure | `check_load.py`, `check_gilbert.py`, `check_contracts.py` |
| Persona conduct | A direct model/provider question got "I am Gemini (specifically Gemini 3.6 Flash), built by Google!", the opener greeted the requester as Gilbert, and a chat message claimed a case was "closed/resolved" while it stayed negotiating | `looks_out_of_character()` + one in-character retry + `CHARACTER_DEFLECTION`; intro says "Speaking as <persona>"; both prompts forbid naming an AI/model/provider/version and claim only commands change status | `check_gilbert.py`, `check_contracts.py` |
| Live lookups | Tavily hits went into the prompt unfiltered: a cookie banner could take the budget the wiki answer needed | `lookup.rank_results()` drops consent/paywall shells, promotes Minecraft/Forge hosts, collapses duplicates | `check_gilbert.py` |
| Union | Treasury guards, membership lifecycle, cleanup on delete, dues, donations, commitment approval donation | Hook in every credit path | `check_factions.py` |
| Individuals | 100M cap (partial/full), overdraw guard, deletion erases wallet + ledger, no wallet resurrection | `credit_individual_strict`, `delete_individual()` | `check_bank.py`, `check_database.py` |
| `/daily` | First claim, cooldown, +10%/day, 400% cap, −20%/missed day, 60% floor, concurrency | Pure `daily.evaluate()` | `check_cogs.py` |
| Payout tax | Natural + manual runs, stale-list re-read, deleted/recreated wallet id, ledger both sides, disable switch | Tax inside payout transaction | `check_tax_cycle.py` |
| Gambling | coinflip 50%/zero edge, all-in 10/11/1/11 edge, slots paytable 58,382/64,000 (8.78%), roulette 36/37 on every bet shape, blackjack branches, poker side pots/uncalled refunds/short all-ins | Pure arithmetic priced exhaustively | `check_coinflip.py`, `check_allin.py`, `check_slots.py`, `check_roulette.py`, `check_casino.py`, `check_poker.py` |
| Leaderboards | nation/kingdom/individual separate and ordered | Queries + embeds | `check_cogs.py` |
| One-shot guards | petition flip, draw/creation status, contract double-approval, kit cooldown, concurrent case numbers | One-shot flips inside transactions | `check_creation_race.py`, `check_database.py` |
| Live data | `integrity_check`, per-account and Union ledger reconciliation, union and individuals schema present | Replay ledger by `id` | `check_database.py` |
| API | JSON type handling, retired routes | `api.py` returns 410 for old keys | `checks/` |
| Schema | Migrations over live schema; `orders.union_discount`; `case_counters_global` | Migration entries only | `check_database.py` |
| Runner | Summary required; bare exit/ERROR is a failure | `run_all.py` rejects silent exits | `check_runner.py` |
| Daily sync | Endpoint/value encoding, idempotent DDL, guide source/fallback, guide cut off mid-word/in an open code span, `guild_id`, run-on-start switches, disabled without credentials, and the guide timing out on the pro tier so `fallback_guide()` shipped (an active key looked dead) | Env-driven, never crashes; guide trimmed at a safe boundary with headroom; flash-tier model + its own `DAILY_GUIDE_TIMEOUT` (600 s live) instead of the 240 s persona budget | `check_daily_sync.py` |

**All 24 scripts pass.** Add a check to this table whenever you fix a bug.

## Known remaining issues (deliberate, not fixed) — do not "fix"
1. `/nation delete` destroys the balance silently (admin-only, intentional; UX gap only).
2. `/bank withdraw` is a money faucet by design (admin tool to inject credits; Union has no counterpart on purpose).
3. The v1 API has one static bearer token (single trusted consumer; rotation is manual).
4. `_is_contract_space()` treats any channel starting with `union-contract-` as a contract space (only reachable by manual channel rename; harmless).
5. The Turso mirror is append-only: deleted-member ledger rows survive remotely (681 local vs 685 mirrored). Owner decision needed — mirror the erasure or document as append-only archive. **Do not pick silently.**
6. Deployment backups are never pruned (559 on host); keep 3 newest per file plus the live build's backups. Prune only on explicit decision.
7. Checks are not run automatically (nothing invokes them at boot or on a schedule; regression caught only when someone runs `run_all.py`). The bot does run `PRAGMA quick_check` at boot — a corruption alarm, not the suite.

**Closed 2026-11-10** (were items 5 and 6 here): `/casino withdraw` now reserves the open hands' worst-case payout, and account names are unique ignoring case — see the table above and `BUG-FIXES.md`.
