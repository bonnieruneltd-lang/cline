# `cogs/` — Discord command groups

Each file is a `discord.ext.commands.Cog` loaded by `bot.py`. A cog owns one
slash-command group and all of its buttons, modals and views.

| File | Group | Responsibility |
|---|---|---|
| `bank.py` | `/bank` | Balances, admin deposit/withdraw, unified transfers, kingdom draw approvals |
| `transfers.py` | `/transfer` | One command for every credit direction between individuals, nations and kingdoms |
| `nation.py` | `/nation` | Nation petitions, leaders, worker/soldier stats, info/list |
| `kingdom.py` | `/kingdom` | Kingdom petitions, leaders, member nations, info/list |
| `payout.py` | `/payout` | Payout previews, the Cash Cycle, the Default Cycle, the natural-cycle trigger and the 14-day job |
| `decree.py` | `/decree` | Channel configuration, pending petitions, ratebook |
| `shop.py` | `/shop` | Royal Shop browsing, buying, orders, catalog admin |
| `kits.py` | `/kit` | Player-created kits and kit purchases |
| `ledger.py` | `/ledger` | Audit history and CSV export |
| `contract.py` | `/contract` | Gilbert and Albert negotiations, the contract lifecycle and tranche claims |
| `union.py` | `/faction` helpers | Union treasury, membership, donations and admin funding, reached through `/faction` |
| `factions.py` | `/faction` | The single faction command: allegiance, contract requests, commitments, donations and treasury actions |
| `individual.py` | `/individual`, `/daily`, `/allin` | Personal wallets, the daily allowance, coinflip, all-in, data erasure |
| `games.py` | `/blackjack`, `/poker`, `/casino` | The blackjack and poker tables plus the Casino house account |
| `fishing.py` | `/fish`, `/fishingguild` | Random 20–100 credit catches paid from the Fishing Guild house account |
| `leaderboard.py` | `/leaderboard` | Separate nation, kingdom and individual rankings |
| `__init__.py` | — | Package marker (no commands) |

---

## `bank.py`

Rules:

* The bank moves credits between nation treasuries, kingdom treasuries and
  individual wallets. The shared transfer core is `account_transfers.py`;
  `/bank transfer` and the top-level `/transfer` both use it.
* Anyone can transfer credits between any two economy accounts.
* You may only move money **out** of a treasury you lead or your own wallet,
  unless you are an admin.
* `deposit` / `withdraw` are admin-only because they cross the in-game /
  Discord boundary.
* A nation can draw from its kingdom, but the kingdom leader must approve the
  request through a DM with buttons.

Commands: `balance`, `deposit`, `withdraw`, `setbalance`, `transfer`, `draw`.

Key helper: `DrawApprovalView` handles the kingdom leader's DM. The transfer
permission check lives in `account_transfers.may_spend()` and is re-run inside
`transfer_funds()`.

---

## `transfers.py`

`/transfer` is a thin command over `account_transfers.transfer_funds()`. Its
source/destination autocomplete (`utils.any_account_autocomplete`) lists
nations, kingdoms and individual wallets. `/give` is the treasury-to-member
shortcut: `source` (a nation or kingdom), `member` and `amount`; it resolves the
member's wallet as the destination and writes `treasury_give` rows on both
sides. The core:

* resolves/refuses cross-guild accounts and same-account transfers;
* requires the actor to lead the source treasury or own the source wallet (an
  admin may move any account);
* debits with the balance guard, credits an individual with
  `credit_individual_strict` (cap refusal, never a trim), logs both sides, and
  runs the destination treasury's Union commitment hook — all in one
  transaction.

`/bank transfer` uses the same core and autocomplete, so there is only one
transfer SQL path. `/individual transfer`, `/individual pay` and `/give` also
call the core; they only keep their own reply wording. `/give` accepts only a
nation or kingdom source, so it can never spend from a member wallet.

---

## `nation.py`

A nation has exactly one leader. Admins create/delete nations and set the four
in-game stat counts that drive payouts (happy/regular workers and soldiers).

Commands: `request`, `add`, `rename`, `delete`, `setleader`, `stats`, `info`,
`list`.

