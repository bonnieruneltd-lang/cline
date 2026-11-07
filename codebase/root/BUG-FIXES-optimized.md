# BUG-FIXES — optimized read-first version

This is a condensed, scannable summary of `BUG-FIXES.md` (~1,750 lines → ~45 lines).
Read this first; `BUG-FIXES.md` is the reference for full symptom/cause/impact/fix/test entries.

## How to use this file
When you touch an area, read the entry for that area first: the "Known remaining issues" section tells you what is **known and deliberate** — do not "fix" a design decision or miss a real defect.

Format per entry: Symptom / Cause / Impact / Fix / Test (symbol names to grep for).

## Defects already fixed (covered by checks)
| Area | Defect | Fix | Check |
|------|--------|-----|-------|
| Money core | Transaction rollback, debit guard, transfer atomicity, `setbalance` fresh-delta atomicity, ledger reconciliation | Atomic transactions + guards | `check_transactions.py`, `check_database.py`, `check_full_transfers.py` |
| Load / concurrency | One connection + one lock serialised every read behind the writer; two admins could overflow a six-nation kingdom | WAL `query_only` read pool (writer stays single), shared+bounded AI client, 3s autocomplete cache, atomic `assign_nation_to_kingdom()` | `check_load.py`, `check_database.py` |
| Contracts | Leverage ladder, claim idempotency, double click, concurrent claim, 75%-until-release, closed contract, legacy refund, atomic quota, global case IDs, approval re-read, short treasury | Status flip + funding in one transaction; quota inside insert | `check_contracts.py` |
| Draws | Approval race, ex-member re-check, one-shot status | Re-read under lock; status guard | `check_bank.py` |
| Shop | Pricing maths, member vs non-member, mixed cart, shulker fee, double redemption | Atomic checkout + redeem | `check_shop.py` |
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
| Daily sync | Endpoint/value encoding, idempotent DDL, guide source/fallback, guide cut off mid-word/in an open code span, `guild_id`, run-on-start switches, disabled without credentials | Env-driven, never crashes; guide trimmed at a safe boundary with headroom | `check_daily_sync.py` |

**All 22 scripts pass.** Add a check to this table whenever you fix a bug.

## Known remaining issues (deliberate, not fixed) — do not "fix"
1. `/nation delete` destroys the balance silently (admin-only, intentional; UX gap only).
2. `/bank withdraw` is a money faucet by design (admin tool to inject credits; Union has no counterpart on purpose).
3. The v1 API has one static bearer token (single trusted consumer; rotation is manual).
4. `_is_contract_space()` treats any channel starting with `union-contract-` as a contract space (only reachable by manual channel rename; harmless).
5. `/casino withdraw` can strand an open hand's stake (withdraw checks current house balance, not worst-case reserve of open blackjack/poker hands).
6. Account name uniqueness is case-sensitive in SQLite but case-insensitive in queries (`UNIQUE` allows `Foo`+`foo` side by side; `COLLATE NOCASE` lookups return one; needs two near-simultaneous requests to collide).
7. The Turso mirror is append-only: deleted-member ledger rows survive remotely (681 local vs 685 mirrored). Owner decision needed — mirror the erasure or document as append-only archive. **Do not pick silently.**
8. Deployment backups are never pruned (559 on host); keep 3 newest per file plus the live build's backups. Prune only on explicit decision.
9. Checks are not run automatically (nothing invokes them at boot or on a schedule; regression caught only when someone runs `run_all.py`).
