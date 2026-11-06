# `forge-mod/` — Minecraft Forge companion (NOT IMPLEMENTED)

This folder is a **placeholder**. There are no Java sources, no `build.gradle`
and no `mods.toml` yet. Nothing here is buildable or installable.

**Target pinned 2026-11-06 by the owner: Minecraft 1.20.1 on Forge 47.x**
(the 1.20.1 line — 47.2.x/47.3.x are the concrete builds; NeoForge is *not*
used, and 1.20.1 is not a NeoForge target). Record any change to that pin here
before writing code, because it decides the Gradle plugin, the mappings
(`official` 1.20.1 vs Parchment) and every `ItemStack`/`CompoundTag` call.

The README describes the intended mod so the next developer knows what to
build and how it must talk to the bot.

---

## What the mod is for

The Discord bot owns the economy. Players browse the Royal Shop and spend
credits from a nation or kingdom treasury **in Discord**. The bot then gives
the buyer a single-use key. The Forge mod's job is to turn that key into
actual Minecraft items.

The mod is therefore a **redemption client**, not a shop UI and not an economy
store. It never reads or writes the bot's SQLite database.

Planned responsibilities:

1. Accept an `RC-XXXXXX-XXXXXX` shop key from a player in-game.
2. Show a confirmation screen listing the items and quantities before granting
   anything.
3. Call the bot API to redeem the key and receive the item list.
4. Give the items to the player, dropping any overflow safely.
5. ~~Preview and redeem Workers' Union contract keys (`UC-...` / `UF-...`).~~
   **Removed.** Contract payouts are Discord credits only; `/api/contract/preview`
   and `/api/contract/redeem` answer `410 no_in_game_currency` and legacy keys
   are inert. Do not build key-based contract redemption into the mod.
6. Cache nothing that would let a key be redeemed twice offline.

Out of scope:

* Creating nations, kingdoms or bank accounts.
* Linking a Discord account to a Minecraft account. The key is the credential;
  the UUID is recorded only at redemption.
* Letting players buy items directly in-game without a Discord order. The
  current design deliberately puts purchasing in Discord.

---

## Bot API contract

Base URL and port come from `API_HOST` / `API_PORT`. Every request must send:

```http
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
```

A missing or wrong token returns HTTP 401.

### `POST /api/redeem`

Redeems a shop order.

Request:

```json
{
  "redeem_key": "RC-AAAAAA-BBBBBB",
  "player_uuid": "00000000-0000-0000-0000-000000000000",
  "player_name": "Steve"
}
```

Success `200`:

```json
{
  "order_id": 12,
  "redeem_key": "RC-AAAAAA-BBBBBB",
  "buyer_id": 123456789012345678,
  "player_uuid": "00000000-0000-0000-0000-000000000000",
  "player_name": "Steve",
  "shulker_fee": 3000,
  "items": [
    { "key": "minecraft:diamond", "display_name": "Diamond", "quantity": 64 }
  ]
}
```

Errors:

| Status | `error` | Meaning |
|---|---|---|
| 400 | `missing_fields` | Key or UUID missing |
| 401 | `unauthorized` | Bad/missing bearer token |
| 404 | `not_found` | No order with that key |
| 409 | `already_redeemed` | Key already used; response includes `redeemed_uuid` |

Redemption is atomic. If two clients race, only one gets `200`; the other gets
`409`.

### `POST /api/contract/preview` and `POST /api/contract/redeem` — **RETIRED**

Both routes are still registered, and both answer **`410 no_in_game_currency`**:

```json
{ "error": "no_in_game_currency",
  "detail": "Union contracts are paid as Discord credits. Use /contract claim." }
```

Workers' Union and Crown contracts pay **Discord credits only** — the 25% upfront
and 75% completion tranches land in the contract's claim account through
`/contract claim` (or the Claim Credit button). There is no in-game currency for
contracts and no contract keys: `UC-…`/`UF-…` keys do not exist any more, and any
old one is inert. **Do not build contract-key redemption into the mod**, and do
not reintroduce a key format for contracts. (This section used to document a 200
response and a "marks the key used" flow; that described the retired v1 API and
was corrected on 2026-11-06.)

---

## Verified on 2026-11-06 (read this before writing code)

The bot side of `/api/redeem` was exercised end to end against a throwaway
database: a real `shop.checkout()` purchase wrote order `RC-866A14-8B891A`, and
the real `RedeemAPI` handler answered

```json
{ "order_id": 1, "redeem_key": "RC-866A14-8B891A", "buyer_id": 10,
  "player_uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "player_name": "Notch",
  "shulker_fee": 0,
  "items": [ { "key": "minecraft:dirt",  "display_name": "Dirt …", "quantity": 64 },
             { "key": "minecraft:stick", "display_name": "Stick",   "quantity": 128 } ] }
```