Creation is not instant for players: `request` calls
`creation.submit_creation_request`, which posts an approval card to the
configured approvals channel. `add` is the admin shortcut: it founds the account
and credits the creation bonus through `creation.found_account()`, the same
helper the approval button uses. `rename` keeps the account id, balance and
history, logs `nation_rename`, and refreshes open contract names. `delete` calls
`Database.delete_account()`, which burns the remaining balance and writes a
negative `nation_delete` ledger row in the same transaction.

---

## `kingdom.py`

A kingdom is a separate account with its own Royal Bank balance, leader and up
to `config.MAX_NATIONS_PER_KINGDOM` (6) member nations.

Commands: `request`, `add`, `rename`, `delete`, `setleader`, `addnation`,
`removenation`, `info`, `list`.

`add` and `rename` work like their `/nation` counterparts; renames update the
`nation_name` snapshot on that kingdom's open contracts and leave the account
id, balance and member nations alone.

Deleting a kingdom releases its member nations back to independent status; it
does not delete the nations or their balances. The kingdom's own remaining
balance is burned and logged as a negative `kingdom_delete` row through
`Database.delete_account()`.

---

## `payout.py`

Three admin commands over one engine,
`_run_cycle(guild_id, natural=..., require_due=False)`:

* `cash` — the Cash Cycle. Pays nation/kingdom allowances, Union
  dues/commitments and the 5% individual net-worth tax. It does **not** call
  `advance_contract_cycle()` or `set_cycle_times()`, so the natural schedule and
  the contract cycle are untouched.
* `default` — the Default Cycle. Runs the full natural cycle in one execution:
  the same payments plus the contract-cycle advance and the next 14-day
  timestamp. Refuses when the next cycle is not due unless `force: True`.
* `trigger days:N` — queues the natural cycle now (`days:0`) or schedules it N
  days out. It pays nothing; the 10-minute task executes it.

The 14-day background job and `/payout default` call
`_run_cycle(natural=True, require_due=True)`, which repeats the due-check after
`_cycle_lock` is acquired; two simultaneous Default runs therefore pay once.
`force:true` bypasses the guard deliberately. Every run posts a summary and
writes every credit/tax row to the ledger. The formulas live in
`payouts.py`; the net-worth tax is `payouts.individual_tax()` plus
`Database.tax_individual()`. Crown members also pay `factions.apply_crown_tax()`:
100% of the regular-soldier line after the 30-day grace, into `crown_account`.
The natural cycle expires overdue Crown contracts before the next slot is
claimed.

Commands: `nation`, `kingdom`, `all`, `cash`, `default`, `trigger`.

---

## `decree.py`

Server configuration and rulebook display.

Commands: `setchannel`, `pending`, `rates`, `info`.

`setchannel` configures either the **cycle channel** (payout summaries) or the
**approvals channel** (nation/kingdom petitions). Payout rates and cycle length
are fixed in `config.py` for this edition.

---

## `shop.py`

The Royal Shop. Purchases are funded only from a nation or kingdom treasury.
Checkout writes an order and returns a single-use `RC-XXXXXX-XXXXXX` key, which
is DMed to the buyer:

Commands: `browse`, `item`, `buy`, `orders`, `order`, `ban`, `unban`,
`setprice`, `markredeemed`.

`markredeemed` is the manual fallback if the Forge mod is unavailable. It
records the Minecraft UUID and closes the order.

Pricing, discounts and packaging are in the `shop/` package. Do not duplicate
that math here.

---

## `kits.py`

Player-created public item bundles. One kit per player per 7 days, up to 10
distinct item types, always delivered in a shulker box (mandatory fee).

Commands: `create`, `list`, `view`, `buy`, `setchannel`, `resetcooldown`.

A kit purchase reuses the normal checkout pipeline, so treasury debits and
order keys behave exactly like a shop order.

---

## `ledger.py`

The audit view. Every money movement and stat change should already be in the
`transactions` table; this cog only reads and exports it.

Commands: `view`, `all`, `export`.

