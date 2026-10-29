# `forge-mod/` — Minecraft Forge companion (NOT IMPLEMENTED)

This folder is a **placeholder**. There are no Java sources, no `build.gradle`
and no `mods.toml` yet. Nothing here is buildable or installable.

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

### `POST /api/contract/preview`

Looks up a contract key without spending it.

Request:

```json
{ "redeem_key": "UC-AAAAAA-BBBBBB" }
```

Success `200`:

```json
{
  "ok": true,
  "case_id": "UN-RUNE-001",
  "nation": "Rune",
  "owner_type": "nation",
  "kind": "upfront",
  "amount": 250000,
  "redeemed": false,
  "eligible": true,
  "status": "approved",
  "items": []
}
```

`owner_type` is `nation` or `kingdom`. `kind` is `upfront` or `final`.
`eligible` is false when the key is not currently payable (not approved yet,
escrow not released, already redeemed, etc.).

### `POST /api/contract/redeem`

Spends a contract key.

Request:

```json
{
  "redeem_key": "UC-AAAAAA-BBBBBB",
  "player_uuid": "00000000-0000-0000-0000-000000000000",
  "confirm": true
}
```

`confirm` must be `true`; otherwise the API returns
`400 confirmation_required`. Success returns the same metadata as preview plus
`ok: true`.

Contract redemption currently returns **no items** and does not move credits in
the bot's database. It only marks the key used. How the in-game economy should
receive the `amount` is an open design decision. Do not assume the mod should
mint currency until that decision is written down here.

---

## Design constraints for the implementation

* Pick and document a Forge version and Minecraft version before writing code.
* Keep the API token in the mod config, never in source or logs.
* Treat every key as opaque; never parse it to infer an order.
* Always show a confirmation screen with item names and quantities before
  granting items.
* Handle `409 already_redeemed` gracefully; it is a normal race, not a crash.
* If the API is unreachable, fail closed: do not grant items.
* Give items inside a server-thread task, never on the network thread.
* Players may disconnect during redemption; write a receipt before granting.

---

## Status checklist

| Item | Status |
|---|---|
| Forge/Minecraft version chosen | TODO |
| Gradle project scaffold | TODO |
| Config (API URL, token, timeout) | TODO |
| Shop key entry + confirmation GUI | TODO |
| `POST /api/redeem` client | TODO |
| Item granting + overflow handling | TODO |
| Contract preview/redeem client | TODO |
| In-game economy hook for contract amounts | TODO (design undecided) |
| Offline/retry safety | TODO |
