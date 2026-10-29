# The Royal Court — Discord bot + Minecraft Forge companion

This repository is the **Royal Bank / Royal Court** bot for the Minecraft server
**The Realm of Rune**, plus the placeholder for the **Forge mod** that will
redeem shop orders and contract keys inside Minecraft.

The bot runs as a Discord application. It owns the economy: nations, kingdoms,
individual wallets, Royal Bank balances, bi-weekly payouts, the Royal Shop,
kits, the audit ledger and the Workers' Union contract system run by the
Gilbert AI persona.

> **Secrets:** never commit `.env` or `royal_bank.db`. They contain live tokens,
> API keys and player data.

---

## 1. What the finished system is meant to do

1. Players found **nations** (one leader each) and **kingdoms** (up to six
   member nations). Creation is a petition that an admin approves in Discord.
2. Every account has a **Royal Bank balance** in credits. Admins convert
   in-game currency to credits with `/bank deposit` and back with
   `/bank withdraw`; players transfer credits between accounts.
3. Every 14 days the bot pays **nation and kingdom allowances** automatically,
   posts a summary and writes every movement to the ledger.
4. Players spend credits in the **Royal Shop** on vanilla items and kits. A
   purchase produces a single-use `RC-XXXXXX-XXXXXX` key, DMed to the buyer.
5. A **Forge mod** (not built yet) redeems those keys in-game and gives the
   items to the player. There is **no Minecraft account linking** — the key is
   the credential and the player UUID is recorded at redemption.
6. Nations and kingdoms can bring a **proposal** to either faction. **The
   Workers' Union** negotiates public works through Gilbert; **The Crown** buys
   **supplies** through Albert. Both open a private Discord thread from
   `/faction contract` with a single free-text `proposal` — what will be
   provided, how much, and when/where. An admin approves the contract.
   Crown supplies are collected in-game by an admin; the bot does not track item
   counts. **The requested faction funds the contract out of its own treasury;
   the supplier is never charged.** The Union treasury is topped up by admins
   (`/faction fund`) and filled by member dues and donations; the Crown treasury
   is funded by admins and its tax. Contract credits are never minted.
   The 25% upfront tranche becomes claimable at approval; the 75% at release —
   after inspection for Union works, and after an officer has collected the
   goods for Crown supplies. Credits land in the contract's claim account with
   `/contract claim` or the Claim Credits button.
   **Faction membership is never required to request a contract.**
   There is **no in-game currency for contracts and no contract keys**.
7. Nations and kingdoms may **join the Workers' Union**
   (`/faction join faction:union`). Members
   get 50% off building blocks and food in the Royal Shop and pay dues on every
   payout (1.75% for a nation, 2% for a kingdom) on their **whole balance**, not
   just the payout, into the Union treasury.
8. Every member also has a personal **individual** balance. It is open to
   everyone, separate from every treasury, and hard-capped at 100,000,000
   credits: earnings past the cap are never stored. `/daily` pays 2,000 credits
   once every 24 hours, adding 10 percentage points per consecutive claim to a
   400% ceiling (8,000) and losing 20 points per missed day down to a 60% floor.
   Individuals can move money with `/individual transfer` (to another member),
   `/individual pay` (to a nation or kingdom treasury) and the unified
   `/transfer` command, which works in every direction between individual
   wallets, nation treasuries and kingdom treasuries. A nation or kingdom leader
   can also pay a member directly with `/give source:<treasury> member:<@user>
   amount:<credits>`, which debits the treasury through the same transfer core.
   Every payout run (Cash
   Cycle and Default Cycle) charges a **5% net worth tax** on their whole
   balance. They receive
   no shop discounts, can erase their wallet with `/individual delete`, and rank
   on their own leaderboard — the three economy types are never merged into one
   ranking. **Nations and kingdoms pay no net worth tax and have no balance
   cap**; their only charge is Workers' Union dues, and only for members.
9. `/fish` gives every member a public catch: one random fish worth **20–100
   credits**, paid into the caller's individual wallet from the **Fishing Guild**
   house account. The guild is funded and drained only by admins
   (`/fishingguild fund` / `withdraw`), so a catch moves credits instead of
   minting them; `/fishingguild balance` shows what it can pay.

---

## 2. Status board

Legend: **DONE** = implemented and verified, **WIP** = partially implemented,
**TODO** = not started. Section 12 records whether a given build is live or
still local-only.