`export` produces a CSV attachment. Keep the column order stable so admins can
compare exports over time.

---

## `contract.py` — faction contracts (Gilbert and Albert)

This is the largest cog. It owns both representatives, the negotiation threads,
approvals, tranche grants and the claim board.

### Lifecycle

1. `/faction contract faction:<union|crown>` opens a private thread and mints a
   case ID (`UN-<TAG>-<NNN>` or `CR-<TAG>-<NNN>`). **No membership check and no
   dice roll.** Quota per natural cycle and per faction: one for a nation, three
   for a kingdom (`config.CONTRACTS_PER_CYCLE`). Numbers come from
   `case_counters_prefix`, one row per `(prefix, tag)`: a Crown case for a tag
   starts at `CR-<TAG>-001` even when Union cases exist; Union numbering
   continues from the legacy `case_counters` / `case_counters_global` floors.
2. Gilbert (Union) or Albert (Crown) replies to every human message in the
   thread using the AI endpoint. The first system message already contains the
   account's live numbers.
3. `/contract approve` checks the funding faction's treasury covers the budget,
   then marks the case approved and records the 25/75 split. **No credits move
   and nothing is debited from the party** — the faction funds the work.
4. `/contract claim` pays every eligible tranche from that faction's treasury
   into the claim account. After approval that is the 25% upfront; after release
   it is the 75%.
5. `/contract release` marks the case completed and unlocks the 75% for claim.
6. `/contract reject` stops all further payments. Claims already made are kept;
   unclaimed tranches are forfeited. A rejected case cannot be turned back into
   a claimable one by `/contract close` or `/contract reopen`.
7. `/contract close` archives an active negotiation (or a funded case); it
   refuses rejected, completed and cancelled cases. `/contract reopen` restores
   an approved, closed or completed case; funding is preserved and re-approval
   stays blocked. Rejected and cancelled cases are final. A closed approved case
   can still be released and claimed.

### Proposal wording

`proposal` is the only descriptive option on `/faction contract`; there is no
`item`, `quantity` or `project` field. A Union proposal is public work for
Gilbert. A Crown proposal is a supply offer for Albert — the goods, quantities
and collection point/timing are agreed in conversation and an officer collects
them in-game; the bot does not track item counts. The text is stored in the
legacy `contracts.project` column so old rows and backups stay readable; the
`project=` keyword remains accepted by `create_contract()` and both prompt
builders for one deploy window, then it can be deleted.

### Money rules

* A new contract must be between **1 and 100,000,000 credits**:
  `config.CONTRACT_MIN_CREDITS` is 1 (positive, no practical minimum) and
  `CONTRACT_MAX_CREDITS` is 100M. The party's treasury is the
  **leverage base**, never the payer. The leverage ladder
  (`contract_limits.py`) is 3x treasury up to 10M, 2x up to 20M, 1x above, with
  the 100M hard cap.
* `claim_account_type` / `claim_account_id` name who receives the credits.
  They default to the party; `approve` can point them at another nation or
  kingdom with `claim_account:`.
* Only the claim account's leader, the original requester or a Union Admin may
  claim.
* `_pay_claim()` is the only place contract credits move. It runs
  `claim_contract_tranche()` (atomic flag flip), `debit_union()`,
  `add_balance()` and both `log_tx()` calls inside one transaction, so a
  retried button or a race pays at most once and never mints credits. A short
  treasury raises `ClaimError` and rolls back the flag, so the claim can be
  retried after `/faction fund`. The claim re-reads the contract inside
  its transaction and refuses a rejected/cancelled case; every contract command
  is scoped to the guild that owns the case.
* The Union treasury is the sole source. `approve` refuses a budget the
  treasury cannot cover; the treasury itself only grows from
  `/faction fund`, `/faction donate`, member dues and
  admin-approved `/faction commitment` donations.
* Legacy rows (`redeem_key` set) were charged under the old model, so both
  tranches are claimable at once and repay the whole budget. Their old keys
  are inert; the API returns `410 no_in_game_currency`.

### Owner model