with `409 already_redeemed` (carrying `redeemed_uuid`) on a second call, `401`
on a bad token, `404` on an unknown key and `400` when `player_uuid` is missing.
The purchase was charged exactly once. **That payload is the whole contract
today** — `key`, `display_name`, `quantity`, and nothing else.

### The item keys you will actually receive

| Family | Count | What the mod must do |
|---|---|---|
| `minecraft:<id>` | 303 | vanilla 1.20.1 registry lookup, straight to `ItemStack` |
| `bundle:<set>` | 3 | **never reaches you** — `shop.checkout` expands bundles into their component `minecraft:<id>` keys at purchase |
| `enchant:<id>:<level>` | 8 | **not expanded and not described in the payload**: you must parse the key (`enchant:mending:1`) and build an enchanted book yourself |
| `backport:<id>` | 7 (27 rows flagged `is_backported`) | these do not exist in vanilla — the mod (or a datapack) has to provide them |

### Four gaps to close, in the order they block work

1. **No preview route.** `/api/redeem` marks the order `redeemed` in the same call
   that returns the items, so the "confirmation screen before granting" required
   by the constraints below cannot be built against it, and a player who
   disconnects after the POST loses the key without items. The bot needs a
   non-consuming `POST /api/preview` (same payload, no state change) — **not yet
   implemented**, see README section 9 item 10. Do not work around it by
   redeeming twice.
2. **No enchant/backport fields.** Either parse `enchant:<id>:<level>` in the mod
   or extend the payload with `enchant: {id, level}` and `backported: true`.
   Extending it is additive and safe for old clients.
3. **Reachability.** `API_HOST=0.0.0.0`, `API_PORT=8080` are set on the host, but
   port 8080 there is answered by the hosting panel's HTTPS proxy (probe returns
   the panel's `401 {"error":"The required authorization heads were not present"}`,
   not our `{"ok": true}` health body). The API is therefore not reachable from
   outside until a port/allocation is assigned; check the panel log for
   `Redemption API listening on …` first.
4. **No `max_stack` in the payload.** 1.20.1 lets you read
   `stack.getMaxStackSize()` from the item itself, so this is the least urgent —
   but overflow handling still has to be written and tested.

### Suggested build order (1.20.1 / Forge 47.x)

1. Gradle MDK for 1.20.1 + `mods.toml`, mod id `royalcourt`, package
   `com.royalcourt.redeem`.
2. Config (`ForgeConfigSpec`): `apiBaseUrl`, `apiToken`, `timeoutSeconds`.
   Token from config only — never source, never logs.
3. `/redeem <key>` client command (or a key-entry GUI) that calls **preview**
   first and renders item name × quantity.
4. Confirm → `POST /api/redeem` → grant each stack **on the server thread**
   (`ServerLifecycleHooks`/`DistExecutor`), dropping overflow at the player's
   feet, then post a chat receipt.
5. Map `enchant:<id>:<level>` → `EnchantedBookItem` with a stored enchantment;
   map `backport:*` → whatever the mod registers.
6. Fail closed on every network error, and treat `409` as "already claimed by
   <uuid>" rather than an error.

---

## Design constraints for the implementation

* Keep the API token in the mod config, never in source or logs.
* Treat every key as opaque; never parse it to infer an order (`enchant:` keys
  are the one documented exception, because the payload carries no enchant data).
* Always show a confirmation screen with item names and quantities before
  granting items — which needs the `POST /api/preview` route above.
* Handle `409 already_redeemed` gracefully; it is a normal race, not a crash.
* If the API is unreachable, fail closed: do not grant items.
* Give items inside a server-thread task, never on the network thread.
* Players may disconnect during redemption; write a receipt before granting.

---

## Status checklist

| Item | Status |
|---|---|
| Forge/Minecraft version chosen | **DONE — Minecraft 1.20.1, Forge 47.x** (owner decision, 2026-11-06) |
| Gradle project scaffold | TODO |
| Config (API URL, token, timeout) | TODO |
| Shop key entry + confirmation GUI | TODO (blocked by the missing `POST /api/preview`) |
| `POST /api/redeem` client | TODO — contract verified, payload documented above |
| Item granting + overflow handling | TODO (plus the `enchant:`/`backport:` key mapping) |
| Contract preview/redeem client | **REMOVED** — both routes answer `410`; contracts pay Discord credits |
| In-game economy hook for contract amounts | **NOT PLANNED** — no in-game contract currency |
| Offline/retry safety | TODO |
| API reachable from the Minecraft server | **BLOCKED** — port 8080 is the panel proxy; needs an allocation |