| Area | Status | Notes |
|---|---|---|
| Nation creation + admin approval | DONE | `/nation request`, `/decree pending` |
| Kingdom creation + admin approval | DONE | `/kingdom request`, max 6 nations |
| Royal Bank (balance/deposit/withdraw/transfer/draw) | DONE | `/bank ...` |
| Automatic 14-day payouts | DONE | Nation + kingdom formulas; the 14-day job runs the Default Cycle |
| Cash cycle | DONE | `/payout cash` [Admin]: immediate cash payouts, no schedule or contract-cycle change |
| Default cycle | DONE | `/payout default` [Admin]: cash allowances + dues + 5% tax + natural schedule/contract advance in one run |
| Natural cycle trigger | DONE | `/payout trigger` [Admin]: queue the cycle now or schedule it N days out without paying |
| Royal Shop catalog and checkout | DONE | Data-driven catalog in `shop/catalog_data.py` |
| Shop order keys (`RC-...`) and DM delivery | DONE | Redeemed by the mod or `/shop markredeemed` |
| Player-created kits | DONE | `/kit ...`, 7-day cooldown, shulker packaging |
| Ledger and CSV export | DONE | `/ledger ...` |
| Gilbert AI contract negotiations | DONE | OpenAI-compatible chat endpoint |
| Nation proposals | DONE | `/faction contract faction:union account:🏛️ Nation · ... proposal:... budget:...` |
| Kingdom proposals | DONE | Same command; the kingdom treasury is the leverage base |
| Contract leverage limits | DONE | `contract_limits.py`; new contracts 1–100M (no practical minimum), ladder 3x/2x/1x |
| Contract quota | DONE | 1 per nation, 3 per kingdom, per natural cycle and per faction |
| Contract payouts (25/75) | DONE | The requested faction funds the credits; supplier never charged |
| Contract claiming | DONE | `/contract claim` + Claim Credits button on the board |
| Union treasury | DONE | `/faction fund`, `/faction donate`; every Union claim debits it |
| Union membership | DONE | `/faction join` / `/faction leave`; 50% off building blocks and food |
| Union dues | DONE | 1.75% nation / 2% kingdom of the whole balance, members only |
| Union commitments | DONE | `/faction commitment`; admin-approved, donates the current balance and every later credit; `/faction uncommit` stops it |
| Factions | DONE | One allegiance per nation/kingdom: Workers' Union or The Crown; one `/faction` command, switching cancels the old side's commitments. Contracts never require membership |
| Crown tax | DONE | 100% of the regular-soldier payout line, 30-day grace, into the Crown treasury |
| Crown supply proposals | DONE | Albert negotiates; an officer collects the goods in-game; 25/75 from the Crown treasury |
| Crown supplier contracts | LEGACY | Old item rows still deliver and pay (`/faction deliver`); no new ones are created |
| Crown treasury | DONE | `/faction status`, `/faction fund` [Admin], `/faction withdraw` [Admin]; Crown claims, supplier payments and tax settle against it |
| Faction contract requests | DONE | `/faction contract faction account proposal budget`; **no dice roll and no membership requirement**; opens Gilbert (Union) or Albert (Crown) with the account's live numbers already in the prompt |
| Individual balances | DONE | `/individual ...`; one wallet per member, hard cap 100M, no discounts |
| Unified transfers | DONE | `/transfer` (and `/bank transfer`) between individuals, nations and kingdoms in all directions; `/give` is the treasury-to-member shortcut |
| Casino house account | DONE | `/casino balance`, `/casino fund`, `/casino withdraw`; every wager settles against it |
| Coinflip gambling | DONE | `/individual coinflip`: 50% win, 2x payout, fair pass-through, public |
| All-in gamble | DONE | `/allin`: whole wallet on one draw, n-times payout at 1 in (1.1n), public |
| Fishing | DONE | `/fish`: random 20–100 credit catch paid by the Fishing Guild, public |
| Fishing Guild house account | DONE | `/fishingguild balance`, `fund` [Admin], `withdraw` [Admin]; every catch debits it |
| Blackjack | DONE | `/blackjack amount`: hit/stand, 3:2 natural, dealer stands on 17, public |
| Gambling floor UI | DONE | public result cards, poker table/result cards, house dashboard, spectator prompts and reaction bait |
| Poker (Texas Hold'em) | **DONE** | `/poker`: four-seat public table vs three house bots; fold/check/call/raise/all-in; buy-in held, timeout fold, restart refund, final-stack settlement |
| Individual net worth tax | DONE | 5% of the whole wallet on every payout run (Cash and Default); nations/kingdoms untaxed |
| Daily allowance | DONE | `/daily`; 24h cooldown, 100–400% streak multiplier, 60% floor |
| Leaderboards | DONE | `/leaderboard nation`, `kingdom`, `individual`; never combined |
| Contract key HTTP API | RETIRED | Returns `410 no_in_game_currency`; contracts pay in Discord |
| **Forge mod** | **TODO** | `forge-mod/` has a README but no source |
| In-game shop item delivery | TODO | Depends on the Forge mod |
| Minecraft account linking | NOT PLANNED | Deliberately avoided; keys are the credential |

The contract payout flow is complete: an approved contract pays nothing
automatically — the recipient claims each eligible tranche, and the credits
land in the contract's claim account (the party by default). One-off legacy
rows that were charged under the old model can claim their whole budget back.
The Forge mod is the remaining major gap.

---

## 3. Architecture

```
Discord users
     |
     v
bot.py (discord.py)  ──> cogs/*.py  ──> database.py ──> royal_bank.db (SQLite, WAL)
                              |
                              ├── Workers' Union treasury (union_account) funds contracts
                              └── Fishing Guild (fishing_guild_account) pays /fish catches
     |                                      ^
     |                                      |
     └── api.py (aiohttp, bearer token) ────┘
                    ^
                    |
        Minecraft Forge mod (planned)
```

* The bot owns the database. The mod never touches SQLite.
* The mod talks to `api.py` over HTTP using `API_TOKEN`.
* Shop and contract keys are single-use credentials; redeeming one marks it
  used atomically so two players cannot spend the same key.

---

## 4. Running the bot

Requirements: Python 3.11+ (3.13 is used in development).

```bash
pip install -r requirements.txt
cp .env.example .env   # if an example exists; otherwise create .env
python bot.py
```

The process entry point is `bot.py`. On startup it connects to SQLite, runs
schema migrations, seeds the shop catalog, loads every cog and syncs slash
commands.

### Environment variables

Set these in the hosting panel or in `.env`. Values are never committed.

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `GUILD_ID` | Guild used for fast slash-command sync (optional) |
| `DB_PATH` | SQLite file path (default `royal_bank.db`) |
| `LOG_LEVEL` | Python logging level |
| `AI_API_KEY` | Key for the Gilbert chat endpoint |
| `AI_API_URL` | OpenAI-compatible base URL |
| `AI_MODEL` | Model name |
| `AI_TIMEOUT` | Request timeout in seconds |
| `AI_TEMPERATURE` | Sampling temperature |
| `AI_MAX_TOKENS` | Reply token limit |
| `API_TOKEN` | Shared bearer token for the Forge mod |
| `API_HOST` | HTTP bind address |
| `API_PORT` | HTTP port |
| `TURSO_DATABASE_URL` | Turso/libSQL URL (`libsql://...`) for the daily mirror |
| `TURSO_AUTH_TOKEN` | Turso token for the daily mirror |
| `TURSO_SYNC_ENABLED` | `false` turns the daily mirror off (default on) |
| `TURSO_SYNC_BATCH_SIZE` | Statements per Turso pipeline request (default 100) |
| `TURSO_SYNC_PAGE_SIZE` | Rows read from SQLite per page (default 500) |
| `TURSO_SYNC_EXCLUDE_TABLES` | Comma-separated tables the mirror must skip |
| `TURSO_TIMEOUT` | Turso request timeout in seconds (default 60) |
| `DAILY_TASK_ENABLED` | `false` turns the whole daily job off |
| `DAILY_TASK_HOUR_UTC` | UTC hour for the once-a-day run (default 6) |
| `DAILY_TASK_INTERVAL_SECONDS` | Use a fixed interval instead of a daily hour |
| `DAILY_GUIDE_ENABLED` | `false` turns the AI guide off (default on) |
| `DAILY_GUIDE_CHANNEL_ID` | Channel that receives the daily guide |
| `DAILY_GUIDE_PATH` | Also write the guide to this Markdown file |
| `DAILY_GUIDE_HOURS` | Transaction window the guide summarises (default 24) |
| `DAILY_GUIDE_MAX_TX` | Maximum transactions fed to the model (default 60) |
| `DAILY_GUIDE_MAX_CHARS` | Maximum guide length (default 3800) |
| `DAILY_GUIDE_LANGUAGE` | Language the guide is written in (default English) |

### Database

SQLite in WAL mode. `royal_bank.db-wal` and `royal_bank.db-shm` are part of the
live database; copy all three together when taking a backup. Migrations are
additive (`ALTER TABLE ... ADD COLUMN`) and run on every startup.

Tables:

| Table | Holds |
|---|---|
| `nations` | Nation identity, leader, worker/soldier stats, balance, kingdom link |
| `kingdoms` | Kingdom identity, leader and treasury balance |
| `transactions` | Append-only audit log of every money movement and stat change |
| `settings` | Per-guild channel IDs and payout/contract cycle state |
| `pending_draws` | Nation-to-kingdom withdrawal requests awaiting approval |
| `pending_creations` | Nation/kingdom petitions awaiting admin approval |
| `catalog_items` | Royal Shop catalog and current prices |
| `kits` | Player-created kits and their publication state |
| `kit_items` | Contents of each kit |
| `orders` | Shop/kit orders, buyer, totals and redeem state |
| `order_items` | Line items and quantities for each order |
| `banned_items` | Catalog keys that may not be sold |
| `kit_cooldowns` | Last kit-creation time per player |
| `contracts` | Faction contract cases: owner, funding faction, **proposal**, budget and tranches |
| `union_account` | The Workers' Union treasury (one row per guild) |
| `faction_members` | One allegiance row per nation/kingdom (`union` or `crown`), with Crown status and joined date |
| `union_members` | Legacy Union mirror, migrated into `faction_members` at startup |
| `union_commits` | Admin-approved standing commitments to donate whole balances |
| `crown_account` | The Crown treasury (one row per guild) that receives tax/donations and pays suppliers |
| `crown_contracts` | Legacy Crown supplier contracts: item, quantity, locked value, delivered progress, status and deadline |
| `crown_sales` | Per-account/per-item supplier history: units sold, demand, last cycle and supplier status |
| `faction_donations` | Credit donations and Crown item deliveries for Albert's record |
| `albert_memory` | Legacy per-account NPC conversation memory (`npc` = `albert` or `gilbert`; the audience command is retired) |
| `fishing_guild_account` | Fishing Guild house balance (one row per guild) that funds `/fish` |
| `contract_cycles` | Natural/manual contract cycle log |
| `case_counters` | Legacy per-guild, per-tag sequence for case IDs (`UN-TAG-001`) |
| `case_counters_prefix` | Per-prefix (`UN`/`CR`) and per-tag case sequence; CR and UN start at 001 independently |

---

### Daily sync (Turso mirror + AI guide)

`daily_sync.py` runs two jobs once a day, at `DAILY_TASK_HOUR_UTC` (default
06:00 UTC). Both halves are independent and env-gated, so the bot behaves
exactly as before until they are configured.

1. **Turso mirror.** Every row of every table — including the append-only
   `transactions` ledger — is copied into the Turso/libSQL database named by
   `TURSO_DATABASE_URL`. The remote schema is created from the local
   `sqlite_master` DDL and rows are upserted with `INSERT OR REPLACE`, so a run
   only brings the mirror up to date (it is a backup/reader copy, never the
   bot's primary store). Requires both `TURSO_DATABASE_URL` and
   `TURSO_AUTH_TOKEN`; with either missing the mirror is skipped with a log line.
2. **AI guide.** A compact digest (table row counts, top treasuries, the four
   house accounts and the last `DAILY_GUIDE_HOURS` of transactions) is sent to
   the configured OpenAI-compatible model (the same `AI_*` settings Gilbert
   uses) to write a plain-language explanation/guide. It is posted to
   `DAILY_GUIDE_CHANNEL_ID` (if set), written to `DAILY_GUIDE_PATH` (if set) and
   stored in a `daily_guides` table in the mirror. If the model is unavailable a
   deterministic fallback is used, so a guide is always produced.

Nothing is hard-bound: every URL, token, hour, channel, path and size is an
environment variable (see the table above), so any of them can be changed from
the hosting panel without editing code.

---


## 5. File map

Every source file and what it is for. Sub-packages have their own README.

### Root

| File | Purpose |
|---|---|
| `bot.py` | Entry point; startup, database connect, cog loading, command sync, signal handling |
| `api.py` | aiohttp redemption API used by the Forge mod (shop keys). Contract endpoints return `410` |
| `daily_sync.py` | Once-a-day job: mirror every table/row (incl. `transactions`) into Turso/libSQL and write an AI "daily guide". Everything (URL, token, schedule, channel, path, AI) is env-driven |
| `config.py` | Fixed economic constants: payout rates, discounts, fees, contract caps |
| `contract_limits.py` | Pure helper for the contract leverage ladder (`max_budget_for_funds`, `budget_allowed`, `limit_explanation`) |
| `database.py` | Async SQLite layer: schema, migrations, queries, transactions |
| `factions.py` | Faction membership/switch rules, Crown tax, supplier contracts, demand decay, deliveries and Albert's report data (no Discord) |
| `albert_prompt.py` | Albert's persona, factual record briefing and deterministic fallback (no Discord) |
| `account_transfers.py` | One atomic credit-transfer core for every economy direction (individual ↔ nation ↔ kingdom), with permission, cap, ledger and Union-commitment hooks |
| `payouts.py` | Pure payout formulas for nations and kingdoms |
| `daily.py` | Pure `/daily` streak and multiplier maths (no Discord, no database) |
| `coinflip.py` | Pure `/individual coinflip` odds, stake guards and payout maths (no Discord, no database) |
| `allin.py` | Pure `/allin` odds, stake guards and payout maths (no Discord, no database) |
| `blackjack.py` | Pure blackjack: shoe, hand values, naturals, dealer policy, settlement (no Discord, no database) |
| `poker.py` | Pure Texas Hold'em engine: card model, five/seven-card evaluator, side-pot `distribute()`, blinds, betting rounds and bot policy (no Discord, no database) |
| `casino.py` | The house counterparty: begin/finish a wager so wins are paid by the Casino and losses fund it |
| `fishing.py` | Pure `/fish` catch maths: random fish, 20–100 credit range, cap/house refusal rules (no Discord, no database) |
| `creation.py` | Nation/kingdom petition flow with Approve/Deny buttons, plus `found_account()` (create + bonus + ledger) shared with the admin `add` commands |
| `embeds.py` | Themed embed builders for accounts, payouts and errors |
| `gambling_ui.py` | Spectator-facing gambling cards: coinflip/all-in results, the Casino floor dashboard, the poker table/result cards, and public reaction cues |
| `checks.py` | Reusable admin permission checks |
| `utils.py` | Money formatting, account encoding/resolution, autocompletes |
| `requirements.txt` | Python dependencies |
| `Procfile` | Process definition for the hosting panel |
| `.env` | Local secrets (never commit) |
| `.gitignore` | Ignores secrets, database files and caches |
| `royal_bank.db` | Live SQLite database (not in git) |
| `README.md` | This document |
| `BUG-FIXES.md` | Running catalogue of every defect found, area by area |
| `checks/` | Standalone verification suite; see below. Shipped with the app, not imported by the bot |

### `cogs/` — Discord command groups

| File | Purpose |
|---|---|
| `cogs/bank.py` | Royal Bank: balance, deposit, withdraw, setbalance, unified transfer, kingdom draw |
| `cogs/transfers.py` | The top-level `/transfer` command over every economy account direction, plus the `/give` treasury-to-member grant |
| `cogs/nation.py` | Nation create/delete/setleader/stats/info/list |
| `cogs/kingdom.py` | Kingdom create/delete/setleader/addnation/removenation/info/list |
| `cogs/payout.py` | Payout previews, `/payout cash`, `/payout default`, `/payout trigger` and the 14-day job |
| `cogs/decree.py` | Channel configuration, pending petitions, ratebook, cycle info |
| `cogs/shop.py` | Royal Shop browse/item/buy/orders/order/ban/unban/setprice/markredeemed |
| `cogs/kits.py` | Player kits: create/list/view/buy/setchannel/resetcooldown |
| `cogs/ledger.py` | Ledger view/all/export |
| `cogs/contract.py` | Gilbert and Albert negotiations, contract lifecycle, approvals and tranche claims |
| `cogs/union.py` | Union treasury, membership, donations and admin funding |
| `cogs/factions.py` | `/faction` — the single command for join/leave, contract, commitment/uncommit, donate, deliver, status, contracts, cancel, fund, withdraw, members and setchannel |
| `cogs/individual.py` | Personal wallets, `/daily`, transfers, coinflip, `/allin` and `/individual delete` |
| `cogs/games.py` | `/blackjack` and `/poker` tables, plus the `/casino` house account commands |
| `cogs/fishing.py` | `/fish` and the `/fishingguild` house account commands |
| `cogs/leaderboard.py` | Separate nation, kingdom and individual rankings |
| `cogs/__init__.py` | Package marker |
| `cogs/README.md` | Per-cog notes and command reference |

### `shop/` — Royal Shop internals

| File | Purpose |
|---|---|
| `shop/catalog_data.py` | Static seed catalog, bundles and hard-banned keys |
| `shop/pricing.py` | Pure pricing, discounts, shulker-fee and stack math |
| `shop/checkout.py` | Validates and writes an order, debits the treasury atomically |
| `shop/embeds.py` | Shared order embed |
| `shop/autocomplete.py` | Catalog item autocomplete |
| `shop/seed.py` | Idempotent catalog seeding at startup |
| `shop/__init__.py` | Package marker |
| `shop/README.md` | Shop rules, key formats and pricing notes |

### `gilbert/` — Workers' Union AI

| File | Purpose |
|---|---|
| `gilbert/ai.py` | OpenAI-compatible async chat client |
| `gilbert/prompt.py` | Gilbert persona, tiers, hard rules and per-contract context |
| `gilbert/__init__.py` | Package exports |
| `gilbert/README.md` | Persona, prompt and model configuration notes |

### `checks/` — verification suite

| File | Purpose |
|---|---|
| `checks/run_all.py` | Runs every check below and prints a PASS/FAIL table; exits non-zero on failure |
| `checks/check_logic.py` | Pure math: payout formulas, pricing, discounts, amount parsing, tax label |
| `checks/check_database.py` | Schema, migrations, guarded one-shot flips, balance/ledger reconciliation, account-delete burn logging |
| `checks/check_transactions.py` | Per-task transaction isolation: commit vs rollback, nesting, no leakage |
| `checks/check_contracts.py` | Contract lifecycle: rejection cannot be undone by close **or reopen**, claims re-read status, closed contracts can still release, cross-guild targets refused, deleted destinations cannot lose credits, global case IDs, atomic cycle quota, approval re-reads balances |
| `checks/check_transfers.py` | `/individual transfer` and `/individual pay` driven through the real cog methods |
| `checks/check_full_transfers.py` | All nine transfer directions, concurrent debits (exactly ten of twenty succeed), source permission, admin override, cap/overdraw refusals, cross-guild refusal, ledger nets and the Union-commitment hook |
| `checks/check_bank.py` | `/bank setbalance` fresh delta and reply-after-commit, draw membership/status re-checks, one-shot creation status, DM `/decree info` guard |
| `checks/check_tax_cycle.py` | 5% net worth tax inside `Payout._run_cycle()` on Cash and Default runs, stale-list/recreated-wallet tax base, ledger both sides, disable switch, Cash leaving the schedule/contract cycle alone, the Cash/Default/Trigger command paths end to end, and two simultaneous natural runs paying/advancing once |
| `checks/check_factions.py` | Faction switching and Union-commit cleanup, open Crown membership, record-based contract judgment, Crown tax/grace, contract caps and requested-budget ceilings, partial/full delivery, demand decay, expiry, short-treasury rollback, Albert context and command callbacks |
| `checks/check_coinflip.py` | Coinflip odds (exactly 50% win over all 10,000 rolls), zero house edge, stake guards, cap refusal, ledger |
| `checks/check_allin.py` | All-in odds for every number 2-100 (exhausted), flat 10/11 return, n=1 refusal, cap/balance refusals, duplicate-confirmation refusal, ledger |
| `checks/check_casino.py` | Blackjack rules (including multi-ace soft hands) and every settlement branch, house solvency, conservation over 200 wagers, restart refund, wallet-deletion lifecycle, poker command/table/final-stack settlement, public posts |
| `checks/check_fishing.py` | `/fish` range/refusals, Fishing Guild guarded balance, end-to-end catch and ledger, cap/short-guild refusals, admin funding |
| `checks/check_poker.py` | Poker evaluator (category order, wheel, best five of seven), side pots, uncalled-bet refunds, short all-ins not reopening raises, odd chips, blinds and the big blind's option, 400 seeded hands with chips conserved |
| `checks/check_gilbert.py` | Gilbert config normalisation (DeepSeek model alias/base URL), prompt tier/context facts, disabled-without-key |
| `checks/check_cogs.py` | Concurrent `/daily` claims plus import and cog-load smoke test (112 commands, 16 cogs) |
| `checks/check_creation_race.py` | Concurrent creation must produce exactly one account and one bonus row; approval and direct-add name races close cleanly |
| `checks/check_runner.py` | The runner rejects an exception or bare exit with no summary instead of reporting a false pass |

Run everything with:

```bash
python3 checks/run_all.py
```

Every check builds its own throwaway SQLite files in a temp directory, so they
never touch the live database and need no Discord login or network. They do need
`discord.py`, `aiosqlite` and `python-dotenv` installed.

### `forge-mod/` — Minecraft Forge companion

| Path | Purpose |
|---|---|
| `forge-mod/README.md` | Planned mod scope, API contract and current status |
| `forge-mod/src/main/...` | Placeholder; no Java sources yet |

---

## 6. Command reference

All commands are slash commands. `[Admin]` means Discord administrator
permission; `[Union Admin]` means the configured Union Admin role or an admin.

| Group | Commands |
|---|---|
| `/nation` | `request`, `add` [Admin], `rename` [Admin], `delete` [Admin], `setleader` [Admin], `stats` [Admin], `info`, `list` |
| `/kingdom` | `request`, `add` [Admin], `rename` [Admin], `delete` [Admin], `setleader` [Admin], `addnation` [Admin], `removenation` [Admin], `info`, `list` |
| `/bank` | `balance`, `deposit` [Admin], `withdraw` [Admin], `setbalance` [Admin], `transfer`, `draw` |
| `/transfer` | `source`, `destination`, `amount` — move credits between individuals, nations and kingdoms in any direction |
| `/give` | `source`, `member`, `amount` — a nation/kingdom leader pays a member's individual wallet from the treasury |
| `/payout` | `nation`, `kingdom`, `all`, `cash` [Admin], `default` [Admin], `trigger` [Admin] |
| `/decree` | `setchannel` [Admin], `pending` [Admin], `rates`, `info` |
| `/shop` | `browse`, `item`, `buy`, `orders`, `order`, `ban` [Admin], `unban` [Admin], `setprice` [Admin], `markredeemed` [Admin] |
| `/kit` | `create`, `list`, `view`, `buy`, `setchannel` [Admin], `resetcooldown` [Admin] |
| `/ledger` | `view`, `all` [Admin], `export` [Admin] |
| `/contract` | `close`, `status`, `list`, `claim`, `approve` [Union Admin], `release` [Union Admin], `reject` [Union Admin], `reopen` [Union Admin], `setchannel` [Admin], `setboard` [Admin], `setrole` [Admin] |
| `/faction` | `join`, `leave`, `contract`, `commitment`, `uncommit`, `donate`, `deliver`, `status`, `contracts`, `cancel`, `fund`, `withdraw`, `members`, `setchannel` — each action shows only its own options |
| `/individual` | `balance`, `transfer`, `pay`, `coinflip`, `delete`, `give` [Admin] |
| `/allin` | `number` (2–100) — stakes the whole individual balance on one draw |
| `/blackjack` | `amount` — one hand against the Casino, hit or stand |
| `/poker` | `amount` — four-seat Texas Hold'em against three house bots |
| `/fish` | _no options_ — one random 20–100 credit catch paid by the Fishing Guild |
| `/fishingguild` | `balance`, `fund` [Admin], `withdraw` [Admin] |
| `/casino` | `balance`, `fund` [Admin], `withdraw` [Admin] |
| `/daily` | _no options_ — one personal claim per 24 hours |
| `/leaderboard` | `nation`, `kingdom`, `individual` |

`/nation add` and `/kingdom add` are the admin-only shortcut past the petition
flow: they found the account at once and credit the same creation bonus
(30,000 / 50,000 credits) in one transaction with their ledger row. A pending
petition for the same name must be decided first.

`/nation rename` and `/kingdom rename` are admin-only. They change only the
name: the account id, balance, leader, member nations and Union membership stay
put, and open contracts are refreshed to the new name. Ledger rows keep the
name they were written under, so history is never rewritten; the rename itself
is logged as `nation_rename` / `kingdom_rename` with `Renamed from <old>`.

`/nation delete` and `/kingdom delete` are admin-only. The remaining treasury
balance is burned and recorded as a negative `nation_delete` /
`kingdom_delete` ledger row with `balance_after = 0`, so the books still
reconcile after a disband; the reply states the burned amount.

Money-moving commands (`/transfer`, `/give`, `/bank transfer`, `/bank draw`,
`/bank withdraw`, `/individual transfer`, `/individual pay`) accept a plain
number **or** the words `all`, `half` and `quarter`, which resolve against the
paying account's balance at the moment the command runs. Fractions round down,
so `half` of an odd balance leaves the extra credit behind.

`/faction contract` takes `faction`, `account`, `proposal` and `budget`, and its
`account` autocomplete lists both `🏛️ Nation · Name` and `👑 Kingdom · Name`.
`proposal` is one free-text message — what you will provide, how much, and
when/where — and it is the only descriptive field: there is no item, quantity or
project option. It opens a **direct** negotiation with that faction's
representative — Gilbert for the Workers' Union, Albert for The Crown.
**Membership is never required** and nothing is rolled: the account's live
numbers are already in the representative's briefing. A Union proposal is
public work; a Crown proposal is a supply offer whose goods an officer collects
in-game. A nation may hold **one** contract per natural payout cycle; a kingdom
may hold **three**, counted separately per faction.
The requested faction's treasury must cover the budget, so `/contract approve`
refuses a budget its treasury cannot pay.

`/individual balance` shows your wallet (or another member's), `/individual give`
is the admin-only faucet and respects the 100M cap, and `/individual delete`
erases the wallet and its ledger rows after a confirmation; members may erase
their own data and administrators may erase anyone's. `/individual give` is not
the same as `/give`: the former creates credits from nothing and is admin-only;
`/give` pays out of a nation or kingdom treasury.

`/transfer` is the unified account-to-account command. Its source and
destination autocompletes list `🏛️ Nation · Name`, `👑 Kingdom · Name` and
`👤 Wallet owner`, so it covers every direction between individuals, nations and
kingdoms: individual → individual, individual → treasury, treasury →
individual, and nation ↔ kingdom (including nation → nation and kingdom →
kingdom). `/bank transfer` uses the same core and the same account list.
`/give source:<nation|kingdom> member:<@user> amount:<credits>` is the
treasury → member shortcut: the treasury is the only accepted source, the member
wallet is the destination, and the ledger rows are `treasury_give`.

The sender is debited and the recipient credited inside **one** transaction, so
a refused transfer changes nothing. You may only move credits out of a treasury
you lead or your own wallet; administrators may move any account. A transfer
into an individual wallet is **refused, not trimmed**, when the recipient would
pass the 100M cap — trimming would delete credits the sender already paid — and
the reply states the room that is left. Paying a treasury that has an active
Union commitment triggers the usual whole-balance donation inside the same
transaction. Both sides of every transfer are ledgered.

`/individual transfer` (member to member) and `/individual pay` (member to
treasury) remain as member-facing shortcuts with their own reply wording. All
four commands use the same `account_transfers` core, so permission, cap,
ledger and Union rules cannot drift between them. `/individual transfer` can
create a missing destination wallet on first receipt; the `/transfer`
autocomplete lists existing wallets, so a new recipient should run `/daily`
first.

`/payout cash` pays the nation and kingdom allowances immediately. It is a
cash-only operation: it never rewrites `last_cycle_at`/`next_cycle_at`, never
advances the contract cycle, and never inserts a contract-cycle history row.
`/payout default` is the full natural cycle in one execution: cash allowances,
Union dues and commitments, the individual net-worth tax, the contract-cycle
advance and the next 14-day timestamp. It refuses when the next cycle is not due
unless you pass `force: True`. `/payout trigger days:N` does not pay: it queues
the natural cycle now (`days:0`, picked up by the 10-minute task) or schedules it
`N` days out, leaving the cash path untouched.

Every payout run — the Default Cycle (the 14-day job or `/payout default`) and
the Cash Cycle (`/payout cash`) — charges each individual a **5% net worth tax**
on the whole wallet balance, floored to whole credits, so 19 credits pays 0. The
tax is debited and logged as `individual_tax` with the wallet and the destination
both on the ledger. Because the Cash Cycle taxes too, an admin cannot use
`/payout cash` to distribute credits without the same charge applying. Until the
wallet is worth 20 credits, nothing is charged. Nations and kingdoms are never
taxed by this rule and have no balance cap.

`/individual coinflip` stakes credits on a coin. You call **heads** or **tails**
and stake an amount, `all`, `half` or `quarter`. The flip is an even **50/50**
whichever face you called, and a win returns **2x** the stake (stake back plus
equal winnings). The call therefore decides which face is shown, never your
chance of winning.

The stake leaves your wallet and the winnings return in one transaction, so a
failure mid-bet cannot take credits without settling the bet. Stakes are bounded
at 100 credits minimum and 10,000,000 maximum, and a stake is refused when a win
would push the wallet past the 100M cap. At 50% and 2x the long-run drift is
zero: the Casino funds each win from its own balance, so variance moves the house
stack but the game is a fair pass-through. See `coinflip.py` for the arithmetic
and `config.COINFLIP_WIN_BP` to adjust it.

`/allin` is the high-variance counterpart. You name a number `n` from **2 to
100** and stake **your entire individual balance** on one draw. A win pays `n`
times the stake; the chance of winning is **1 in (1.1 x n)**, so `n: 5` pays 5x
at 1 in 5.5 and `n: 100` pays 100x at 1 in 110. A loss takes the whole wallet.

The important consequence of the 1.1x rule is that **your expected return does
not depend on the number you pick**: `n x 1/(1.1n) = 1/1.1 = 10/11` for every
value of `n`. Every number therefore carries the same **9.09%** house edge, and
choosing 100 instead of 2 buys variance, not a better or worse bet. The draw is
exact integer arithmetic (`10` winning outcomes out of `11n`), not a float, and
`checks/check_allin.py` exhausts every outcome for all 99 playable numbers.

Because a miss empties the wallet, `/allin` asks for confirmation on a card
showing the stake, the payout and the chance before it draws. The card is posted
in the channel so the draw is watched, but only the caller can press the button.
`n: 1` is rejected: a 1x payout returns only the stake, so it would risk
everything for no profit. A bet is also refused when a win would breach the
100M individual cap, since the payout - not the stake - is what the cap limits.
See `allin.py` and `config.ALLIN_HARDNESS_NUMERATOR`.

`/blackjack amount:` deals one hand in the channel against the Casino. You stake
any amount between 100 and 10,000,000 (or `all`, `half`, `quarter`), get two
cards against the dealer's two, and press **Hit** or **Stand**. Number cards
count face value, court cards ten, and an ace counts 11 unless that would bust
the hand. A **natural** 21 on the first two cards pays **3:2**; any other win pays
even money; equal totals push and return the stake exactly. The dealer hits to 16
and **stands on all 17s, including soft 17**. There is no split, double or
insurance.

The stake is taken when the cards are dealt and held by the house while you
decide, so it cannot be spent mid-hand and leave a loss unpaid. A hand left
alone for two minutes is settled at stand, and a hand cut short by a restart is
**refunded in full** at startup rather than keeping the stake.

`/poker amount:` seats you at a four-seat Texas Hold'em table with three house
bots. Your buy-in (200–10,000,000, or `all` / `half` / `quarter`) becomes your
starting stack; the bots start with the same stack, so the table holds four
times the buy-in. The Casino must be able to cover that whole table before the
deal. You see your own hole cards; the community cards, pot, stacks and table
talk are public for spectators. Press **Fold**, **Check/Call**, **Raise** (enter
the total raise-to amount in the modal) or **All-In**; the bots answer and the
next street is dealt automatically. If you leave the table idle for three
minutes, you are folded and the hand settles. At the end you are paid your
**remaining chip stack**, not merely a win/loss binary, and a restart returns
the buy-in because the table itself lives in memory.

### The Casino

All four games settle against the **Casino**, one house account per server, and
the house is a real ledger account rather than a bookkeeping fiction. A losing
bet credits the Casino; a winning bet is paid out of it; and a bet is refused
when the payout would exceed the house balance. Gambling therefore **moves**
credits between the player and the house instead of creating them: the sum of
every wallet plus the Casino is unchanged by any number of bets, which
`checks/check_casino.py` asserts over 200 consecutive wagers.

This replaces the earlier model, in which a loss destroyed the stake and a win
minted the profit. Those odds made each game a net sink, but each individual
payout still created credits from nothing and each loss burned them, so the
money supply drifted with luck. With a house account the supply changes only
when an administrator runs `/casino fund` or `/casino withdraw`. The house edge
is now **earned by the Casino** instead of being destroyed, and the Casino can
run dry: `/casino balance` shows what it can pay, and a wager whose worst-case
payout exceeds it is refused with the shortfall named.

All gambling is posted **in the channel**, never as an ephemeral reply, so the
stake, the draw and both balances are visible to everyone who can read it. Only
the buttons are restricted to the player whose hand it is.

`/fish` is the Fishing Guild's public catch. It rolls one random fish worth
**20–100 credits** and pays it into the caller's individual wallet from the
Fishing Guild house balance. The catch is posted in the channel, names the fish
and shows both the wallet and the guild balance. The catch is refused before any
money moves if it would cross the 100M individual cap or the guild cannot cover
it; the guild starts empty and is funded only with `/fishingguild fund` (admin),
drained only with `/fishingguild withdraw` (admin), and shown by
`/fishingguild balance`. It is a credit transfer, not a faucet: a catch can
never create credits.

`/daily` pays into the caller's individual wallet only. It is one claim per 24
hours, the multiplier starts at 100%, adds 10 points for each consecutive claim
(cap 400% = 8,000 credits) and loses 20 points for each missed day down to a 60%
floor (1,200 credits). A payout that would cross the 100M cap is trimmed; the
excess is not stored anywhere.

`/leaderboard` has three separate subcommands, one per economy type. They read
three different tables and are never combined into a single ranking.

`/faction join` and `/faction leave` are for the account's leader
(or an admin). `/faction donate` moves credits from a nation or kingdom
treasury into the current faction; `/faction fund` is the admin-only way
to add credits. Those paths, plus dues and approved commitments, are the only
inflows.

`/faction commitment` asks an administrator to turn on a
standing commitment for an account. The request goes to the channel set with
`/faction setchannel`; only an administrator can press Approve. Approval
immediately donates the balance the account already holds, and from then on
**every credit** to that account is followed by a donation of its **whole
balance** to the Union. The account leader or an admin can stop it with
`/faction uncommit`. Dues are still charged first on a
payout, so the Union receives both the dues and the remaining balance.

### Poker — the public high table

`/poker amount:` opens a **four-seat Texas Hold'em table** against three house
bots with rotating **Minecraft-themed names** (`poker_names.py` samples an
AI-generated pool using Gilbert's `AI_API_KEY`, with a curated fallback pool).
The buy-in is held by the Casino while the hand is played, and the player's
**final chip stack** is what comes back at settlement. The table is posted in the
channel, so every street has an audience.

What the engine does:

* **Cards and the evaluator.** `new_deck()` builds a 52-card deck whose suits are
  the single trailing character of each card, so `card_value()` and the flush
  test read `card[-1]`. `score_five()` returns a sortable tuple for exactly five
  cards, `hand_rank()` takes the best five of five to seven, and `describe()`
  renders the result for an embed.
* **Side pots.** `distribute(contributions, ranks, button=..., seat_count=...)`
  builds one pot per distinct contribution level, refuses to pay a seat with no
  rank (a folded or losing hand), and sends an odd chip to the first winner left
  of the button.
* **Table state.** `Holdem` seats four players — seat 0 is the human
  (`seat.is_player`) and the others are house bots. Each seat starts with the
  player's stake, so the table holds `4 x stake`; `max_gross(stake)` returns
  exactly that, which is what the Casino must be able to cover before a table
  opens.
* **Blinds.** `blinds_for(stake)` derives the small blind from
  `config.POKER_BLINDS_DIVISOR` (stake // 50) and doubles it for the big blind.
  The button, small blind and big blind seats are fixed at hand start; the seat
  left of the big blind acts first preflop, then action moves clockwise. The
  blinds keep their **option**: after the table limps, the big blind owes
  nothing and may check or raise.
* **Betting rounds.** `_begin_street()` opens flop/turn/river: it resets
  `current_bet` and every seat's `committed`, deals the board and starts action
  left of the button. Preflop is deliberately *not* opened with it — see
  `_start_preflop()` and `BUG-FIXES.md`.
* **Bots.** `bot_choice()` weighs `strength()` against the pot odds with
  `config.POKER_FOLD_MARGIN`, raises for value above
  `config.POKER_RAISE_STRENGTH` and bluffs at `config.POKER_BLUFF_RATE`.

The Discord layer lives in `cogs/games.py`:

* `/poker` checks the 200–10M buy-in, the player's wallet and the 100M cap, and
  requires the Casino to cover `4 x stake` before any credits move.
* The buy-in is taken with `casino.begin_wager()` and a `poker_hands` row is
  written in the same transaction. A restart cannot rebuild the in-memory
  table, so `bot._refund_abandoned_poker_hands()` closes the row and returns the
  buy-in at startup.
* The table offers **Fold**, **Check/Call**, **Raise** (a modal asking for the
  total raise-to amount) and **All-In**. Only the player can press them;
  spectators see every card on the board, the player's hole cards, the pot, the
  stacks and the table talk.
* The bots act immediately after the player's action, and the table is updated
  with the new street, stacks and log. A hand with no action for
  `POKER_TIMEOUT_SECONDS` (180s) folds the player and settles automatically.
* `settle_table()` closes `poker_hands` once, then uses
  `casino.finish_wager(..., payout_override=final_stack)` so even a losing hand
  returns the chips still in the player's stack. The result card reveals every
  hand, names the winners, and seeds spectator reactions.

Prove the engine and the table with:

```bash
python3 checks/check_poker.py
python3 checks/check_casino.py
```

---

## 7. Economy rules that must not be broken

* Balances are integer **credits**. Never use floats for money.
* Three **economy** account types exist and never mix in a single balance:
  **nation**, **kingdom** and **individual**. Treasuries (nation/kingdom) are
  spent by the shop, transfers and contracts; individuals are personal wallets
  fed by `/daily` and admin grants. Contracts and the shop never pay into or out
  of an individual **directly**; a treasury pays a wallet only through an
  explicit `/transfer` (or `/bank transfer`), never through a contract or shop
  checkout. Individuals move credits with `/individual transfer`,
  `/individual pay` and the unified `/transfer`. The Workers' Union, the Casino
  and the Fishing Guild are separate **house** accounts, not economy types.
* Individual balances are hard-capped at ``INDIVIDUAL_MAX_CREDITS``
  (100,000,000). A faucet credit larger than the remaining room is trimmed,
  never banked, and an over-cap balance is a bug. A **transfer** into a wallet is
  refused instead (`credit_individual_strict` raises ``CapExceeded``), because
  trimming would destroy credits the sender has already been debited for.
* **Nations and kingdoms have no cap.** Never apply ``INDIVIDUAL_MAX_CREDITS``
  to a treasury.
* **Every credit transfer uses one core.** `account_transfers.transfer_funds()`
  is the only transfer implementation: it re-reads both accounts under the
  transaction lock, requires the actor to lead the source treasury or own the
  source wallet (admins override), debits with the balance guard, credits an
  individual with `credit_individual_strict` (cap refusal, never a trim), logs
  both sides and runs the destination treasury's Union commitment hook. It
  supports all nine pairs between nation, kingdom and individual. `/transfer`,
  `/give`, `/bank transfer`, `/individual transfer` and `/individual pay` all
  call it; never add a second transfer SQL path.
* **Cash and Default cycles are separate.** `/payout cash` pays allowances and
  the individual tax but must not touch `last_cycle_at`/`next_cycle_at` or the
  contract cycle. `/payout default` (and the 14-day job) runs the full natural
  cycle: allowances, dues/commitments, the tax, `advance_contract_cycle()` and
  the next timestamp. `/payout trigger` only queues/schedules the natural cycle;
  it pays nothing. `_run_cycle(natural=False)` is the cash path and
  `natural=True` is the default path. The Default path repeats the due-check
  **inside** `_cycle_lock` (`require_due=True`), so two simultaneous Default
  runs pay one cycle and advance `contract_cycle` once; only `force:true`
  deliberately bypasses that guard.
* **A nation or kingdom belongs to at most one faction.** `faction_members` has
  one row per account (`PRIMARY KEY (guild_id, account_type, account_id)`) and
  the value is either `union` or `crown`. `/faction join` and the union
  migration all go through `factions.join_faction()`: switching cancels the old
  side's standing Union commitment and active Crown contracts in the same
  transaction. The legacy `union_members` table is a mirror, not the authority.
* **The Crown has no soldier minimum.** Any nation or kingdom can join, and
  **contracts never require faction membership**. Soldiers are shown in
  Albert's briefing as standing, but they never gate a request. A kingdom sums
  its member nations for that figure.
* **Crown tax is 100% of the regular-soldier payout line**, not the whole
  allowance: `payouts.regular_soldier_income()` isolates it. It is charged in
  the payout transaction into `crown_account`, after the 30-day grace from
  `faction_members.joined_at`. There is no dues table: the tax is deducted
  immediately, so unpaid Crown dues cannot exist.
* **Legacy Crown supplier contracts buy one catalog item.** No new ones are
  created — `/faction contract` opens a proposal contract for either faction (Union work or Crown supplies) —
  but existing rows still settle. A nation may hold **1** active supplier
  contract and a kingdom **2**; fulfilled, cancelled and expired rows free the
  slot. The total value is capped by the contracting treasury:
  **10M below 15M**, **15M from 15M through 50M**, and **one third above 50M**.
  The Crown must hold the full value (plus every other active contract's
  value) before a contract can be signed.
* **Demand decays per account and item.** Demand starts at 100% and falls by
  **5 percentage points** in each natural cycle in which that account fulfils a
  contract for the same item, floored at **40%**. A signed contract keeps its
  locked value; the next one sees the lower demand. The rate is
  `config.CROWN_DEMAND_DECAY_BP` / `CROWN_DEMAND_FLOOR_BP`.
* **A legacy supplier contract pays only on full delivery.** `/faction deliver`
  fills the oldest active contract of that item first; a partial delivery pays
  nothing, excess is a pure item donation, and a short Crown treasury rolls the
  entire delivery back. Fulfilment records the account as official supplier for
  `CROWN_SUPPLIER_CYCLES` cycles and writes both `crown_supplier` ledger rows.
* **The Crown treasury is a house account, not an economy type.** Only
  `/faction fund` and `/faction withdraw` are admin taps;
  withdraw refuses to take the balance below the sum of active contract values.
  Credit donations from a member are allowed, but item donations are the
  supplier path.
* **Contract requests open a conversation, not a dice roll.** `/faction contract`
  takes `faction`, `account`, a free-text `proposal` and a required `budget`.
  **No faction membership is required** and nothing is randomised. A Union
  proposal is public work and opens Gilbert's negotiation; a Crown proposal is a
  supply offer and opens Albert's, with an officer collecting the goods in-game.
  Both briefings carry the account's live numbers (balance, soldiers, treasury,
  quota, contributions or supply history, tax status) from the first turn, and
  the 25/75 tranches are paid from the requested faction's treasury. The legacy
  Crown supplier-contract rows still deliver and pay through `/faction deliver`.
  `/faction commitment` requests the Union whole-balance commitment.
  `/faction status` shows the ledger.
* Individuals pay a **5% net worth tax on every payout run** (Cash and Default;
  ``config.INDIVIDUAL_TAX_BPS`` = 500), on the whole balance, floored to whole
  credits. Nations and kingdoms are **not** taxed by this rule. The charge runs
  inside the payout transaction so the debit and its ledger rows cannot diverge.
* Individuals receive **no discounts** of any kind (`discount_percent` returns 0).
  The 50% Union discount and the 20% treasury discount are nation/kingdom only.
* `/daily` is the only automatic individual inflow: one claim per rolling 24
  hours, integer credits, multiplier between 60% and 400%.
* **Gambling never creates credits.** Every wager settles against the Casino
  through ``casino.begin_wager()`` / ``casino.finish_wager()``: the stake moves
  into the house, the payout moves out of it, and both sides are logged. The sum
  of all player wallets plus the Casino balance must be identical before and
  after any number of bets. Never pay a win with ``credit_individual()`` alone -
  that mints the credits. ``checks/check_casino.py`` asserts conservation over a
  long run; keep it that way.
* **The Casino only goes up by an admin command.** ``/casino fund`` and
  ``/casino withdraw`` are the only faucet and drain (`casino_fund` /
  `casino_withdraw` ledger rows, admin only). ``db.debit_casino()`` refuses to
  take the balance below zero, and ``casino.cover_problem()`` refuses a wager
  whose worst-case payout the house cannot cover, so a losing streak cannot leave
  a winner unpaid.
* **Fishing never creates credits.** ``/fish`` debits ``fishing_guild_account``
  and credits the individual wallet in one transaction, with a ledger row on both
  sides. The Fishing Guild starts empty and only ``/fishingguild fund`` (admin)
  puts credits in; ``/fishingguild withdraw`` (admin) takes them out. A catch is
  refused before either balance moves when the guild is short or the 100M wallet
  cap would be crossed. ``checks/check_fishing.py`` asserts the conservation and
  refusal rules.
* **Coinflip is a fair pass-through.** ``config.COINFLIP_WIN_BP`` = 5,000 (50%)
  against a 2x payout gives ``coinflip.house_edge_bp()`` = 0. The Casino still
  funds every win from its own balance, so variance moves the house stack but
  the long-run drift is zero. Raising the win rate above 5,000 would make the
  game a faucet; lowering it creates a sink. Odds are decided by the roll alone,
  never by the player's call.
* **Poker pays the final stack.** The buy-in is held like blackjack, and
  ``casino.finish_wager(..., payout_override=final_stack)`` returns every chip
  still on the table even when the hand is a net loss. The house must cover
  ``poker.max_gross(stake)`` (four seats) before the deal, and the
  ``poker_hands`` row is closed in the same transaction as the payout.
* **Blackjack reserves the natural.** ``blackjack.max_gross(stake)`` is the 3:2
  payout and is what the house must cover before a deal, because a natural is
  paid immediately. Lowering ``config.BLACKJACK_BLACKJACK_BP`` under the even
  money return would make the reserve wrong.
* **`/allin` multiplies the wallet, never the stake type.** It always bets the
  whole individual balance, so a loss releases nothing back. A win pays `n`
  times the stake at 1 in `1.1 x n`; the expected return is therefore `1/1.1`
  = `10/11` **for every n**, a flat **9.09%** sink that does not change with the
  number picked. Higher numbers are higher variance, not worse odds. Keep
  `config.ALLIN_HARDNESS_NUMERATOR` above `ALLIN_HARDNESS_DENOMINATOR` or the
  Casino stops earning. `n = 1` is refused because a 1x payout returns only the
  stake, so it risks everything for no profit.
* Deposits and withdrawals are admin-only because they cross the in-game /
  Discord boundary.
* A nation may draw from its kingdom only with the kingdom leader's approval.
* A payout cycle is 14 days. The Cash Cycle does not reset a contract cycle;
  the Default Cycle advances it and schedules the next 14-day run.
* **Contracts never charge the contracting party.** The Workers' Union pays
  from its own treasury: 25% upfront (building) on approval, 75% (maintenance)
  on release. A contract cannot be approved unless the treasury holds the whole
  budget, and a claim cannot be paid unless it still does.
* **The Union treasury is the only source of contract credits.** It grows from
  `/faction fund` (admin), `/faction donate` (nation/kingdom),
  member dues and approved commitments. Never add another path that creates
  Union credits.
* **Union dues** are charged on every payout run (Cash and Default) to member
  accounts only: 1.75% of a nation's **whole balance**, 2% of a kingdom's whole
  balance, integer floor. The rates live in `config.UNION_TAX_BPS` as basis
  points (175 and 200) so credit maths stays integer; `payouts.union_dues()`
  owns the calculation. The base is the balance *after* the payout is credited,
  so a member with 0 credits still pays its rate on the payout itself, and dues
  may legitimately exceed the payout. The credit and its dues share one
  transaction.
* **Union commitments multiply the inflow, never mint it.**
  `/faction commitment` only becomes active after an
  administrator approves it in the channel set by
  `/faction setchannel`. Approval donates the balance the account already holds;
  while active, every credit to the account is followed by
  a donation of its **whole balance** to the Union, inside the same transaction
  as the credit. That includes payouts, contract claims, admin deposits,
  transfers into the account and approved draws. On a payout, dues are charged
  first and the remainder is committed. `Database.commit_balance_to_union()`
  is the only implementation; every new credit path must call it (guarded by
  the actual credit) or the commitment is bypassed. `/bank setbalance` is an
  admin correction, not income, so it does not trigger a donation. Only the
  account leader or an admin can start or stop a commitment; approval and
  cancellation are both ledgered.
* **Union membership** costs nothing but changes pricing: members pay 50% on
  `Basic Building`, `Wood & Masonry` and `Food & Crops` lines, and keep the
  normal treasury discount on everything else. Redstone, combat gear and
  materials (e.g. iron blocks) are excluded from the member rate.
* A **new** contract must be positive and at most **100,000,000 credits**
  (`CONTRACT_MIN_CREDITS` / `CONTRACT_MAX_CREDITS` in `config.py`; the minimum is
  1, effectively no practical floor). Existing low-value contracts are
  grandfathered and remain claimable. The party's treasury sets the ceiling
  (`contract_limits.py`): up to 10,000,000 carried at 3x the treasury, up to
  20,000,000 at 2x, above that at 1x. Ceilings are inclusive.
* A contract may only be approved once. Reopening a funded case is discussion
  only and keeps the original funding.
* Contract quota per natural cycle: **1 per nation, 3 per kingdom**
  (`config.CONTRACTS_PER_CYCLE`).
* **Contract credits are only paid in Discord.** There is no in-game contract
  currency and no contract keys; the API refuses old keys with `410`.
* Each tranche is paid at most once. `claim_contract_tranche` flips the
  `upfront_claimed` / `final_claimed` flag atomically, so a double click or a
  retried button cannot duplicate credits.
* Shop keys are single-use and must be marked atomically.
* There is no Minecraft account linking by design.

---

## 8. Deployment

Before you change money code, read `BUG-FIXES.md`. It records every defect found
so far, what caused it and which check proves the fix, plus the remaining issues
that are deliberate rather than broken.


The bot runs from the SFTP account root. The live files are the same layout as
this project:

```
Procfile
api.py
bot.py
config.py
database.py
...
cogs/
shop/
gilbert/
royal_bank.db
```

Deployment checklist:

1. Run `python3 checks/run_all.py`, `compileall` and `pyflakes`. Do not upload a
   red suite.
2. Check each target file's remote mtime. If it is newer than your pull, someone
   uploaded over you: re-pull before editing.
3. Back up `royal_bank.db`, `royal_bank.db-wal` and `royal_bank.db-shm`.
4. Upload changed files with timestamped backups (`.bak-YYYYMMDD-HHMMSS`) and
   compare SHA-256 of every uploaded file against your local copy.
5. Pull the deployed `.py` tree into an empty directory and re-run
   `checks/run_all.py` against **that** copy. This catches a file you edited but
   forgot to upload.
6. Restart the process from the hosting panel. Code changes do nothing until
   the bot restarts, and schema migrations only run at startup.
7. Check the panel log for `Loaded extension ...` and a successful command sync.
8. After a Union release, check the feature by hand:
   - `/faction status faction:union` shows the treasury, the member count and
     any pending or active commitments;
   - an admin sets the approval channel once per guild with
     `/faction setchannel` — until that is set, `/faction commitment` refuses
     and tells the member an admin must configure it;
   - `/faction commitment account:<own account>` posts
     Approve/Deny in that channel; approving empties the account into the Union
     immediately and `/ledger` shows two `union_commit` rows, with the account's
     balance matching the sum of its transactions;
   - `/faction uncommit` clears the commitment and later
     credits stay put.

Never upload `.env`, the live database or `__pycache__` as part of a code-only
change.

The host also has a `royal-bot/` placeholder directory containing only a
README. The live bot runs from the account root, not from that folder.

---

## 9. Known gaps and next steps

1. **Forge mod (TODO).** The `forge-mod/` tree has no source. It needs a Forge
   version, item delivery, shop key preview/redeem calls and a config for the
   API base URL and token. Contract key routes now answer `410
   no_in_game_currency`; do not resurrect in-game contract money.
2. **Claim board backfill (nice to have).** `/contract setboard` posts new
   boards, but contracts approved before the board existed have no
   `board_message_id`, so they are not listed. `/contract claim` still works
   for them; a small admin "republish board" pass would fix the listing.
3. **Legacy contract rows.** Cases approved under the old "party pays" model
   keep their bytes but are paid back through `/contract claim` (whole budget,
   since the old model charged it in full and keys are now inert). The one live
   legacy case (`UN-COMMUN-001`, 1,000,000 credits) has **no claimed tranches**
   on the live row (`upfront_claimed = 0`, `final_claimed = 0`), so one claim
   pays the **whole 1,000,000** (250,000 upfront + 750,000 completion) from the
   Union to the Commune. The row is already `closed`; a Union Admin can mark it
   completed with `/contract release case_id:UN-COMMUN-001` after the claim.
   Do not assume the old 250,000-claimed figure — it does not match the row.
4. **Tests.** The `checks/` suite is the automated verification: 18 standalone
   scripts run by `python3 checks/run_all.py`, all passing against this tree.
   They cover the pure maths, the database and transaction layer, transfers,
   the per-run tax cycle, factions/Crown contracts, every gambling game
   including poker, fishing and the Fishing Guild, the `/daily` race, the
   creation race, bank/draw guards, the runner's own false-pass guard and a
   cog-load smoke test. They build throwaway
   SQLite files, so they never touch the live database. Keep it that way: a new
   money path should arrive with a check.
5. **Union configuration is live.** `settings.union_commit_channel_id` is set
   on the live database, so `/faction commitment` can post
   approval requests. An admin still needs `/faction setchannel` only if
   the channel is changed.
6. **Poker and fishing are live.** `/poker` is implemented in `cogs/games.py`
   and `/fish` in `cogs/fishing.py`; both are documented in section 6. Future
   work should preserve the held `poker_hands` buy-in, the final-stack payout,
   the public spectator card and the Fishing Guild's zero-sum house settlement.
7. **Audit defects fixed.** The full 2026-10-27 audit defects are fixed and
   covered: per-run tax reads, wallet resurrection, draw membership, contract
   quota/approval races, stale `setbalance` deltas, guarded status writes,
   cross-guild case IDs, poker uncalled bets/short all-ins, the DM settings
   ghost row and the runner false pass. See `BUG-FIXES.md`; the 18-check suite
   exercises each one. A follow-up pass also pinned the concurrent-Default
   double-pay guard, the `setbalance` reply-after-commit rule, the all-in
   confirmation idempotence and multi-ace `is_soft`; `check_factions.py`
   covers the faction/Crown layer end to end.

---

## 10. Contract payout model (read this before touching `cogs/contract.py`)

```
/faction contract faction:<union|crown>  →  opens a direct negotiation with
                                           Gilbert / Albert (negotiating); takes
                                           one free-text `proposal`; no
                                           membership check and no dice roll
/contract approve  →  requires the funding faction's treasury >= budget;
                      status approved, upfront/escrow recorded, claim account
                      set (no credits move, nothing is debited from the party)
/contract claim    →  25% paid from that treasury into the claim account
                      (flag: upfront_claimed, treasury debited in the same
                      transaction)
/contract release  →  status completed, released = escrow (unlocks the 75%);
                      Union: final inspection; Crown: goods collected by an
                      officer
/contract claim    →  75% paid from that treasury (flag: final_claimed)
/contract reject   →  status rejected, further claims blocked
```

* `contracts.faction` (`union` or `crown`) names the paying treasury. Rows that
  predate the column default to `union`.
* Case IDs are faction-prefixed: `UN-<TAG>-<NNN>` for a Workers' Union case and
  `CR-<TAG>-<NNN>` for a Crown case. `Database.next_case_number()` allocates
  **per prefix**, so the first Crown case for a tag is `CR-<TAG>-001` even when
  the Union already holds `UN-<TAG>-001..NNN`; Union numbering continues from
  the legacy `case_counters` / `case_counters_global` floors.
* `claim_account_type` / `claim_account_id` default to the contracting party.
  `approve` can name a different nation or kingdom with `claim_account:`.
* The recipient must be a nation or kingdom treasury — contracts never pay
  into an individual wallet. From the treasury, `/transfer` (or `/bank transfer`
  or `/give`) pays builders or anyone else. Individuals hold their own money,
  but they are outside the contract and shop economies.
* `/contract reopen` accepts only `approved`, `closed` or `completed` cases
  (`Contract.REOPENABLE_STATUSES`). `rejected` and `cancelled` are final:
  reopening one would make its forfeited, unclaimed tranche claimable again.
* Who may claim: the claim account's leader, the original requester, or a
  Union Admin.
* Money moves only inside `_pay_claim`, always with `claim_contract_tranche`,
  `debit_union`/`debit_crown`, `add_balance` and both `log_tx` calls in one
  transaction, so a crash, a concurrent click or a short treasury cannot pay
  twice or mint credits. If the treasury is short, the whole claim rolls back
  and can be retried.
* Crown approval checks the unreserved Crown balance (`crown_reserved()` covers
  legacy supplier obligations); Union approval checks the Union balance.
* Legacy rows (`redeem_key` present) are treated as fully owed: both tranches
  are claimable even without a release, because the old code charged them.

---

## 11. House style for future contributors

* Keep money movement inside `async with db.transaction():`.
* Log every balance change with `db.log_tx(...)`.
* Never hard-code tokens, IDs or channel IDs; use settings or environment.
* Keep pure math (payouts, pricing) free of Discord imports so it stays easy
  to test.
* Add a migration entry whenever you add a column; never edit an old database
  by hand.
* Treat `.env`, `*.db`, `*.db-wal` and `*.db-shm` as secrets/data, not code.
* **Fix a bug and leave a check behind.** Add it to `checks/` and to the table in
  `BUG-FIXES.md`. A check that lives only in a scratch directory is lost at the
  end of the session, which is exactly how a process-wide transaction bug
  survived several passes.
* Write the doc while the reasoning is fresh: README section 7 for a rule,
  section 12 for what changed and how it was verified, `AGENTS.md` for the
  invariant the next agent must not break, `BUG-FIXES.md` for the defect.

---

## 12. Maintenance log

Newest first. Each entry records what changed, how it was verified and whether
it is live, so a future agent can tell deployed code from local work. Entries
are historical: where an older one says "not uploaded" or quotes a rate such as
5%, a newer entry supersedes it, and section 7 always holds the current rules.

### 2026-10-29 — Session close-out (`20261029-0216`, docs to `0315`; **live since 03:00 UTC**)

**State at close.** The process restarted at **03:00:49–55 UTC** and loaded the
`20261029-0216` build: `/give` is in Discord's command list with
`source`/`member`/`amount`, the startup schema created `case_counters_prefix`,
and the surface is **112 / 16 cogs**. `contracts.faction` was already present
from the 02:00 restart of the `20261029-0028` build, which also loaded the
proposal-only `/faction contract`. The live DB passes `integrity_check: ok`.

**Live action still to take.** `UN-COMMUN-001` is a closed legacy case with
`upfront_claimed = 0` and `final_claimed = 0`, so
`/contract claim case_id:UN-COMMUN-001` pays the whole **1,000,000**
(250,000 + 750,000) from the Union into Commune Ouvrière;
`/contract release case_id:UN-COMMUN-001` then marks it completed. SFTP cannot
run slash commands — an admin must do both in Discord.

**Close-out checks.** Live tree **77/77** hash-identical to this checkout; the
deployed pull (`/tmp/deployed_20261029-0216`) passes `compileall`, `pyflakes`
and `checks/run_all.py` **18/18**; the live DB pull at 03:00 is
`integrity_check: ok`, with 0 per-account gaps and the historical 30,000
deleted-nation gap unchanged. `case_counters_prefix` is empty until the first
new case, so the next Union case is `UN-SOVERE-004` and the first Crown case is
`CR-SOVERE-001` (verified by a dry run on a copy of the live DB).

### 2026-10-29 — `/give`, per-faction case counters, prompt floor (uploaded, stamp `20261029-0216`; **live since 03:00 UTC**)

**Change.**
- New top-level **`/give`**: `source` (nation or kingdom), `member`, `amount`
  (`all` / `half` / `quarter` resolve against the treasury). It is the
  treasury-to-member shortcut over `account_transfers.transfer_funds()`: the
  treasury is debited with the balance guard, the member wallet is created on
  first receipt, a capped wallet refuses the whole grant, both `treasury_give`
  ledger rows and the Union commitment hook share one transaction. `/transfer`
  and `/bank transfer` still work in every direction; `/give` removes the need
  to spell out an individual destination.
- `Database.next_case_number()` now allocates **per faction prefix**. Union cases
  continue their `UN-<TAG>-NNN` sequence from the legacy `case_counters` /
  `case_counters_global` floors; Crown cases start at `CR-<TAG>-001` in their own
  `case_counters_prefix` row. The shared global row is a UN mirror only.
- Gilbert's prompt now states the **1-credit floor** (the Micro-Utility tier
  starts at 1, not 100) and names both case-ID prefixes (`UN-...` for the Union,
  `CR-...` for the Crown).
- Docs: section 9 no longer claims the 250,000 upfront of `UN-COMMUN-001` was
  claimed — the live row has both flags at 0, so one claim pays the whole
  1,000,000; `gilbert/README` no longer quotes a 100-credit minimum; the
  `albert_memory` note no longer points at the retired `/audience`.

**Verification.** `check_transfers.py` drives `/give` end to end for a nation and
a kingdom treasury (source debit, member credit, two `treasury_give` rows,
non-leader refusal, individual-source refusal, capped-wallet refusal with no
movement); `check_contracts.py` pins the independent UN/CR starter counters and
concurrent reservations; `check_gilbert.py` pins the 1-credit floor and the CR
prefix. `compileall` and `pyflakes` are clean, `checks/run_all.py` is **18/18**,
and the surface is **112 / 16 cogs** (`check_cogs.py` pins the three `/give`
options). Uploaded with `.bak-20261029-0216` backups and matching SHA-256; a
fresh pull of the deployed tree passes the suite. **Live since 03:00:55 UTC**:
`/give` is in Discord's command list (112 commands) and `case_counters_prefix`
exists on the live database.

### 2026-10-29 — One proposal field; The Crown pays for supplies (uploaded, stamp `20261029-0028`; **live since 02:00 UTC**)

**Change.** `/faction contract` now takes `faction`, `account`, a single
free-text `proposal`, and `budget`. The `project` option is gone, along with
`item` and `quantity`. A Union proposal is public work for Gilbert. A Crown
proposal is a supply offer for Albert: the goods are collected in-game by an
admin, the bot does not track item counts, and the 25/75 tranches come from the
Crown treasury (25% at approval, 75% after collection). The dead `/commit`
command — already removed from the command tree — was deleted rather than left
as a second contract surface; `/faction commitment` and `/faction uncommit` now
call the Union desk directly. Proposal text is stored in the legacy
`contracts.project` column so existing rows and backups keep working; the
`project=` keyword is accepted by the storage/prompt helpers for one deploy
window only.

**Verification.** `check_contracts.py` asserts the opening turn calls the text a
proposal, both briefings label it "Proposal brief", and the Crown prompt covers
supply collection. `check_cogs.py` pins the four options
(`faction`/`account`/`proposal`/`budget`); `check_factions.py` drives a blank
proposal refusal; `check_gilbert.py` pins Albert's supply wording. Suite
**18/18**, `compileall`/`pyflakes` clean, surface unchanged at **111 / 16
cogs**. Uploaded 12 files with `.bak-20261029-0028` backups and matching
SHA-256. **Restart required** for the new command surface and prompts.

### 2026-10-28 — Contract requests are a conversation again (uploaded, stamp `20261029-0000`; restart pending)

**Change.** `/faction contract` no longer rolls dice or checks faction
membership. It takes `faction`, `account`, `project` and `budget`, and opens a
direct negotiation: Gilbert for the Workers' Union, Albert for The Crown.
`item` and `quantity` were removed from the command surface. Crown requests now
use the same 25/75 tranche engine as the Union, paid from `crown_account`
(`contracts.faction = 'crown'`, case IDs `CR-<TAG>-<NNN>`, quota counted per
faction). Legacy Crown supplier rows still deliver and pay through
`/faction deliver`; no new supplier rows are created.

Both briefings are assembled before the first reply and carry the live numbers:
balance, soldiers, the relevant treasury (including Crown credits reserved for
legacy suppliers), quota, Union contributions or Crown supply history, and tax
status. Gilbert's and Albert's prompts were softened — Gilbert is explicitly
kind and encouraging, Albert warm — and both state that membership is never
required to request a contract.

**Verification.** `checks/check_contracts.py` covers a real Crown request (CR
case ID, `crown-contract-*` thread, `faction='crown'`, Albert intro), the
Crown-funded 25% claim, the per-faction quota, a short Crown treasury refusal,
and asserts both system prompts contain the case ID, budget, treasury, soldiers
and membership rule. `check_factions.py` drives a non-member Crown request
through the command and pins the removed RNG; `check_cogs.py` pins the four
contract options; `check_gilbert.py` pins the tone and membership text. Suite
**18/18**, `compileall`/`pyflakes` clean, surface unchanged at **111 / 16 cogs**.
Uploaded over SFTP with `.bak-20261029-0000` backups; every file was re-downloaded
and SHA-256 matched, and a fresh pull of the deployed tree
(`/tmp/deployed_20261029-0000`) passes **18/18**. **A restart from the hosting
panel is still required** before the process serves the new command, the
`contracts.faction` migration and the new prompts.

### 2026-10-27 — Session close-out (uploads `20261027-2325` + `20261027-2340`; restart pending)

**Live state at 2026-10-27 23:55 UTC.** The faction/Crown feature, the five
bug-testing fixes and the kingdom cap of **6** are live: the process restarted
from the hosting panel, `faction_members`, `crown_account`, `crown_contracts`,
`crown_sales`, `faction_donations` and `albert_memory` exist, the three legacy
Union members were migrated, and an admin funded the Crown with **10,000,000**
credits (`crown_fund` tx at `23:55:35`). The historical **30,000-credit**
`BoxierFire25` gap remains and is not rewritten.

**2026-10-28 04:35 UTC live check.** The `20261028-0135` `/faction` group is
serving, but its surface still includes `/faction audience` and the two
specialized contract subcommands. The `20261028-0435` contract-judgment build
is uploaded; **restart is required** before `/faction contract` replaces them.

**Smoke test after restart:** `/faction status faction:crown`;
`/faction join faction:crown account:<nation>`;
`/faction fund faction:crown` [Admin];
`/faction contract faction:crown account:<nation> budget:<credits> item:<item> quantity:<n>`;
`/faction contracts faction:crown account:<nation>`.
Existing Union members should still be present from the migration.

### 2026-10-28 — `/faction` hub admin-check bypass (uploaded, stamp `20261028-0311`)

**Fix.** `admin_only()` registers an `app_commands.check` on the command
*object*, and only the dispatcher runs it. The `/faction` hub subcommands that
forwarded with `<command>.callback(...)` (`fund`, `withdraw`, `setchannel`) never
ran the delegate's check, so any member could mint credits into the Crown or
Union treasury and drain the Crown. `Factions._require_admin()` now gates those
three subcommands and they also carry `@admin_only()`, so the hub is gated on
both the dispatch and delegation paths. `checks/check_factions.py` drives the
hub as a non-admin and asserts that both treasuries and the commitment channel
are unchanged (plus that an admin can still fund the Crown);
`checks/check_cogs.py` pins `faction fund`, `faction withdraw` and
`faction setchannel` as live surface. Suite **18/18**, command surface
**111 / 16 cogs**, `compileall`/`pyflakes` clean on a fresh pull of the deployed
tree. **Restart still required** for the 0135 process to pick up everything
since then.

### 2026-10-28 — Contract request polish (uploaded, stamp `20261028-0450`)

**Fix.** A Union request that passed the record roll but then failed to open
(missing contract channel, quota, leverage, permission or treasury) no longer
sends a misleading "record passed" review after the error. The Crown terms
errors keep the chance/roll/factors visible, and the refusal text now points at
`/faction fund faction:crown` instead of the removed `/crown fund`. The status
line describes soldiers as a soft odds advantage, the unreachable audience
method/imports and delivery/donation eligibility branches were removed. The
delivery rule now names `/faction deliver` rather than the old
`/faction donate` item form. `checks/check_factions.py` also asserts that a
Crown budget below the locked value signs no contract, and covers both the
successful Union review and the silent failed opening. The suite is **18/18**;
the command surface stays **111 / 16 cogs**.

### 2026-10-28 — One faction contract with stat/donation judgment (uploaded, stamp `20261028-0435`)

**Feature.** `/faction audience`, `/faction union-contract` and
`/faction crown-contract` are gone. `/faction contract` is the one contract
request and the `budget` is required:

```text
/faction contract faction:union account:<nation> budget:<credits> project:<text>
/faction contract faction:crown account:<nation> budget:<credits> item:<item> quantity:<n>
```

**Judgment.** No hard soldier, donation or membership threshold exists. The
representative rolls against a chance built from the live record:

- Crown: soldiers, credits/items received from the account, and supply history.
- Union: membership, dues/donations/commitments, and soldiers.

A failed roll creates no contract and shows the chance/factors. A successful
Union request opens the Gilbert negotiation; a successful Crown request signs
a supplier contract. The Crown's requested budget is a ceiling: the computed
contract value must fit inside it.

**Rules changed.** Crown membership no longer has a 20-soldier gate; more
troops simply improve contract odds. The contract minimum is now **1 credit**
(no practical minimum; the budget must be positive).

**Tests.** `checks/check_factions.py` covers the score ordering (more troops /
donations / Union support raises the chance), joins a 19-soldier Crown member,
verifies low-troop tax, and drives approved and refused `/faction contract`
requests. `check_cogs.py` pins 111 commands.

**Deployment.** Uploaded with backups `.bak-20261028-0435`; the deployed copy
was pulled to `/tmp/deployed_20261028-0435`, passing `compileall`, `pyflakes`
and `checks/run_all.py` **18/18**. **Restart is required** for the new command
surface and rules.

### 2026-10-28 — Audience open to all, with budget requests (uploaded, stamp `20261028-0355`)

**Change.** `/faction audience` no longer implies any faction-membership
requirement. Any nation or kingdom can address Gilbert or Albert, including
unaffiliated accounts and accounts serving the other faction. The prompts and
deterministic fallbacks now say so explicitly.

**Tone.** Membership only changes how the representative treats the petitioner:
Union members in good standing get a warmer Gilbert; Crown members in good
standing get a warmer Albert. Nonmembers still get a full, courteous answer and
are never told they must `/faction join` to speak.

**Budget request.** `/faction audience` now takes an optional `budget` in
credits. It is passed into both representatives' context and fallbacks, shown
as a `Requested Budget` field, and stored with the conversation. It is
discussion only: the representative cannot approve it or create a contract;
use `/faction union-contract` or `/faction crown-contract` for that.

**Tests.** `checks/check_factions.py` adds an unaffiliated `Neutral` nation and
drives both `/faction audience` routes with the AI disabled, asserting that
Albert and Gilbert answer and that their fallbacks say allegiance/membership is
not required. It also checks the prompt/context audience lines, sends a budget
to each route, and verifies the response field and stored memory.

**Deployment.** Uploaded with backups `.bak-20261028-0355`; the deployed copy
was pulled to `/tmp/deployed_20261028-0355`, passing `compileall`, `pyflakes`
and `checks/run_all.py` **18/18**. **Restart remains required** for 0135 and
0355.

### 2026-10-28 — One `/faction` group for everything (uploaded, stamp `20261028-0135`)

**Feature.** The separate `/join`, `/commit`, `/donate`, `/audience`, `/crown`
and `/union` commands are gone. `/faction` is the only entry point, using
action subcommands. Discord does not allow one slash command to change its
options after a choice, so each action is a `/faction` subcommand with only its
own options:

```text
/faction join faction account
/faction leave account
/faction union-contract account project budget
/faction crown-contract account item quantity
/faction commitment account
/faction uncommit account
/faction donate faction account amount
/faction deliver account item quantity
/faction audience faction account message
/faction status faction [account]
/faction contracts faction [account]
/faction cancel contract_id
/faction fund faction amount
/faction withdraw amount
/faction members
/faction setchannel channel
```

The `/contract` group remains only for contract lifecycle administration
(approve, release, claim, reject, close, reopen, board/channel/role).

**Counts / tests.** The surface is **113 commands / 16 cogs**: the 18 old
faction commands were removed, the single `/faction` command became a group,
and 16 action subcommands were added. `check_factions.py` drives join, Crown
contract, Crown delivery, status, both audience routes, Union contract, Union
commitment and uncommit through the new subcommands; `check_cogs.py` pins 113
and asserts every old faction command is absent.

**Deployment.** Changed files were uploaded with backups `.bak-20261028-0135`;
the deployed copy was pulled to `/tmp/deployed_20261028-0135`, passing
`compileall`, `pyflakes` and `checks/run_all.py` **18/18**. **Restart from the
hosting panel is required** before `/faction` and the removal of the old
commands appear live.

### 2026-10-28 — Commitments and uncommits under `/commit` (uploaded, stamp `20261028-0105`)

**Feature.** `/union commit` and `/union uncommit` are removed. The single
`/commit` command now covers them:

```text
/commit faction:union kind:commitment  account
/commit faction:union kind:uncommit    account
```

- `kind:commitment` posts the same admin-approval request as the old
  `/union commit`.
- `kind:uncommit` cancels a pending or active commitment; later credits stay in
  the account's Royal Bank.
- `faction:crown kind:commitment` and `kind:uncommit` are refused.
- `/union` is now treasury, membership, donations, admin funding and channel
  configuration only.

**Counts / tests.** The surface is **114 commands / 16 cogs**.
`checks/check_factions.py` requests a Union commitment through
`/commit faction:union kind:commitment`, verifies the approval post, then
cancels it through `kind:uncommit`; `checks/check_cogs.py` pins 114 and asserts
that `/union commit` and `/union uncommit` are absent. The full suite is
**18/18**.

**Deployment.** Changed files were uploaded with backups `.bak-20261028-0105`;
the deployed copy was pulled to `/tmp/deployed_20261028-0105`, passing
`compileall`, `pyflakes` and `checks/run_all.py` **18/18**. **Restart from the
hosting panel is required** before the new choices appear live.

### 2026-10-28 — One contract entry point (uploaded, stamp `20261028-0055`)

**Feature.** `/contract request` is removed. `/commit` is now the single entry
point for every contract/commitment request:

```text
/commit faction:union kind:contract    account project budget
/commit faction:union kind:commitment  account
/commit faction:union kind:uncommit    account
/commit faction:crown kind:contract    account item quantity
```

- `faction:union kind:contract` delegates to `Contract.request_contract()`, the
  old `/contract request` flow: leader or Union Admin only, project + budget,
  natural-cycle quota, case ID and Gilbert thread.
- `faction:union kind:commitment` keeps the admin-approved whole-balance
  commitment flow; `kind:uncommit` stops a pending or active commitment.
- `faction:crown kind:contract` keeps the Crown supplier contract flow.
- `faction:crown kind:commitment` and `kind:uncommit` are refused.
- The `/contract` group now contains only lifecycle commands
  (`close`, `status`, `list`, `claim`, `approve`, `release`, `reject`, `reopen`,
  `setchannel`, `setboard`, `setrole`).

**Counts / tests.** The surface is **116 commands / 16 cogs** (117 minus
`/contract request`). `checks/check_factions.py` verifies that
`/commit faction:union kind:contract` reaches Gilbert’s desk and that the Union
commitment route still posts its approval request; `checks/check_cogs.py` pins
the new count. The full suite is **18/18**.

**Deployment.** Changed files were uploaded with backups `.bak-20261028-0055`;
the deployed copy was pulled to `/tmp/deployed_20261028-0055`, passing
`compileall`, `pyflakes` and `checks/run_all.py` **18/18**. **Restart from the
hosting panel is required** before the command surface changes live.

### 2026-10-28 — Merged faction commands and audience repair (uploaded, stamp `20261028-0015`)

**Feature.** `/commit` is now one command with a `faction` choice:
`faction:union` calls the extracted `Union.request_commit()` (the same admin
approval flow as `/union commit`), while `faction:crown` signs a Crown supplier
contract. The selected faction must match the account's allegiance.
`/audience faction:union` speaks to Gilbert; `/audience faction:crown` speaks
to Albert. The old `/crown audience` subcommand is replaced by the unified
command.

**Audience repair.** The old Crown audience called the model directly and could
wait for the provider's full 60-second timeout. The unified command caps the
call at `config.NPC_AUDIENCE_TIMEOUT_SECONDS` (20s) and falls back to the
deterministic in-character reply on timeout or error. Conversation memory is
now NPC-separated: `albert_memory.npc` is `albert` or `gilbert`, so the two
histories cannot mix. A migration adds the column to existing databases.

**Tests.** `checks/check_factions.py` drives both audience routes with the AI
disabled, asserts the memory separation, and drives the real Union desk through
`/commit faction:union` (pending request and channel post). `checks/check_cogs.py`
pins the 117-command surface, including `/audience`.

**Deployment.** All changed files were uploaded with per-file backups
(`.bak-20261028-0015`) and hash-verified; the live tree is **74/74**
`.py`/`.md` files identical to this checkout. The deployed tree pulled to
`/tmp/deployed_20261028-0015` passed `compileall`, `pyflakes` and
`checks/run_all.py` **18/18** (117 commands / 16 cogs) against that copy.
**Restart from the hosting panel is required** before `/audience`, the
`/commit faction` choice and the `albert_memory.npc` migration are live.

### 2026-10-27 — Factions, The Crown and Albert (uploaded, stamp `20261027-2325`; restart pending)

**Feature.** Added the two-faction layer: a nation/kingdom belongs to at most one
of the Workers' Union (Gilbert) or The Crown (Albert). `/join` switches sides and
cancels the old side's commitments. The Crown requires 20 total soldiers
(kingdoms sum member nations), taxes **100% of the regular-soldier payout line**
after a **30-day grace** into a new `crown_account`, and runs item supplier
contracts. `/commit faction:crown` signs a contract for one catalog item;
`/donate item` delivers it; `/donate amount` sends credits to the current
faction. The Crown surface is `/crown status|contracts|cancel|fund|withdraw`,
and `/audience faction:crown` reaches Albert with a factual record briefing,
deterministic fallback and persisted conversation memory.

**Rules implemented.** Active contract capacity is **1 nation / 2 kingdoms**.
The value cap is **10M below 15M treasury**, **15M from 15M through 50M**, and
**one third above 50M**. The Crown must hold the full value plus every active
contract's value before signing. Demand starts at 100%, decays **5 points per
natural cycle** in which that account fulfils that item, floors at **40%**, and
only affects future contracts. Contracts last **3 natural cycles**, pay only on
full delivery, and a short Crown treasury rolls the delivery back. The Crown
withdraw command reserves active contract values.

**Data.** New tables: `faction_members` (one PK row per account, `union`/`crown`,
status, joined_at), `crown_account`, `crown_contracts`, `crown_sales`,
`faction_donations`, `albert_memory`. `union_members` is migrated into
`faction_members` at startup and retained as a rollback mirror; `/union join`
and `/union leave` now go through the faction logic, and leaving/cancelling a
Union commitment is atomic.

**Commands / counts.** `/join`, `/commit`, `/donate`, `/audience` and the
`/crown` group (status, contracts, cancel, fund, withdraw) take the surface to
**117 commands / 16 cogs**.

**Cap update (stamp `20261027-2340`).** `MAX_NATIONS_PER_KINGDOM` is now **6**
(was 4). The `/kingdom addnation` description, `cogs/README.md`, `README.md`
and the known-gap note follow the constant; `check_factions.py` now pins the
boundary (the sixth nation joins, the seventh is refused).

**Verification.** `checks/run_all.py` **18/18**, `compileall`/`pyflakes` clean.
`check_factions.py` covers switching cleanup, soldier eligibility, tax and
grace, contract caps/value tiers, partial and full delivery, demand decay,
expiry, short-treasury rollback, Albert context and the command callbacks. A
dry run of `Database.connect()` on a copy of the live database created every new
table, seeded the three existing Union memberships into `faction_members`, and
kept `integrity_check: ok`.

**Uploaded with stamp `20261027-2325`.** This upload also carries the five
bug-testing fixes recorded in the entry below. Per-file backups are beside each
existing original; new files have none. **Restart pending**: the live process
still serves the previous build until the hosting panel restarts it, and the new
schema tables are created at that startup. Files: `factions.py` (new),
`albert_prompt.py` (new), `cogs/factions.py` (new), `cogs/payout.py`,
`cogs/union.py`, `cogs/ledger.py`, `bot.py`, `config.py`, `database.py`,
`checks/check_factions.py` (new), `checks/check_cogs.py`, `checks/run_all.py`,
`README.md`, `AGENTS.md`, `BUG-FIXES.md` and `cogs/README.md`, plus the bug-fix
files listed in the entry below.

### 2026-10-27 — Live audit and bug pass (uploaded with stamp `20261027-2325`; restart pending)

**Live check.** The deployed tree was re-read over SFTP: all **70** `.py`/`.md`
files matched this checkout byte-for-byte (SHA-256), including `bot.py`, every
cog, `checks/` and the docs. A pulled copy of the live database (`.db` + `-wal`
+ `-shm`) reported `integrity_check: ok`, no foreign-key errors, no negative
balances, no over-cap wallets, no duplicate case IDs, no open blackjack/poker
hands and no ghost settings row. Per-account ledger reconciliation is exact for
kingdoms, individuals, the Union, the Casino and the Fishing Guild; the only
gap is the documented **30,000 credits** from deleted nation id 5
(`BoxierFire25`), unchanged by design.

**The restart has happened.** Live transactions at `2026-10-27T22:23:49` include
`individual_tax`, `union_commit` and the new payout behaviour, and
`settings.last_cycle_at` is `2026-10-27T22:22:57`, so the process is serving the
`20261027-221143` build. The earlier "restart pending" notes are historical.

**Five defects found and fixed locally.**

1. **Two simultaneous `/payout default` runs paid twice.** The due-check ran
   before `_cycle_lock`, so a run that waited behind a winner still paid a full
   natural cycle and advanced `contract_cycle` again. Reproduced with two
   concurrent command callbacks: a nation gained 40,000 instead of 20,000 and
   the cycle moved from 1 to 3. `_run_cycle(..., require_due=True)` now repeats
   the due-check under the lock; the command and the 10-minute task pass it,
   while `force:true` and direct engine calls bypass it.
2. **`/bank setbalance` awaited the Discord response inside its transaction.**
   A slow or expired interaction held `BEGIN IMMEDIATE` over every money
   command and could roll the admin's edit back after they saw it applied. The
   reply now happens after the commit; the fresh-delta logic is unchanged.
3. **The `/allin` confirmation could be double-clicked into two bets.**
   `settle_allin(..., view=...)` flips a `used` flag synchronously and refuses
   the second click before it reads the wallet; `Cancel` also refuses once a
   bet has started.
4. **`blackjack.is_soft()` misread multi-ace hands.** `A♠ A♥ 9♦` is a soft 21
   (one ace stays 11) but the old `raw <= 21` test called it hard. The helper
   now counts demoted aces. Gameplay is unchanged: no blackjack rule branches
   on `is_soft`.
5. **A creation name race could strand a petition or crash a direct add.** If a
   direct `/nation add` or `/kingdom add` won the race at approval, the
   `IntegrityError` path edited the card to "Name Taken" but the petition
   stayed `pending`; the reverse race left the direct add unhandled. The
   petition is now marked `failed` in a fresh transaction (guarded against a
   concurrent decision), and both direct add commands catch the same
   `IntegrityError` and report "Name Taken".

**Verification.** `checks/run_all.py` **17/17**, `compileall` and `pyflakes`
clean, command count unchanged at **107 / 15 cogs**. New regression cases live
in `check_tax_cycle.py` (concurrent natural runs), `check_bank.py`
(reply-after-commit), `check_allin.py` (duplicate confirmation),
`check_casino.py` (multi-ace soft hands) and `check_creation_race.py`
(approval and direct-add name races).

**Uploaded with stamp `20261027-2325`.** Files: `cogs/payout.py`,
`cogs/bank.py`, `cogs/individual.py`, `cogs/nation.py`, `cogs/kingdom.py`,
`blackjack.py`, `creation.py`, the five check scripts above, `README.md`,
`AGENTS.md`, `BUG-FIXES.md` and `cogs/README.md`. The running process still
needs the hosting-panel restart.

### 2026-10-27 — Session close-out: uploaded, restart pending (stamp `20261027-221143`)

**Uploaded.** The payout split, unified transfers, net-worth tax wiring and the
two bug-testing fixes are now on the live SFTP tree. Existing files were backed
up in place as `<file>.bak-20261027-221143`; the new files are
`account_transfers.py`, `cogs/transfers.py` and
`checks/check_full_transfers.py`. No schema migration was required: the new code
uses the existing tables and columns (including the live `case_counters_global`,
`poker_hands` and `fishing_guild_account`).

**Verification against the deployed tree.** The upload was pulled to
`/tmp/deployed_20261027-221143`: `compileall`/`pyflakes` clean,
`checks/run_all.py` **17/17**, command count **107 / 15 cogs**. There was no live
file drift against the pre-change baseline before upload, and every uploaded
file was SHA-256 verified on the remote.

**Restart pending.** SFTP has no shell, so the running process still serves the
previous build. Restart it from the hosting panel to activate this upload. The
live database is unchanged; the historical **30,000-credit** gap from deleted
nation id 5 (`BoxierFire25`) remains and is deliberately not rewritten by this
build.

**Files uploaded (backups where they existed).** `account_transfers.py` (new),
`cogs/transfers.py` (new), `cogs/payout.py`, `cogs/bank.py`,
`cogs/individual.py`, `cogs/nation.py`, `cogs/kingdom.py`, `cogs/contract.py`,
`database.py`, `utils.py`, `bot.py`, `checks/check_full_transfers.py` (new),
`checks/check_tax_cycle.py`, `checks/check_database.py`,
`checks/check_contracts.py`, `checks/check_cogs.py`, `checks/run_all.py`,
`README.md`, `AGENTS.md`, `BUG-FIXES.md`, `cogs/README.md`.

### 2026-10-27 — Bug-testing close-out

**Status.** Two audit defects are fixed and pinned by checks; the feature work is
unchanged. Uploaded with the session close-out (stamp `20261027-221143`); the
running process still needs the panel restart.

1. **Disbanding a nation/kingdom no longer burns credits silently.**
   `Database.delete_account()` re-reads the row, deletes it and writes a
   negative `nation_delete` / `kingdom_delete` ledger row for the remaining
   balance (`balance_after = 0`) in one transaction; both cogs report the burned
   amount. The historical live gap from nation id 5 (`BoxierFire25`, 30,000
   credits) remains because history is not rewritten, but new deletes reconcile.
2. **`/contract reopen` can no longer resurrect a rejected case.**
   `Contract.REOPENABLE_STATUSES` is `("approved", "closed", "completed")`;
   `rejected` and `cancelled` are final, so the forfeited tranche stays
   unclaimable. The command description now matches.

**Extra testing.** Adversarial passes covered 20 concurrent transfers from one
wallet (exactly ten paid, ten refused), five concurrent transfers into a capped
wallet (exactly two paid, three refused), exact-room vs one-over-cap, stale
leader re-check, deleted source/destination accounts, same individual by
Discord id, virtual destination-wallet creation, a commitment with pre-existing
balance, and the Cash/Default/Trigger commands plus the background tick end to
end. The concurrent-debit case is now permanent in
`check_full_transfers.py`, and the three command paths are permanent in
`check_tax_cycle.py`.

**Verification.** `checks/run_all.py` **17/17**; `check_database.py` and
`check_contracts.py` carry the new assertions; `compileall`/`pyflakes` clean;
command count unchanged at **107 / 15 cogs**. Files: `database.py`,
`cogs/nation.py`, `cogs/kingdom.py`, `cogs/contract.py`,
`checks/check_database.py`, `checks/check_contracts.py`, `README.md`,
`AGENTS.md`, `cogs/README.md`, `BUG-FIXES.md`.

### 2026-10-27 — Payout split, tax hook and unified transfers

**Status.** Uploaded with the session close-out (stamp `20261027-221143`); the
running process still needs the panel restart. Counts are **107 commands /
15 cogs / 17 checks**.

**Payouts.** `/payout run` is now `/payout cash` (Cash Cycle). It pays the
nation/kingdom allowances, Union dues/commitments and the 5% individual
net-worth tax, and deliberately does **not** touch the natural schedule or the
contract cycle. `/payout default` runs the full natural cycle in one execution
(cash + dues + tax + `advance_contract_cycle()` + the next timestamp); it
refuses when the next cycle is not due unless `force:True`. `/payout trigger
days:N` queues the natural cycle now (`days:0`) or schedules it N days out
without paying. The background 14-day job still calls
`_run_cycle(natural=True)`.

**Transfers.** New `account_transfers.py` is the single atomic transfer core for
all nine pairs between nations, kingdoms and individual wallets. New
`cogs/transfers.py` exposes `/transfer`; `/bank transfer` now uses the same core
and account autocomplete, so treasury → wallet works. Source permission
(leader/owner, admin override), the 100M cap refusal
(`credit_individual_strict`), both ledger rows and the destination
Union-commitment hook all live in that one transaction. `/individual transfer`
and `/individual pay` now construct refs and call the same core; only their
reply wording differs.

**Tax.** `payouts.individual_tax()` (500 bp) is called by both cycle modes; no
nation or kingdom is taxed. The hook lives in `_run_cycle`, so a Cash Cycle run
cannot bypass it.

**Verification.** `checks/run_all.py` **17/17**. New
`checks/check_full_transfers.py` covers all nine directions, permissions, admin
override, cap/overdraw refusals, cross-guild refusal, ledger nets and the
commitment hook; `check_tax_cycle.py` now asserts Cash leaves the schedule and
contract cycle alone and Default advances both. `compileall`/`pyflakes` clean;
command count **107 / 15 cogs**. Files: `account_transfers.py` (new),
`cogs/transfers.py` (new), `cogs/payout.py`, `cogs/bank.py`,
`cogs/individual.py`, `utils.py`, `bot.py`,
`checks/check_full_transfers.py` (new), `checks/check_tax_cycle.py`,
`checks/check_cogs.py`, `checks/run_all.py`, `README.md`, `AGENTS.md`,
`cogs/README.md`, `BUG-FIXES.md`.

### 2026-10-27 — Session close-out: all work uploaded, restart pending

This session shipped three code changesets, each uploaded and hash-verified:

1. **Economy retune + all 11 audit fixes** (stamp `20261027-132619`): coinflip
   is now 50% / 2x (zero house edge), `/allin` uses 1.1x hardness (10/11 return,
   9.09% edge), and the 5% individual net-worth tax runs on **every** payout run,
   natural and manual. The audit fixes cover the stale tax list, wallet
   resurrection, draw membership, contract quota/approval races, stale
   `setbalance`, one-shot status writes, cross-guild case IDs, poker uncalled
   bets/short all-ins, the DM settings ghost row and the runner false pass.
2. **Kit creation cooldown 30 → 7 days** (stamp `20261027-133459`): one constant,
   `config.KIT_COOLDOWN_DAYS`, drives both the friendly message and the atomic
   `claim_kit_cooldown()` window. Existing rows become eligible after seven days.
3. **Minecraft-themed poker bot names** (stamp `20261027-134031`): new
   `poker_names.py` uses the same `GilbertAI` client and `AI_API_KEY` as Gilbert
   to refresh a themed name pool in the background; `/poker` samples three names
   per table and falls back to `config.POKER_BOT_NAME_POOL`. The engine stays
   pure and never waits on the network.

Verification for every changeset: `checks/run_all.py` **16/16**, including the
pulled deployed trees (`/tmp/deployed_20261027-132619`,
`/tmp/deployed_20261027-133459`, `/tmp/deployed_20261027-134031`);
`compileall`/`pyflakes` clean; command count **104 / 14 cogs**. A read-only live
database copy migrated cleanly (`integrity: ok`; the migration created
`case_counters_global` in the copy) with no duplicate case IDs and no ghost
settings row. The live database itself does not have `case_counters_global`
until the restart.

**Restart still required.** The live process has not been restarted, so it still
serves the old 49% coinflip, 1.2x all-in, natural-only tax, 30-day kit cooldown
and medieval bot names until the hosting panel restarts it. `case_counters_global`
is created only at that restart. This README/AGENTS refresh is the session
close-out; each changed remote file has its own `.bak-<stamp>` backup beside it
for rollback.

### 2026-10-27 — Minecraft-themed poker bot names from Gilbert's AI key (uploaded 2026-10-27 13:40 UTC, backup stamp `20261027-134031`)

`/poker` now seats three **Minecraft-themed** bot names instead of the fixed
medieval trio. The new `poker_names.py` uses the same `GilbertAI` client and
therefore the same `AI_API_KEY` / `AI_API_URL` / `AI_MODEL` as Gilbert: it asks
for a JSON array of themed names in a background task, dedupes and validates
them, and keeps a curated `config.POKER_BOT_NAME_POOL` as the offline fallback.
`Casino.__init__` starts the refresh; `/poker` samples three names synchronously,
so a slow or failed AI request can never expire the interaction. `poker.py`
stays pure and only receives the `names=` sequence.

Files: new `poker_names.py`; changed `config.py`, `cogs/games.py`,
`checks/check_poker.py`, `README.md`, `cogs/README.md` and `AGENTS.md`.
Verification: `checks/run_all.py` **16/16** (including parser/fenced-JSON,
dedupe, curated fallback and custom-seat-name checks), `compileall`/`pyflakes`
clean, command count unchanged at **104 / 14 cogs**. The deployed tree was
re-pulled to `/tmp/deployed_20261027-134031` and passed **16/16** against that
copy.

### 2026-10-27 — Kit cooldown shortened to 7 days (uploaded 2026-10-27 13:34 UTC, backup stamp `20261027-133459`)

`config.KIT_COOLDOWN_DAYS` is now **7** (was 30). `/kit create` uses the same
constant for the friendly “next kit” message and for the atomic
`claim_kit_cooldown()` window, so there is one source of truth. Existing
`kit_cooldowns` rows are not rewritten: a player whose last kit was created
under the old 30-day rule becomes eligible as soon as seven days have passed.
Files: `config.py`, `cogs/kits.py`, `checks/check_database.py`, `README.md`,
`cogs/README.md` (and `AGENTS.md` for the handoff). Verification:
`checks/run_all.py` **16/16**, including an explicit day-6 blocked / day-7
allowed boundary; `compileall`/`pyflakes` clean; command count unchanged at
**104 / 14 cogs**. The deployed tree was re-pulled to
`/tmp/deployed_20261027-133459` and passed **16/16** against that copy.

### 2026-10-27 — Economy retune and audit fixes (uploaded 2026-10-27 13:26 UTC, backup stamp `20261027-132619`)

**Economy retune (owner decision).**
- `/individual coinflip` is now an even **50%** win at 2x (`config.COINFLIP_WIN_BP = 5_000`).
  `coinflip.house_edge_bp()` is **0**: a fair pass-through, with the Casino
  funding wins from its own balance. The 49/51 sink and its "must stay positive"
  invariant are retired; do not restore them without a new instruction.
- `/allin` hardness is **1.1** (`ALLIN_HARDNESS_NUMERATOR = 11`,
  `DENOMINATOR = 10`). The chance is `1 in (1.1 x n)`, the expected return is
  `10/11`, and the flat house edge is **9.09%**; `n = 5` is 1 in 5.5.
- The **5% individual net worth tax now runs on every payout run**, natural and
  manual. `Payout._run_cycle()` reads the wallet list and each wallet inside the
  payout transaction, so a concurrent catch/daily is reflected and a
  deleted/recreated wallet is taxed under its live id.

**Audit fixes (all 11).**
1. Payout tax reads fresh wallet rows inside the transaction.
2. Deleted wallets are never resurrected: `debit_individual()` and
   `credit_individual_strict()` use `find_individual()`; `/individual delete`
   is refused while a blackjack/poker hand is open; settlement re-reads the row.
3. Draw approval re-reads kingdom membership, current leader and treasury and
   marks an ex-member request `invalid`.
4. Contract quota is enforced inside `create_contract(quota_limit=...)`.
5. `Contract._approve_checked()` re-reads status, owner balance and Union
   treasury inside the approval transaction.
6. `/bank setbalance` re-reads the balance and logs the fresh delta.
7. All remaining draw/creation status writes use the guarded claim helpers.
8. `case_counters_global` reserves globally unique case IDs per tag, so two
   guilds with the same tag no longer collide.
9. `poker.distribute()` refunds an uncalled bet; `raise_locked` stops a short
   all-in reopening raising for seats that already acted.
10. `/decree info` is guild-only and `get_settings(None)` no longer creates a
    ghost settings row.
11. The check runner requires a summary line; bare exits and `ERROR:` output are
    failures, and every check exits non-zero on failure.

Files: `config.py`, `coinflip.py`, `allin.py`, `gambling_ui.py`, `poker.py`,
`casino.py`, `database.py`, `creation.py`, `cogs/payout.py`, `cogs/bank.py`,
`cogs/contract.py`, `cogs/individual.py`, `cogs/games.py`, `cogs/decree.py`,
`checks/run_all.py`, `checks/check_coinflip.py`, `checks/check_allin.py`,
`checks/check_tax_cycle.py`, `checks/check_contracts.py`, `checks/check_casino.py`,
`checks/check_poker.py`, `checks/check_database.py`, `checks/check_transactions.py`,
`checks/check_transfers.py`, `checks/check_logic.py`, `checks/check_creation_race.py`,
new `checks/check_bank.py`, new `checks/check_runner.py`, plus `README.md`,
`AGENTS.md`, `BUG-FIXES.md` and `cogs/README.md`. Verification:
`checks/run_all.py` **16/16**, `compileall`/`pyflakes` clean, command count
unchanged at **104 / 14 cogs**. The deployed tree was re-pulled to
`/tmp/deployed_20261027-132619` and passed **16/16** against that copy; a
read-only live-DB copy migrated cleanly (`integrity: ok`, `case_counters_global`
present, no duplicate case IDs). **Restart still required** from the hosting
panel; the running process still serves the old odds and natural-only tax until
then, and this upload is the one that creates `case_counters_global`.

### 2026-10-27 — Fishing Guild and `/fish` (uploaded 2026-10-27 03:39 UTC, backup stamp `20261027-033920`)

**Feature.** `/fish` is a public catch: one random fish worth **20–100
credits**, paid into the caller's individual wallet from the new **Fishing
Guild** house balance. `/fishingguild balance` is public; `/fishingguild fund`
and `/fishingguild withdraw` are admin-only, mirroring the Casino. The guild
starts empty and a catch moves credits from it rather than minting them. The
catch is refused before any money moves when the guild cannot cover it or the
100M individual cap would be crossed, and it re-reads the wallet and guild under
the transaction lock. Both sides are logged as `fishing_catch` rows.

Files: `fishing.py` (new), `cogs/fishing.py` (new), `checks/check_fishing.py`
(new), `database.py` (`fishing_guild_account` plus guarded accessors),
`config.py`, `bot.py`, `cogs/ledger.py`, `checks/run_all.py`,
`checks/check_cogs.py`, `README.md`, `AGENTS.md`, `cogs/README.md`,
`BUG-FIXES.md`. Verification: `checks/run_all.py` **14/14**,
`compileall`/`pyflakes` clean, command count **104 / 14 cogs**, with checks for
the random 20–100 range, pure cap/short-guild refusals, guarded guild balance,
end-to-end catch, both ledger sides, conservation, no-wallet handling and the
admin taps. A follow-up bug test ran **10 concurrent catches against a
100-credit guild**: exactly five paid, five were refused, the guild ended at 0,
the players held 100, and the 10 ledger rows netted to zero. Boundary checks
(exact cap/guild cover allowed, one credit over refused) and a live-DB migration
copy (`fishing_guild_account` created, integrity `ok`, balances unchanged) also
passed. **The live process restarted after the upload**: `fishing_guild_account`
is present and the guild was funded with 1,000,000. Live data shows ten `/fish`
catches totalling 813 credits, leaving the guild at **999,187**; both sides
reconcile and `poker_hands` is present. No restart remains. README/AGENTS were
refreshed for this close-out with backup `.bak-20261027-125500`.

### 2026-10-27 — `/poker` high table (uploaded 2026-10-27 02:24 UTC, backup stamp `20261027-022436`)

**Feature.** `/poker amount:` opens a public four-seat Texas Hold'em table:
the caller on seat 0 and three house bots on seats 1–3, each starting with the
buy-in. The player gets Fold, Check/Call, Raise (a modal for the total raise-to
amount) and All-In; bots answer and the board/log update. The buy-in is held by
the Casino and the player is paid their **remaining chip stack** at the end, so
a losing hand still returns chips left on the table. A 180-second timeout folds
an abandoned table. `poker_hands` records the held buy-in and startup refunds a
table cut short by a restart.

Files: `cogs/games.py`, `gambling_ui.py`, `casino.py`, `config.py`,
`database.py`, `bot.py`, `checks/check_casino.py`, `checks/check_cogs.py`,
`README.md`, `AGENTS.md`, `cogs/README.md`. Verification: `checks/run_all.py`
**13/13**, `compileall`/`pyflakes` clean, command count **99 / 13 cogs**, with
new tests for the public table, the buy-in hold, fold settlement, a full
bot-turn hand, elimination of its open row, ledger conservation, and restart
refunds. **Restart still required** from the hosting panel; this is the upload
that creates `poker_hands`.

### 2026-10-27 — Public gambling UI / spectator floor (uploaded 2026-10-27 01:49 UTC, backup stamp `20261027-014918`)

**UI only; no money rules changed.** New `gambling_ui.py` gives every public
wager a spectator-facing card: coinflip and all-in results now show a large
result, stake/return/net, player wallet and Casino stack; the all-in
confirmation shows the whole-wallet risk before the button; `/casino balance`
is a **House & Floor** dashboard listing the games and house rules.
Coinflip and all-in results seed 🎉/💀 reactions (when the bot has permission)
and every card invites spectators to react. Blackjack now lays the table out as
Dealer / Your Hand / Stake fields with a spectator line, and settles with the
same reaction bait. Reaction API calls run **after** the result response is
sent, so a slow reaction can never delay a blackjack button edit.
`/blackjack` and `/casino balance` stay public; the table
buttons remain restricted to the player.

Files: `gambling_ui.py` (new), `cogs/individual.py`, `cogs/games.py`,
`checks/check_coinflip.py`, `checks/check_allin.py`, `checks/check_casino.py`,
`README.md`, `cogs/README.md`, `AGENTS.md`. Verification: `checks/run_all.py`
**13/13**, `compileall`/`pyflakes` clean, plus new assertions that the public
cards expose "The Floor" and the Casino dashboard lists the games.
`cogs/games.py` was re-uploaded after the reaction-ordering fix with backup
`.bak-20261027-015723`, so a slow reaction cannot delay a blackjack button edit.
**At that time** the Texas Hold'em engine passed `checks/check_poker.py` (400
seeded hands) but had no `/poker` command or Discord UI; the 02:24 entry above
supersedes that status. Process restart still required.

### 2026-10-27 — Correction: contract minimum is 100 credits (uploaded 2026-10-27 01:28 UTC, backup stamp `20261027-012852`)

**Correction.** The minimum is **100 credits**, not 100 million; the maximum
remains **100,000,000 credits**. A new contract is therefore valid anywhere in
`100..100,000,000`, subject to the leverage ladder. The minimum is enforced by
`contract_limits.budget_allowed()`, `/contract request` and
`/contract approve`; existing below-100 rows are grandfathered for
claim/release. Gilbert's prompt and all docs were corrected.

Files: `config.py`, `contract_limits.py`, `cogs/contract.py`, `gilbert/ai.py`,
`gilbert/prompt.py`, `checks/check_logic.py`, `checks/check_gilbert.py`,
`README.md`, `AGENTS.md`, `BUG-FIXES.md`, `cogs/README.md`,
`gilbert/README.md`. Verification: `checks/run_all.py` **13/13**,
`compileall`/`pyflakes` clean. The live `.env` was rotated to the new
owner-supplied `AI_API_KEY` (backup `.env.bak-20261027-012852`); a test
completion returned HTTP 200. The `.env` itself is not in the repository or the
source upload. Process restart still required.

### 2026-10-27 — Interim exact-100M rule (uploaded 00:37, stamp `20261027-003702`)

This interim rule was superseded by the correction above. It briefly set the
minimum equal to the 100M maximum; no current code enforces exact-only.

### 2026-10-26 — Transaction/contract hardening, Gilbert diagnosis (uploaded 2026-10-27 00:24 UTC, backup stamp `20261027-002422`)

**Code and checks uploaded; process restart pending.** `Database` now serialises
autocommit reads/writes on `_tx_lock`, so a plain write can no longer commit
another task's open transaction; the transaction tests assert the rollback
survives. Contracts use the new guarded `transition_contract()` for
close/release/reject/reopen, a rejected case cannot be made claimable by
`/contract close`, claims re-read the status inside the transaction, and
contract/order/kit lookups are scoped to the interaction guild. Deleted
destinations raise `AccountMissing` instead of silently losing credits.
Blackjack no longer locks a view when settlement rolls back. New
`checks/check_contracts.py` and `checks/check_gilbert.py` cover the regressions;
the suite is now 13 scripts.

**Gilbert.** At the time of this upload the live `.env` `AI_API_KEY` was rejected
with HTTP 401, so every reply fell back. A replacement key was supplied and
verified later (HTTP 200). `AI_MODEL=deepseek-flash` is accepted by this endpoint;
the code still normalises the legacy name to `deepseek-chat` as the portable
default, logs model/URL/status (never the key), and defaults to `deepseek-chat`
when no model is configured. A valid key must be present in the panel/`.env`.

Verification: the uploaded tree was re-pulled into `/tmp/deployed_tree` and ran
`checks/run_all.py` **13/13**; `compileall` and `pyflakes` were clean, and the
live database read-only copy passes `integrity_check`/ledger reconciliation.
Restart the process from the hosting panel and replace `AI_API_KEY` to activate
the fixes/Gilbert.

### 2026-10-24 — Poker engine validated, no command yet (uploaded 2026-10-24 17:05 UTC; code stamp `20261024-165039`, docs stamp `20261024-170500`)

Work done on the engine only. **No `/poker` command exists**; nothing imports
`poker.py`, so this upload changes no behaviour and keeps the command count at
98 commands / 13 cogs.

* **New `poker.py`** — pure Texas Hold'em: card model, five-card scorer, best
  five of seven, side-pot `distribute()`, four-seat `Holdem` with blinds,
  betting rounds and bot policy.
* **New `checks/check_poker.py`**, added to `checks/run_all.py` (now 11 checks).
  It exercises the evaluator, side pots and odd chips, blind posting, the big
  blind's option, the flop reset, and 400 seeded hands with chips conserved.
* **`config.py`** gained the poker block: `POKER_BOT_NAMES`,
  `POKER_BLINDS_DIVISOR` (50), `POKER_MIN_STAKE` (200), `POKER_MAX_STAKE`
  (10,000,000), `POKER_TIMEOUT_SECONDS` (180), `POKER_FOLD_MARGIN`,
  `POKER_RAISE_STRENGTH`, `POKER_BLUFF_RATE` and `POKER_BOT_DELAY_SECONDS`.
  Unused until the command lands.
* **Two engine bugs fixed before any Discord code was written.** Preflop used to
  open via `_begin_street()`, which zeroes `current_bet` and every seat's
  `committed`: the blinds sat in the pot as dead money nobody had to match, and
  the small blind acted first. `_post_blinds()` now records the blind seats and
  the new `_start_preflop()` opens the round with `current_bet = max(committed)`
  and the seat left of the big blind acting first. Details and impact in
  `BUG-FIXES.md`.
* **A third reported "failure" was the check's own arithmetic**: the seven-card
  flush `A♠ J♠ 8♠ 5♠ 2♠ K♥ Q♦` was expected to score a king, which is not a
  spade. The engine was right; the expectation was corrected.

Verified: `checks/run_all.py` **11/11**, `compileall` and `pyflakes` clean,
then the deployed tree re-pulled and re-tested against the uploaded copy.
Uploads with per-file `.bak-` backups; new files had nothing to back up.

**Restart still pending** from the `/blackjack` upload in the previous entry.
This entry does not need one for poker (nothing loads it), but the earlier
`/casino` and `/blackjack` code is still inert until the panel restarts.

### 2026-10-24 — Casino house account and `/blackjack` (uploaded 2026-10-24 04:43 UTC, backup stamp `20261024-044331`)

**Feature.** `/blackjack amount:` deals one hand against a new **Casino** house
account. Any stake from 100 to 10,000,000, or `all` / `half` / `quarter`. The
player gets two cards and presses **Hit** or **Stand**; the dealer hits to 16 and
stands on all 17s including soft 17; a natural pays **3:2** and every other win
even money; a push returns the stake exactly. `blackjack.py` holds the rules as
pure functions (`card_value`, `hand_value`, `is_soft`, `is_blackjack`,
`dealer_should_hit`, `gross_return`, `max_gross`, `settle`) so every branch is
testable without Discord.

**Why a house account.** The user asked that gambling stop creating credits. The
old games paid wins with `credit_individual()` and burned losses, so the supply
drifted with luck. Now `casino.py` makes the Casino the counterparty for every
wager: `begin_wager()` moves the stake into the house, `finish_wager()` pays wins
out of it, and both sides get a ledger row (`account_type='casino'`). Coinflip
and `/allin` were rewired to it, so all three games are zero-sum and the supply
only changes with `/casino fund` or `/casino withdraw`.

**Guards.** `casino.wager_problem()` refuses a stake below the minimum, above the
balance, or whose payout would breach the individual cap. `casino.cover_problem()`
refuses a wager the house cannot pay, naming the shortfall, and `db.debit_casino()`
refuses to take the house below zero in the same `UPDATE` that spends it, so two
payouts cannot both spend the same credits. Blackjack reserves the **3:2 natural**
(`max_gross`) before dealing, because a natural is paid on the deal.

**Stake safety.** The stake is taken at the deal and held while the player
decides. A hand left for 120 seconds is settled at stand by `on_timeout`, and each
dealt hand is recorded in `blackjack_hands` so that `_refund_abandoned_blackjack_hands()`
can refund any hand a restart cut short - the buttons live in memory, so without
that row a restart would keep the stake. `db.close_blackjack_hand()` is a guarded
one-shot flip, so a timeout racing a button press pays only once.

**Public tables.** All gambling now answers in the channel: coinflip, the
`/allin` confirmation and the `/blackjack` table are non-ephemeral, so the draw
and both balances are visible. Only the buttons are limited to the player whose
hand it is (`interaction_check`).

**Bug found while wiring it.** `casino.finish_wager()` originally unpacked
`db.credit_individual_strict()` as `(balance, added)`, but it returns `(added,
balance)`. Every win reported the payout as the new balance. The smoke test
showed a wallet that was actually 6,000 reporting 2,000; `check_casino.py` now
asserts the exact post-settlement balances so the mistake cannot return.

**Files.** New `casino.py`, `blackjack.py`, `cogs/games.py` and
`checks/check_casino.py`. `database.py` gained `casino_account`,
`blackjack_hands` and their accessors; `config.py` the `BLACKJACK_*` block;
`cogs/individual.py` coinflip and `/allin` now settle through the Casino and post
publicly; `cogs/ledger.py` the 🃏/🏦 emoji; `bot.py` loads `cogs.games` and calls
the refund at startup; `checks/check_coinflip.py` and `checks/check_allin.py`
were updated for the two-row house ledger and fund the house before betting.

**Verified.** `python3 checks/run_all.py` -> **10/10 passed** (new
`check_casino.py`: 86 checks). `check_cogs.py` -> 13 cogs, 98 commands, ALL OK
(the pinned count moved 93 -> 98: `/blackjack`, the `/casino` group and its three
subcommands). `pyflakes` clean. Conservation is asserted over 200 consecutive
wagers and the house is checked to stay non-negative.

**Uploaded 2026-10-24 04:43 UTC.** Seventeen files went to the SFTP account
root, each byte-exact backup kept beside the original as
`<file>.bak-20261024-044331`: thirteen replacements (`AGENTS.md`,
`BUG-FIXES.md`, `README.md`, `bot.py`, `config.py`, `database.py`,
`cogs/README.md`, `cogs/individual.py`, `cogs/ledger.py`,
`checks/check_allin.py`, `checks/check_cogs.py`, `checks/check_coinflip.py`,
`checks/run_all.py`) and four new (`blackjack.py`, `casino.py`,
`cogs/games.py`, `checks/check_casino.py`). Every upload was re-read and
SHA-256 compared against the local file; the whole deployed tree was then
pulled again, `compileall` and `pyflakes` came back clean on it, and
`checks/run_all.py` passed **10/10** against that copy rather than the working
copy. Re-comparing all 45 tracked files afterwards reported 45 identical.

**Restart still pending.** SFTP gives no shell, so `/blackjack` and `/casino`
will not appear until the process is restarted from the hosting panel. The
`casino_account` and `blackjack_hands` tables are created by
`CREATE TABLE IF NOT EXISTS` at startup, so the restart also applies the
schema; until then the old build keeps running untouched.

### 2026-10-23 — `/allin` whole-wallet gamble (uploaded 2026-10-23 09:44 UTC, backup stamp `20261023-094442`)

**Feature.** `/allin number:` stakes the caller's whole individual wallet on one
draw. A win returns `n` times the stake, a loss empties the wallet.

**Odds.** The chance of winning is `1 in (1.2 x n)`, so `5` wins 1 in 6 and
`100` wins 1 in 120. The draw is integer, not floating point: `allin.draw_size()`
is `6n` equally likely outcomes and `allin.winning_slips()` is a constant `5`, so
`n = 5` is literally 5 chances in 30. `checks/check_allin.py` exhausts all `6n`
outcomes for every number from 2 to 100 and asserts the observed rate equals the
configured one.

**The edge is flat.** Expected return is `n x 1/(1.2n) = 1/1.2 = 5/6`, so the
house keeps exactly **16.67%** of everything staked whichever number is played.
That is a property of the "1.2x harder" rule, not a coincidence: the payout and
the difficulty scale together. `allin.house_edge()` returns `1 - 1/1.2` and
section 7 requires it to stay positive.

**Why `n = 1` is refused.** A 1x payout returns the stake and nothing more, so
the bet risks the whole wallet to gain zero. It is a strictly dominated wager,
so `allin.number_problem()` rejects it and the slash-command range starts at 2.

**Money handling.** `debit_individual()` and the payout credit share one
`db.transaction()`; one `individual_allin` ledger row carries the net change
(`+(n-1) x stake` on a win, `-stake` on a loss). `allin.stake_problem()` screens
the bet before drawing - minimum balance, overdraw, and a payout that would
breach the 100M cap - so a refusal never draws the RNG or moves credits. Because
the whole balance is at stake, the command asks for confirmation on an ephemeral
card, and re-reads the wallet when the button is pressed: if the balance has
fallen below the confirmed stake the bet is refused rather than silently
resized.

**Files.** New `allin.py` and `checks/check_allin.py`; `config.py` gained
`ALLIN_HARDNESS_NUMERATOR/DENOMINATOR`, `ALLIN_MIN_NUMBER`, `ALLIN_MAX_NUMBER`
and `ALLIN_MIN_BALANCE`; `cogs/individual.py` gained `AllinConfirm`,
`settle_allin()` and the `/allin` command; `cogs/ledger.py` gained the 🎰 emoji.

**Also fixed.** `checks/check_cogs.py` hung instead of failing when its command
count assertion tripped: the assertion ran before the database close and bot
close, so the aiosqlite worker thread stayed alive and the interpreter blocked
in shutdown for the full 600s subprocess timeout. The cleanup is now in a
`finally`. Verified by running a copy with a deliberately wrong count, which now
exits 1 in seconds. The pinned count moved 92 -> 93 for `/allin`.

**Verification.** `checks/run_all.py` - 9/9 pass, 12 cogs, **93** commands.

**Uploaded 2026-10-23 09:44 UTC.** Two new files (`allin.py`,
`checks/check_allin.py`) and nine replacements, each with a
`.bak-20261023-094442` copy beside it: `config.py`, `cogs/individual.py`,
`cogs/ledger.py`, `checks/run_all.py`, `checks/check_cogs.py`, `README.md`,
`AGENTS.md`, `BUG-FIXES.md`, `cogs/README.md`. Every upload was re-downloaded and
matched on SHA-256, then the complete deployed `.py` tree was pulled to a clean
directory and passed `compileall`, `pyflakes` and `checks/run_all.py` (46 files,
9/9 checks).

**Restart still pending.** SFTP gives no shell, so `/allin` will not appear in
Discord until the process is restarted from the hosting panel.

### 2026-10-23 — `/individual coinflip` (uploaded 2026-10-23 03:56 UTC, backup stamp `20261023-035552`)

**Feature.** Individuals can stake credits on a coin with
`/individual coinflip side: amount:`. Calling **heads** or **tails** picks the
face, `amount` accepts a number or `all` / `half` / `quarter` through the shared
`utils.parse_amount()`.

**Odds.** A player wins **49%** of flips and loses **51%**, and a win returns
**2x** the stake. The win rate is decided by `coinflip.resolve()` from the roll
alone: the called side only decides which face is displayed, so calling heads or
tails never changes the mathematics. Rolling below `config.COINFLIP_WIN_BP`
(4,900) wins; 4,900 and above loses. `checks/check_coinflip.py` exhausts all
10,000 possible rolls and asserts exactly 4,900 wins for both faces.

**Why the odds are not 50/50.** There is no second player, so a win creates the
stake and a loss destroys it. At an even 50/50 that is a zero-sum loop that
adds no risk; at 49/51 the drift is
`10,000 - 4,900 x 2 = 200 bp`, i.e. **2% of everything staked disappears**.
`coinflip.house_edge_bp()` exposes that number and section 7 requires it to stay
positive. The game is therefore a deliberate sink alongside the net worth tax.

**Money handling.** `debit_individual()` and the winnings credit run inside one
`db.transaction()` with a single `individual_coinflip` ledger row carrying the
net change (`+stake` on a win, `-stake` on a loss), so the row always matches the
wallet movement. `coinflip.stake_problem()` screens the stake *before* the flip:
minimum 100 credits, maximum 10,000,000, overdraw, and a win that would cross the
100M cap. Refusals never roll the coin and never touch the balance, which the
check asserts by watching the RNG.

**Files.** New `coinflip.py` (pure, no Discord) and
`checks/check_coinflip.py`; `config.py` gained `COINFLIP_WIN_BP`,
`COINFLIP_PAYOUT_MULTIPLIER`, `COINFLIP_MIN_STAKE` and
`COINFLIP_MAX_STAKE`; `cogs/individual.py` gained the command; `cogs/ledger.py`
gained the 🪙 emoji.

**Verification.** `checks/run_all.py` — 8/8 pass, 12 cogs, **92** commands.
`check_cogs.py` now asserts the command and cog counts instead of only printing
them (it previously claimed to assert and did not).

**Uploaded 2026-10-23 03:56 UTC.** Nine files went to the SFTP host root, each
with a `.bak-20261023-035552` copy beside it (`config.py`, `cogs/individual.py`,
`cogs/ledger.py`, `checks/run_all.py`, `checks/check_cogs.py`, `README.md`,
`AGENTS.md`, `BUG-FIXES.md`, `cogs/README.md`), plus two new files with no
backup: `coinflip.py` and `checks/check_coinflip.py`. Every uploaded file was
re-downloaded and matched on SHA-256, then the whole deployed `.py` tree was
pulled to a clean directory where `compileall`, `pyflakes` and
`checks/run_all.py` all passed (44 files, 8/8 checks).

A follow-up upload at 03:58 UTC (stamp `20261023-035810`) reordered one guard in
`coinflip.py` so a stake larger than the wallet is reported as insufficient
funds rather than as an oversized bet, and extended the check to cover both
messages. Both files hash-matched and the re-pulled tree again passed 8/8.

**Restart still pending.** SFTP gives no shell, so `/individual coinflip` will
not appear in Discord until the process is restarted from the hosting panel.

### 2026-10-22 — Individuals can move money; 5% net worth tax per cycle (uploaded 2026-10-23 02:21 UTC, backup stamp `20261023-022154`)

**Individual transfers.** `/individual transfer member: amount:` moves credits
to another member's wallet and `/individual pay account: amount:` moves them
into a nation or kingdom treasury. Both accept `all` / `half` / `quarter` via the
shared `utils.parse_amount()`. The source debit, the destination credit and both
ledger rows share one `db.transaction()`, so a refused transfer changes nothing.
`/individual pay` also runs `commit_balance_to_union()` on the destination, so an
active commitment behaves exactly as it does for `/bank transfer`.

**Cap on transfers.** `credit_individual()` trims, which is right for a faucet
and wrong for a transfer: trimming would delete credits the sender has already
been debited for. New `Database.credit_individual_strict()` refuses instead and
raises `CapExceeded(balance, amount, room)`; the command reports the remaining
room and transfers nothing. Bots and self-transfers are rejected up front.

**5% net worth tax.** `config.INDIVIDUAL_TAX_BPS = 500` (5%, integer basis
points) with `payouts.individual_tax(balance)` doing the floor-to-whole-credit
maths. `Payout._run_cycle()` charges every wallet inside the payout transaction
via the new `Database.tax_individual()`, which debits the wallet, credits the
destination and writes both `individual_tax` ledger rows. The cycle now returns
an `individuals` section that `_post_summary()` renders as a "Net Worth Tax"
field. `config.INDIVIDUAL_TAX_ENABLED` and `INDIVIDUAL_TAX_BPS` can pause it;
`config.INDIVIDUAL_TAX_DESTINATION` chooses `"union"` (default, keeps the credits
in the economy and lets them fund contracts) or `"burn"` (an inflation sink).

**Scope decisions.** The tax is charged on the wallet's **whole** balance and
only on **natural** cycles: `/payout run` skips it deliberately, so an admin
cannot drain every wallet by repeating the manual run. **Nations and kingdoms are
not taxed by this rule and have no balance cap** — only their Union dues apply,
and only to members. The cycle no longer bails out when a guild has individuals
but no nations/kingdoms, which previously left those wallets untaxed forever.

**Transaction isolation defect fixed (high severity).** `Database.transaction()`
tracked depth in process-wide `_tx_depth` / `_in_tx` fields. Because every
statement awaits, two tasks interleaved inside what each believed was its own
transaction: a second task's `async with db.transaction()` silently *joined* the
open one, and its work was committed or rolled back with the stranger's block.
Reproduced before the fix — a task that committed 500 credits lost them when a
concurrent task raised, with no error surfaced. The depth counter is now a
`ContextVar` (per task) and `_in_tx` is a property over it, so nested blocks in
the same task still join, while a different task waits on `_tx_lock` and gets its
own `BEGIN IMMEDIATE`. This also closes the `_in_tx` loophole in
`commit_balance_to_union()`'s guard, which another task's transaction could
satisfy.

**Verified** on throwaway SQLite databases with real command handlers (the
cog methods against a fake interaction): transfer debits/credits and nets the
ledger to zero; over-balance, self-transfer and no-wallet paths are refused with
the sender unchanged; `half` resolves against the live balance; a recipient at
the 100M cap is refused and the sender is not debited; `/individual pay` credits
the nation and writes both rows; a nation balance above 100M is accepted, proving
treasuries are uncapped. Cycle checks: 10,000 pays 500 and lands in the Union;
19 pays 0; empty wallets are skipped; a second cycle taxes the reduced balance
(475); a manual run charges nothing and leaves balances alone; a nation gains its
payout and produces no tax row; tax rows net to zero; the disable switch charges
nothing. Transaction checks: a committed task survives another task's rollback,
a rolled-back task leaves nothing, an independent plain write survives another
task's rollback (this was the failing check in the old suite), nested blocks join
the outer transaction, and an inner failure rolls the outer back. `compileall`,
`pyflakes` and the cog-load smoke test are clean at 91 commands (was 89).
`check_db.py`'s previously failing isolation check now passes.

**Verification is now part of the repository.** The ad-hoc suites used by
earlier passes lived in `/tmp` and vanished with the session, so nobody could
re-run them. They now live in `checks/` and ship with the app: seven standalone
scripts plus `checks/run_all.py`, which prints a PASS/FAIL table and exits
non-zero when anything fails. See section 5 for the file map and section 8 for
where the suite sits in the deployment checklist. Run it before every upload and
again against a pulled copy of the deployed tree; that second run is what catches
a file you edited but forgot to upload (it is how this deploy was validated).

**Uploaded 2026-10-23 02:21 UTC.** Eight files went to the SFTP host root, each
with a byte-exact `.bak-20261023-022154` backup beside it: `payouts.py`,
`config.py`, `database.py`, `cogs/payout.py`, `cogs/individual.py`, `README.md`,
`AGENTS.md`, `BUG-FIXES.md`. Every upload was SHA-256 compared with the local
file, and the deployed tree was then pulled back, compiled, linted and re-run
through the end-to-end, cycle and cog-load tests (12 cogs, 91 commands). No
schema change is needed — `individual_tax` is only a ledger `kind` — so the tax is
charged on the next natural cycle after a restart. **The bot process must be
restarted** for any of it to take effect; until then the live bot keeps the old
behaviour. To revert, copy each `.bak-20261023-022154` file back over its
original and restart.

### 2026-10-22 — Individual wallets, `/daily`, separate leaderboards; one-shot guard pass (uploaded 2026-10-22 17:18 UTC)

**New economy type.** `individuals` holds one wallet per member per guild; its
`id` is the ledger `account_id`. The balance is hard-capped at 100,000,000:
`credit_individual()` trims each credit to the remaining room and callers log
the amount actually added. `/individual delete` erases the wallet **and its
ledger rows** after a confirmation (the documented exception to the append-only
ledger). Individuals get no discounts and stay outside the shop and contract
economies.

**`/daily`.** `daily.py` is pure rules: 2,000 base, 100% start, +10 points per
consecutive claim to 400% (8,000), -20 points per missed day down to a 60% floor,
one claim per rolling 24 hours. `Individuals.claim_daily()` flips
`last_daily_at` with a guarded UPDATE, then credits and logs in the same
transaction.

**Leaderboards.** `/leaderboard nation|kingdom|individual` run three separate
queries and render three separate embeds; they are never combined.

**Bug pass.** Fixed: a non-`Forbidden` DM failure left a draw `pending` forever;
petition approval could found the account twice; contract approval was not a
guarded one-shot status; `release` and nation/kingdom deletion wrote their
ledger rows outside the transaction; two kit modals could beat the 30-day
cooldown; concurrent `/contract request` calls could collide on `case_id`. Each
entry is in `BUG-FIXES.md`.

**Verification.** `compileall`, `pyflakes`, cog-load smoke showing **89
commands** with no failures, 22/22 ad-hoc checks (`_bugcheck.py`), and the
migration against a copy of the live DB (`integrity_check` ok, `individuals`
present, 0 ledger mismatches, 228 existing rows preserved, daily claim and
erase round-trip, Union treasury unchanged).

**Open decision:** individuals earn but cannot spend; the shop and contracts
still accept nation/kingdom treasuries only. Full-price individual purchases are
not enabled (see `AGENTS.md`, "Open product decision").

**Uploaded** (backups `.bak-20261022-171846`, SHA-256 verified): `bot.py`,
`config.py`, `database.py`, `creation.py`, `daily.py` (new), `cogs/bank.py`,
`cogs/contract.py`, `cogs/individual.py` (new), `cogs/kingdom.py`,
`cogs/kits.py`, `cogs/leaderboard.py` (new), `cogs/nation.py`.
`cogs/individual.py` was re-uploaded at 17:20 and 18:08 after wording fixes
(backups `.bak-20261022-172024`, `.bak-20261022-180841`). The four documentation
files (`README.md`, `AGENTS.md`, `BUG-FIXES.md`, `cogs/README.md`) were uploaded
with the code, each with a timestamped backup. **A panel
restart is required**: the `individuals` table is created at startup and the
new slash commands sync after login.

### 2026-10-21 — Nation/kingdom `add` and `rename`; live-DB re-check (uploaded 2026-10-22 00:08 UTC)

**Admin `add`.** `/nation add name leader:` and `/kingdom add name leader:` found
an account immediately instead of waiting for a petition. They are admin-only,
accept an optional leader, run the same name checks as `request` (existing
account and pending petition), and credit the normal creation bonus. The
create + bonus + ledger row is now one helper, `creation.found_account()`, used
by both the shortcut and `CreationApprovalView._approve()`, so the two paths
cannot drift. It joins the caller's transaction (the approval path still flips
the petition status in the same unit of work).

**Admin `rename`.** `/nation rename` and `/kingdom rename` change the name only.
`Database.rename_account()` updates the row and the `contracts.nation_name`
snapshot for open (`negotiating`/`approved`) cases in one transaction; the cog
writes a `nation_rename` / `kingdom_rename` ledger row with the old name in the
note. Account ids, balances, leaders, member nations and Union rows are
untouched. Closed contracts keep the name they were argued under and
`transactions.account_name` is history, so the ledger explains itself without
rewriting the past. Names are trimmed, capped at 100 characters and checked
case-insensitively against both the account table and pending petitions.

**Live DB re-check (read-only copy).** `PRAGMA integrity_check` is `ok` and the
journal is `wal`. The copy has 8 nations, 3 kingdoms, 3 Union members, 4 active
commitments, 2 contracts and **225** ledger rows (the previous entry's 163 rows
predate the recent payouts). Every nation, every kingdom and the Union treasury
reconciles exactly against its signed `transactions.amount` rows:
**0 mismatches**. No negative balances, duplicate names, orphan members or
orphan commitments; every `nations.kingdom_id` points at a real kingdom. Union
treasury **15,597,545**; total money supply **23,625,292**. The four active
commitments belong to nations 3, 4 and 8 and kingdom 2, whose balances are all
0 after their last credit was swept, as designed.

**Verified locally.** `compileall` and `pyflakes` are clean, including the
`_smoke.py` unused imports. The cog-load smoke test registers **80 commands**
(10 groups; the four new leaves are `nation add`, `nation rename`,
`kingdom add`, `kingdom rename`) with no failed extensions. A temporary suite on
a copy of the live database passed 17/17: rename keeps id/balance/ledger sum and
logs the change, history keeps the old name, case-insensitive lookups see the
new name, open contracts follow a kingdom rename, direct founding credits
30,000/50,000 and reconciles, and a crash inside the outer approval transaction
rolls the account, bonus and ledger row back together.

**Uploaded.** Code and the three sub-documents were uploaded to the SFTP account
at **2026-10-22 00:08 UTC**: `creation.py`, `database.py`, `cogs/nation.py`,
`cogs/kingdom.py`, `AGENTS.md`, `BUG-FIXES.md` and `cogs/README.md`. Each file
was renamed to a timestamped backup (`<file>.bak-20261022-000851`) before the
put, re-downloaded and compared by SHA-256, and all seven matched. This README
and `AGENTS.md` were refreshed afterwards, each with its own timestamped
backup. `royal_bank.db` was not uploaded
and was never written to; `_smoke.py` is a local check and is not deployed.
`contract.py`, `bot.py` and the other deployed files were re-downloaded and are
byte-identical to this checkout: a full sweep shows **41/41 deployed files**
matching by SHA-256. **A process restart from the hosting panel is
required** before the four new commands appear in Discord; no schema migration
is involved, so rollback is just restoring the `.bak-20261022-000851` files and
restarting.

### 2026-10-21 — Session close-out: live-DB cross-check and approval-time donation (uploaded)

**Live DB cross-check.** A copy of `royal_bank.db` plus its `-wal`/`-shm` was
downloaded to a scratch directory (the live file was never written to).
`PRAGMA integrity_check` is `ok`, journal mode is `wal`. The union schema is
present (`union_account`, `union_members`, `union_commits`) and
`settings.union_commit_channel_id` exists but is still `NULL`, so
`/union commit` tells the member an admin must run `/union setchannel` first.
There are 8 nations, 3 kingdoms, 3 members, no commitments and 163 ledger rows.
Every nation, kingdom and the Union reconciles exactly against its transaction
rows; the treasury stands at **13,151,687**.

**Historical dues are pre-fix.** The only four `union_tax` rows are dated
2026-10-21 14:51 UTC and read "5% Union dues on a 20,000 payout" (1,000) and
"2% ... on a 80,125 payout" (1,602). They were written by the old code before
the 1.75%/2% whole-balance change was deployed. No payout has run since, so no
new-rate dues exist yet. They were left untouched: they are a correct record of
what the old rule charged. The next payout will charge 1.75%/2% of the
post-payout balance.

**Defect found and fixed.** `/union commit` only donated on the next credit, so
an approved commitment left the account's existing balance sitting in the bank
indefinitely. `UnionCommitApprovalView._finalize()` now calls
`Database.commit_balance_to_union()` inside the same transaction that flips the
request to `active`, and the approval embed reports the amount donated
immediately. Details in `BUG-FIXES.md` ("An approved commitment left the
existing balance in the bank").

**Second defect found and fixed.** `/bank setbalance` wrote the balance and its
ledger row in autocommit, unlike every other money path. It now uses one
transaction. Admin corrections still skip `commit_balance_to_union()`.

**Verified.** `compileall` and `pyflakes` clean; 10/10 cogs load and the tree
still lists 76 commands. Scratch tests against the DB copy covered dues
arithmetic (1.75% and 2% of the whole balance, dues above the payout, zero and
negative balances), the full commit lifecycle, inbound transfer and payout
donation, cancellation, contribution totals, ledger reconciliation, and the
approval path itself (existing balance donated, second decision refused, denial
moves nothing, empty balance is a no-op). **Uploaded to the live bot with
timestamped backups as commits `78fb499` and `02bd1e4`; a full sweep shows
40/40 repo files identical to live. A panel restart is required to activate
both.**

### 2026-10-21 — `/union commit`: admin-approved whole-balance donations (uploaded)

**New command.** `/union commit account:` asks an administrator to turn on a
standing commitment for a nation or kingdom. The request is posted to the
channel set with `/union setchannel` with Approve/Deny buttons; only a Discord
administrator can decide. Approval donates the balance the account already
holds, and from then on the account donates its **whole balance** to the Union
after **every credit**, not just the credit itself. `/union
uncommit` stops it (account leader or admin) and is also the escape hatch for a
pending request. A pending or active commitment is visible in `/union balance`.

**One transaction.** `Database.commit_balance_to_union()` debits the full
balance, credits the Union and writes both `union_commit` ledger rows inside
the caller's transaction. It refuses to run outside a transaction, so a crash
cannot move credits without a ledger trace. It is called after the credit in
`Payout._run_cycle()`, `Contract._pay_claim()`, `/bank deposit`,
`/bank transfer` (destination) and an approved `/bank draw` (nation). Payouts
charge Union dues first, then commit the remainder; the Union receives both.
`/bank setbalance` is an admin correction and deliberately does not trigger a
donation. The creation bonus cannot hit an active commitment because commits
are deleted with their account and IDs are never reused.

**Gilbert and contributions.** `union_contributions()` now separates `dues`,
`donations` and `committed`; `contract_context(..., union_committed=...)`
includes the commitment total in the line Gilbert sees, and the prompt still
uses it to thank the account.

**Verified** on a throwaway database: inactive and pending accounts are
untouched; one approval only (a second decision is refused); an active nation
at 1,000 that receives 500 donates 1,500; the kingdom path works; cancellation
stops later donations; re-requesting after cancellation returns to pending;
contributions stay split by source; Union grants are not counted; deleting the
account removes the commitment; calling the helper outside a transaction
raises. A dry run on a copy of the live database with commitments active gave
Commune Ouvrière 15,731 in dues then 883,184 committed, and Sovereign
Socialist Republic 119,782 in dues then 5,869,342 committed, with the Union
ledger reconciled at 20,046,433. The legacy 1,000,000 claim was paid, committed
straight back, and a second claim was refused. `compileall`, `pyflakes` and the
76-command cog-load smoke test are clean. **Uploaded to the live bot with
timestamped backups; a process restart is required to load it.**

### 2026-10-21 — Nation dues cut to 1.75%; Gilbert sees Union contributions

**Rates.** `config.UNION_TAX_BPS` replaces `UNION_TAX_PCT`: **175 basis points
(1.75%) for a nation** and **200 (2%) for a kingdom**. Basis points keep the
maths integer, so no float ever touches a balance. `payouts.union_dues(balance,
account_type)` owns the calculation and floors to whole credits;
`Payout._charge_union_dues()` and the `/union` embeds read the same source.
The whole-balance base from Update 1 is unchanged, as is the deliberate
behaviour where dues can exceed the payout. `config.union_tax_label()` formats
the rate for user-facing text ("1.75", "2").

**Gilbert sees contributions.** `Database.union_contributions(guild_id,
account_type, account_id)` sums the account's `union_tax` and `union_donate`
ledger rows and returns dues, donations and total. `contract_context(...,
union_dues_paid=..., union_donated=...)` adds a ground-truth line such as
"Contributions to the Union so far: 40,350 credits (35,350 in dues, 5,000 in
donations)", and rule 8 tells Gilbert to thank members for that specific
support. Contract grants paid *by* the Union are excluded.

**Verified** on a throwaway database: 175 bp/200 bp produce 17,500/20,000 on
1,000,000; 19 credits floors to 0; non-members are untouched; dues above the
payout are allowed; contributions separate dues, donations and Union grants;
the full cycle and Union ledger reconcile. A dry run on a copy of the live
database gave Commune Ouvrière 15,731 on 898,915 (1.75%) and Sovereign
Socialist Republic 119,782 on 5,989,124 (2%), with the Union ledger reconciled.
The legacy `UN-COMMUN-001` claim still paid its full 1,000,000 once and refused
a second claim. `compileall`, `pyflakes` and the 73-command cog-load smoke test
were clean at the time. **Uploaded to the live bot before the `/union commit`
work.**

### 2026-10-21 — Dues now tax the whole balance; Gilbert warmer to members

_Rates in this entry were superseded by the 1.75%/2% entry above; the
whole-balance base and wording about the base still apply._

**Dues base fixed (Update 1).** `Payout._charge_union_dues()` charged 5%/2% of
the payout amount. It now charges **5% of a nation's whole balance and 2% of a
kingdom's whole balance**, taken after the payout is credited and before the
cycle closes. At the time `config.UNION_TAX_PCT` was unchanged and only the
base moved; it has since been replaced by `UNION_TAX_BPS` in the entry above.
Example: a member nation at 1,000,000 that earns a 48,600 payout ends at
996,170 after 52,430 of dues. A member at 0 still pays 5% of the payout, so
nothing changes for new accounts. Ledger notes now record both the balance and
the payout. The change is inside the existing payout transaction, so the credit
and dues still cannot diverge.

**Gilbert and Union members.** `gilbert.prompt.GILBERT_SYSTEM_PROMPT` has a new
rule 8 ("Union Members Are Our Own People"): warmer tone, more patience and the
most generous compliant reading of the proposal, with no relaxation of public
benefit, budgets, tiers, quotas, inspections or the treasury limit.
`contract_context(..., union_member=...)` adds a ground-truth membership line;
`cogs/contract.py` passes `db.is_union_member(guild_id, owner_type, owner_id)`.

**Verified** on a throwaway SQLite database: member nation/kingdom dues match
`balance * pct // 100` on the post-credit balance, non-members are untouched,
a 19-credit balance floors to 0, the four `union_tax` ledger rows reconcile with
the treasury, payout summary balances match the ledger, and the member vs
non-member Gilbert contexts differ. `compileall`, `pyflakes` and the import +
cog-load smoke test (73 commands) are clean. **Not uploaded yet** — the SFTP
copy still has the payout-based dues.

### 2026-10-21 — Union release uploaded (restart pending)

The Union work was uploaded to the SFTP account at **2026-10-21 12:27 UTC**,
23 files, each preceded by a timestamped backup (`<file>.bak-20261021-122730`)
and verified by re-downloading and comparing SHA-256. `royal_bank.db` was not
uploaded and its remote mtime is unchanged (`2026-10-21T05:11:38`).

The code is on disk but **not active**: the process must be restarted from the
hosting panel, and the schema migrations (`union_account`, `union_members`,
`orders.union_discount`) only run at startup. After the restart the Union
treasury starts at **0**, so:

1. `/union fund <amount>` before any contract can be approved.
2. `UN-COMMUN-001` (1,000,000) cannot be claimed until the treasury covers it.

Rollback is a rename of the `.bak-20261021-122730` files back over the new ones
and a restart. Nothing in this release touches existing rows except adding the
`orders.union_discount` column with a default of 0.

### 2026-10-21 — Union treasury, membership and contract quota

_Historical: the 5%/2% payout-based dues in this entry were replaced by the
whole-balance 1.75%/2% rules at the top of this log. Read those first._

**Contract quota.** `config.CONTRACTS_PER_CYCLE` now allows **1** contract per
nation and **3** per kingdom in each natural payout cycle. `/contract request`
uses it instead of the old one-per-owner rule; `count_contracts_for_cycle`
already ignored cancelled and rejected cases.

**Union treasury (the anti-inflation change).** New `union_account` table, one
row per guild. Contract credits are no longer created on the spot:

* `/contract approve` refuses unless the treasury holds the whole budget.
* `_pay_claim()` debits the treasury (`debit_union`) in the same transaction as
  the tranche flag, the recipient credit and both ledger rows. A short
  treasury rolls the whole claim back, including the flag, so it can be retried.
* Inflows are only `/union fund` (admin), `/union donate` (nation/kingdom),
  member dues and admin-approved commitments. Legacy `UN-COMMUN-001`
  (1,000,000) now needs the treasury funded before it can be claimed.

**Union membership (`cogs/union.py`).** New `/union` group:
`balance`, `members`, `join`, `leave`, `donate`, `fund` [Admin]. Membership is
stored in `union_members` and removed automatically when the nation or kingdom
is deleted. Join/leave/donate are limited to the account's leader or an admin.

**Member discount.** `pricing.quote()` takes `member_items_total`: the part of
the cart from `Basic Building`, `Wood & Masonry` or `Food & Crops` gets 50% off
(`config.UNION_MEMBER_DISCOUNT`) *instead of* the treasury discount, so nothing
stacks past half price. Everything else keeps the normal 20%/25% and the
shulker fee still earns the treasury discount. `shop/checkout.py` computes the
eligible total for shop and kit purchases; `orders.union_discount` stores the
split and the order embed shows both discounts. Non-members are priced exactly
as before.

**Member dues.** On every payout run (natural and manual) member nations pay 5%
and member kingdoms 2% of their payout (`config.UNION_TAX_PCT`), floor-rounded,
moved into the Union treasury in the payout transaction and logged on both
sides as `union_tax`. Non-members pay nothing.

**Bugs fixed in this pass:**

* `/bank draw` approval was not atomic: two clicks that interleaved before the
  status write could move the money twice. The status flip now uses
  `claim_pending_draw()` inside the transfer transaction.
* `/bank transfer` wrote its two ledger rows outside the money transaction,
  violating the ledger invariant. Now one transaction.
* `/ledger view` and `/ledger all` accepted `limit: 50`, but Discord allows 25
  embed fields, so a large limit failed at send time. Capped at 25.
* `/kit create` published the kit before confirming; a deleted or forbidden kit
  channel raised and killed the command after the kit and cooldown were
  written. Channel and creator lookups now fail soft.
* `api.py` assumed a JSON object body and answered 500 for `[]` or a string;
  it now returns `400 invalid_json`. The retired contract routes also require
  the bearer token before answering `410`.

**Verified** against a copy of the live database (db + WAL) with temporary
suites, 29 checks total: ledger rollback, debit guard, transfer atomicity,
double redemption, claim idempotency, concurrent claims, 75%-until-release,
closed-contract claiming, draw approval race, Union credit/debit guards,
membership lifecycle and cleanup, member vs non-member pricing, mixed carts,
shulker-fee interaction, 5%/2% dues with integer flooring, a full payout cycle
with dues and Union-ledger reconciliation, contract quota, claim refusal and
midway rollback on a short treasury, donation atomicity, and the
`orders.union_discount` migration. `compileall`, `pyflakes` and the import +
cog-load smoke test (73 commands) are clean. The live database was only read;
the code was uploaded later the same day (see the release entry above).

### 2026-10-21 — contract economics rewrite (deployed)

**Contract leverage limits (Update 1).** New `contract_limits.py` with
`max_budget_for_funds()`, `budget_allowed()` and `limit_explanation()`.
`/contract approve` enforces an inclusive ladder: up to 10,000,000 credits at
3x the party's treasury, up to 20,000,000 at 2x, above that at 1x, hard cap
100,000,000. Gilbert receives the live ceiling in `contract_context()`.

**Contracts pay the contractor (Update 2).** Reversed the old model, which
debited the party and issued `UC-` / `UF-` keys. `approve` now only records the
25/75 split and the claim account; `/contract claim` pays the 25% (building)
after approval and the 75% (maintenance) after `/contract release`. Claims land
in `claim_account_type` / `claim_account_id`, defaulting to the contracting
party, selectable at approval with `claim_account:`. A persistent **Claim
Credits** button on the `/contract setboard` channel mirrors the command, and
both routes share `_pay_claim()`.

**No in-game contract currency (Update 3).** Removed `generate_contract_key`
and all key issuance. `/api/contract/preview` and `/api/contract/redeem` stay
registered but answer `410 no_in_game_currency`, so stale clients fail loudly
instead of minting credits. Legacy keyed rows are repaid through
`/contract claim`; one live case (`UN-COMMUN-001`) is still unclaimed at the
time of writing.

**Amount keywords (Update 4).** `utils.parse_amount()` accepts `all`, `half`
and `quarter` against the paying balance, in addition to plain and
comma-grouped integers. Fractions round down. Wired into `/bank transfer`,
`/bank draw` and `/bank withdraw`.

Bugs fixed in this pass:

* `reject` still refunded credits under the old charge-the-party arithmetic,
  which would have created money nobody deposited.
* Claim buttons were not re-registered in `cog_load()`, so they would have died
  on the first restart after posting.
* The contract embed needed the claim account's *name*, but embed builders are
  synchronous; the lookup moved to the callers and `_contract_embed()` takes an
  optional `claim_name`.

Verified locally (temporary SQLite scripts, not committed): ledger chain and
rollback, payout cycles, transfer routing, leverage-ladder boundaries, and
contract claim / double-claim / concurrent-claim / reject / legacy-refund
behaviour. The import and cog-load smoke test registers 66 commands.

Deployed over SFTP with per-file backups `.bak-20261021-043206`; every uploaded
file was re-downloaded and SHA-256 matched. The live database was only read,
never uploaded. **The bot must be restarted from the hosting panel** before the
new code and the new columns (`claim_account_type`, `claim_account_id`,
`upfront_claimed`, `final_claimed`, board IDs) take effect.