A contract has `owner_type` (`nation` or `kingdom`) and `owner_id`. The legacy
`nation_id`, `nation_name` and `nation_tag` columns are still populated:
for a kingdom contract `nation_id` is `0` and the name/tag columns carry the
kingdom's name and case-ID tag. Always use `_owner_type()` / `_owner_id()` /
`_owner_row()` rather than reading `nation_id` directly.

### Commands

`close`, `status`, `list`, `claim`, `approve`, `release`, `reject`, `reopen`,
`setchannel`, `setboard`, `setrole`.

`union.py` supplies the helpers reached by `/faction join`, `leave`, `donate`,
`fund` [Admin], `members`, `setchannel` [Admin] and `status`. `join`, `leave`
and `donate` accept the account's leader or an admin; `donate` uses
`utils.parse_amount`, so `all`/`half`/`quarter` work.

`/faction commitment` creates a pending row in
`union_commits` and posts Approve/Deny buttons to
`settings.union_commit_channel_id`; only an administrator can decide. That
channel must be set with `/faction setchannel` first — while it is
`NULL`, the command refuses and points the member at an admin. Approval donates
the balance the account already holds, and from then on every credit to the
account (payout, contract claim, deposit, incoming transfer, approved draw)
calls `Database.commit_balance_to_union()` in the same transaction, which
donates the **whole balance**. `/faction uncommit` cancels a
pending or active row. `/faction status` counts active commitments.

`/faction contracts faction:union` and `faction:crown` both list
contracts; `account:<...>` narrows the list to one nation or kingdom. Legacy
Crown supplier rows are still shown in `/faction status`; they are no longer
listed by `/faction contracts`.

`/faction contract faction:union` takes `account` and autocompletes both
nations and kingdoms. Only the leader of that account or a Union Admin may open
it; the negotiation opens directly, with no membership check and no roll.

### Claim board

`/contract setboard #channel` stores `contract_board_channel_id`. Each approval
posts an embed with a persistent **Claim Credits** button and stores
`board_channel_id` + `board_message_id` on the row. The embed shows the upfront
and completion state (`⏳`, `🔓`, `✅`) and the claim account. The board message
is edited after each claim and after release. `setboard` only affects contracts
approved afterwards; earlier ones can still be claimed with the command.

### Persistent views

`ContractCloseView` is re-registered for `negotiating` and `approved`
contracts on startup, and `ContractClaimView` for every contract with a board
message. Any new persistent button must also be re-registered in `cog_load()`,
otherwise it stops working after a restart.

---

## `factions.py` — factions, The Crown, Gilbert and Albert

The command surface over `factions.py`, `albert_prompt.py` and Gilbert's
prompts. A nation or kingdom belongs to at most one of the Workers' Union or
The Crown.

`/faction` is the one command group. Each action is a subcommand, so Discord
shows only the options that action needs:

* `/faction join` / `/faction leave` — swear allegiance, switch sides or leave
  the Union. Switching cancels the old side's active Union commitment and Crown
  contracts. The Crown has no soldier gate, and contracts never require
  membership; more soldiers improve the account's standing, not its access.
* `/faction contract` — the one contract request: `faction`, `account`,
  a free-text `proposal`, `budget`. **No faction membership is required and
  there is no dice roll, item, quantity or project field.** `faction:union`
  opens Gilbert's public-works negotiation; `faction:crown` opens Albert's
  supply proposal, whose goods an officer collects in-game, funded by the
  Crown treasury under the same 25/75 tranches. Quota is per faction (nation 1,
  kingdom 3 per natural cycle).
* `/faction commitment` / `/faction uncommit` — request or cancel the
  admin-approved whole-balance Union commitment.
* `/faction donate` — donate credits to the account's faction; use `amount` or
  `all`/`half`/`quarter`.
* `/faction deliver` — **legacy** Crown supplier delivery. A delivery fills
  the oldest active supplier contract for that item; payment happens only when
  the full quantity is delivered. Excess is an item donation. New Crown
  proposals are paid as project/supply contracts, not by item count.
* `/faction status` — Albert's account/Crown record, or the Union treasury
  dashboard.
* `/faction contracts` — contracts for the chosen faction, optionally
  filtered by nation/kingdom. Legacy Crown supplier rows are shown in
  `/faction status` instead.
* `/faction cancel` — cancel an active Crown supplier contract; no payment moves
  and delivered units are forfeited.
* `/faction fund` / `/faction withdraw` [Admin] — the only taps on the faction
  treasuries. Crown withdraw refuses to leave less than the value reserved by
  active contracts.
* `/faction members` — list Union members.
* `/faction setchannel` [Admin] — set the Union commitment approval channel.

Rules and data structures live in `factions.py`; see the root README section 7
and AGENTS.md. Never change a faction or contract row from this cog directly.

---

## `individual.py`

Personal wallets. One row per `(guild_id, discord_id)`, created on first use,
hard-capped at `config.INDIVIDUAL_MAX_CREDITS` (100,000,000).

Commands: `balance`, `transfer`, `pay`, `coinflip`, `delete`, `give` (admin),
plus the top-level `/daily` and `/allin`.

* Credits go through `Database.credit_individual()`, which trims to the
  remaining cap room and returns `(added, after)`; the ledger logs `added`.
  A transfer **into** a wallet uses `credit_individual_strict()` instead, which
  raises `CapExceeded` rather than trimming, because the sender is already debited.
* `transfer` and `pay` call `account_transfers.transfer_funds()`, the same core
  as `/transfer` and `/bank transfer`. It debits, credits, writes both ledger
  rows and (for a treasury destination) runs `commit_balance_to_union()` in one
  transaction. A member-to-member transfer can create the destination wallet on
  first receipt; a refused cap/overdraw rolls it back.
* `coinflip` stakes credits at a fixed **50% win rate** for a **2x** return. The
  winner comes from `coinflip.resolve()` and the roll alone — the player's call
  only picks the displayed face. At 50% and 2x the house edge is zero: a fair
  pass-through with the Casino funding wins from its own balance.
  `coinflip.stake_problem()` screens the stake before rolling (min 100, max 10M,
  overdraw, 100M cap room) so a refusal never touches money. The stake and the
  payout settle through the **Casino** (`casino.begin_wager()` /
  `casino.finish_wager()`) in one transaction, so a win is paid by the house
  instead of being minted.
* `/allin` bets the **whole wallet** on one draw and pays `n` times it, `n` being
  2–100. The chance is `1 in (1.1 x n)`, so the expected return is
  `1/1.1 = 10/11` whatever `n` is — a flat 9.09% house edge, and a higher `n` is
  only more variance. The draw is integer: 10 winning outcomes out of `11n`.
  `AllinConfirm` shows the stake, payout and chance first, and `settle_allin()`
  re-checks the balance when the button is pressed, refusing rather than resizing
  if it has fallen. `n = 1` is refused because a 1x payout returns only the
  stake.
* Both are **public**: the confirmation and the result are posted in the channel
  so the draw is watched. Only the confirm button is limited to the caller.
  `gambling_ui.py` renders large result cards, stake/return/net/wallet/house
  fields, a spectator prompt, and the bot seeds 🎉/💀 reactions on results.
* Every payout run (Cash and Default) charges each wallet a 5% net worth tax
  through `Database.tax_individual()`. `_run_cycle()` reads the wallet list and
  each wallet inside the transaction, so a deleted/recreated wallet is taxed
  under its live id. `/individual delete` is refused while a blackjack or poker
  hand is open, and settlement never recreates a deleted wallet.
* `/daily` uses `daily.evaluate()` and `claim_daily()`. The state flip, credit
  and ledger row share one transaction, and the flip is guarded on the
  `last_daily_at` that was read, so two claims cannot both pay.
* `/individual delete` erases the wallet **and all of its `transactions` rows**
  after a confirmation. This is the deliberate exception to the append-only
  ledger; do not reuse `delete_individual()` for anything else.
* Individuals receive no shop discount and no contract payout. They are a
  separate economy, not a treasury.

---

## `games.py`

The Casino. `/blackjack amount:` plays one hand, `/poker amount:` seats you at a
four-seat Texas Hold'em table, and the `/casino` group holds the house account:
`balance`, `fund` (admin), `withdraw` (admin).

* **The Casino is the counterparty for every wager in the bot.** `casino.py`
  moves the stake into the house and the payout out of it, both as ledger
  accounts, so gambling moves credits instead of creating them. The only taps
  are `/casino fund` and `/casino withdraw`.
* `BlackjackView` deals from a 6-deck shoe (`blackjack.new_shoe()`), shows one
  dealer card, and offers **Hit** and **Stand**. The rules are all in
  `blackjack.py` and none of them live in the view.
* The **stake is taken at the deal** and held while the player decides. The hand
  is written to `blackjack_hands` in that same transaction so a restart can find
  it: `bot._refund_abandoned_blackjack_hands()` runs at startup and refunds any
  hand still open. `on_timeout` (120s) settles an abandoned hand at stand.
* `settle_hand()` closes the hand through `Database.close_blackjack_hand()` — a
  guarded one-shot flip — **inside** the payout transaction, so a timeout racing
  a button press pays once. A second press gets a "hand closed" notice.
* Before dealing, `casino.cover_problem()` checks the house can pay
  `blackjack.max_gross(stake)`, the 3:2 natural, because a natural is paid on
  the deal. `casino.wager_problem()` checks the minimum, the player's balance and
  the 100M cap.
* The table is **public**: only `interaction_check` restricts the buttons to the
  player whose hand it is. `gambling_ui.py` adds a spectator line to the table,
  seeds 🎉/💀 reactions on settlement, and `/casino balance` is a public
  House & Floor dashboard listing the four games and the house rules.
* **Poker** `/poker amount:` deals a four-seat table: the player on seat 0 and
  three Minecraft-themed bot names on seats 1–3, each starting with the same
  stake. `poker_names.py` samples an AI-generated pool using Gilbert's
  `AI_API_KEY` (background refresh) and falls back to
  `config.POKER_BOT_NAME_POOL`. The buy-in is held through
  `casino.begin_wager()` and a `poker_hands` row; a restart closes the row and
  refunds the buy-in, because the table itself is in memory.
* `PokerTableView` offers Fold, Check/Call, Raise (a modal for the total
  raise-to amount) and All-In. The player acts first when the view is posted;
  bots answer synchronously and the log/board update. `interaction_check`
  restricts every button to the table owner, while `gambling_ui.poker_table()`
  shows only the player's hole cards to the channel.
* `settle_table()` closes `poker_hands` once, then calls
  `casino.finish_wager(..., payout_override=final_stack)`: poker returns the
  player's **remaining stack**, which can be positive even on a losing hand.
  `gambling_ui.poker_result()` reveals all hands and the winner; the 180s
  timeout folds the player and settles the hand.

---

## `fishing.py`

`/fish` is a public catch: one random fish from `config.FISHING_NAMES` worth an
inclusive **20–100 credits**. The payout is not minted. The cog debits
`fishing_guild_account` and credits the member's individual wallet in one
transaction, logging a `fishing_catch` row on both sides.

* The caller must already have an individual wallet (`/daily` creates one).
* `fishing.catch_problem()` checks the 100M individual cap and the Fishing Guild
  balance before the transaction; both are re-checked under the transaction lock
  before any credits move.
* The Fishing Guild starts empty. `/fishingguild balance` is public;
  `/fishingguild fund` and `/fishingguild withdraw` are admin-only and write
  `fishing_guild_fund` / `fishing_guild_withdraw` ledger rows.
* A short guild refuses the catch instead of paying credits it does not hold; a
  cap-breaking catch is refused before the guild is debited.

`checks/check_fishing.py` proves the range, the pure refusals, the guarded
balance, the end-to-end catch, both ledger sides, conservation, cap/short-guild
refusals and the admin taps.

---

## `leaderboard.py`

Three subcommands — `nation`, `kingdom`, `individual` — and three separate SQL
queries in `_QUERIES`. There is no cross-table ranking on purpose: a nation
treasury, a kingdom treasury and a personal wallet have different caps and
rules, so combining them would compare unlike balances.
