# `forge-mod/` — Minecraft Forge companion (IMPLEMENTED)

A **server-side Forge mod** for **Minecraft 1.20.1 on Forge 47.x** that redeems
Royal Court shop keys in-game. The sources live under
`src/main/java/com/royalcourt/redeem/`; `build.gradle`, `settings.gradle`,
`gradle.properties` and `src/main/resources/META-INF/mods.toml` build it into
`royalcourt-redeem-1.0.0.jar`.

**Target pinned 2026-11-06 by the owner: Minecraft 1.20.1 on Forge 47.x**
(the 1.20.1 line — 47.2.x/47.3.x are the concrete builds; NeoForge is *not*
used, and 1.20.1 is not a NeoForge target). Record any change to that pin here
before writing code, because it decides the Gradle plugin, the mappings
(`official` 1.20.1 vs Parchment) and every `ItemStack`/`CompoundTag` call.

The README describes the intended mod so the next developer knows what to
build and how it must talk to the bot.

---

## Security gate: the admin must enter the server IP first

**The mod cannot claim a single order until a Discord admin registers the
Minecraft server's IP.** This is enforced on the bot, not the mod, so a modified
client cannot bypass it:

1. An admin runs `/server setip ip:<public-ip>` in Discord (Administrator only).
   `/server show` reports the current value; `/server clear` removes it.
2. Every `/api/preview` and `/api/redeem` call then checks the **connecting
   address** against that stored IP (behind the panel proxy it reads the first
   hop of `X-Forwarded-For`). A mismatch is `403 ip_not_allowed`.
3. With no IP stored, every redemption is `403 server_not_registered` and the
   key is left untouched. A blocked attempt never consumes the order.

The mod sends its UUID/name and (optionally) its configured `serverIp` for
logging, but the bot trusts only the real socket/forwarded address.

## Build & install

```bash
cd forge-mod
./gradlew build            # needs JDK 17; produces build/libs/royalcourt-redeem-1.0.0.jar
```

If the Gradle wrapper is not present, use any Gradle 8.1.x–8.5 with the ForgeGradle
6 plugin (`gradle build`). Copy the jar into the server's `mods/` folder on
Minecraft 1.20.1 / Forge 47.x. The mod is **server-side**; clients do not need it.

## Configure

On first launch write `config/royalcourt-common.toml`:

```toml
[api]
    # Base URL of the bot API host — no /api suffix.
    baseUrl = "https://your-panel-host:8080"
    # The same value as API_TOKEN in the bot's environment. Never commit it.
    token = "paste-the-shared-token-here"
    timeoutSeconds = 15
    # Optional, for logs only; the bot checks the real connecting IP.
    serverIp = "203.0.113.7"
```

## In-game use

* `/redeem RC-XXXXXX-XXXXXX` — asks the bot to **preview** the order and prints
  the items with a green **[ Claim Now ]** button. Nothing is granted yet.
* `/redeem RC-XXXXXX-XXXXXX confirm` — asks the bot to **redeem** the key, then
  gives the items (dropping overflow at the player's feet) and posts a receipt.

Common answers: `403 server_not_registered` → an admin must run `/server setip`;
`403 ip_not_allowed` → this server is not the registered one; `409` → the key was
already claimed.


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

### Security gate — `403` before any item is granted

Every call below first looks up the order's guild and reads the IP an admin
registered with `/server setip`. It refuses **before** touching the order:

| Status | `error` | Meaning |
|---|---|---|
| 403 | `server_not_registered` | No admin has run `/server setip`; redemption is locked |
| 403 | `ip_not_allowed` | The connecting address is not the registered server IP |

The connecting address is the socket peer, or the first hop of
`X-Forwarded-For` when the panel proxy sits in front (set `API_TRUST_FORWARDED=0`
to ignore it). A refused request leaves the key `pending`, so it can be claimed
once the server is registered.

### `POST /api/preview`

Returns what an order would grant **without consuming the key** — the same auth,
payload and IP gate as `/api/redeem`. This is what `/redeem <key>` calls before
showing the Claim button.

Success `200` adds `"preview": true` to the redeem response shape (no
`redeemed_uuid` change, order stays `pending`). A redeemed key answers `409`; an
unknown key `404`.

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

`server_ip` is accepted as an optional echo for logs; it never replaces the gate.

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
| 403 | `server_not_registered` | No admin-registered server IP (see above) |
| 403 | `ip_not_allowed` | Caller is not the registered server IP |
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

### The four gaps, and how they are now closed

1. **No preview route.** **Fixed** — the bot now serves a non-consuming
   `POST /api/preview` (same auth, payload and IP gate as `/api/redeem`, no state
   change). The mod previews, shows the items and a Claim button, and only then
   calls `/api/redeem`. A player who disconnects after preview has lost nothing.
2. **No enchant/backport fields.** **Handled in the mod.** `enchant:<id>:<level>`
   is parsed into an `EnchantedBookItem` with a stored enchantment;
   `backport:<id>` is resolved against `minecraft:<id>` and then the raw id,
   and anything still missing is reported to the player instead of silently lost.
3. **Reachability.** Unchanged on the host side: port 8080 is the panel proxy,
   so the mod's `api.baseUrl` must point at a reachable allocation and the API
   log must show `Redemption API listening on …`. Until then the mod fails closed.
   When the proxy terminates TLS, it must pass `X-Forwarded-For` for the IP gate
   to see the Minecraft server (or set `API_TRUST_FORWARDED=0` for a direct bind).
4. **No `max_stack` in the payload.** **Not needed** — the mod reads
   `stack.getMaxStackSize()` and splits stacks itself, dropping overflow at the
   player's feet.

### Suggested build order (1.20.1 / Forge 47.x) — all done

1. Gradle MDK for 1.20.1 + `mods.toml`, mod id `royalcourt`, package
   `com.royalcourt.redeem`. **DONE**
2. Config (`ForgeConfigSpec`): `apiBaseUrl`, `apiToken`, `timeoutSeconds`.
   Token from config only — never source, never logs. **DONE**
3. `/redeem <key>` command that calls **preview** first and renders item name ×
   quantity with a Claim button. **DONE**
4. Confirm → `POST /api/redeem` → grant each stack **on the server thread**,
   dropping overflow at the player's feet, then post a chat receipt. **DONE**
5. Map `enchant:<id>:<level>` → `EnchantedBookItem` with a stored enchantment;
   map `backport:*` best-effort and report misses. **DONE**
6. Fail closed on every network error, and treat `409` as "already claimed".
   **DONE**


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
| Gradle project scaffold | **DONE** — ForgeGradle 6, `gradlew`, `build.gradle`; `gradle build` produced `royalcourt-redeem-1.0.0.jar` |
| Config (API URL, token, timeout) | **DONE** — `RedeemConfig` / `royalcourt-common.toml` |
| Shop key entry + confirmation | **DONE** — `/redeem <key>` preview list + clickable Claim, then `/redeem <key> confirm` |
| `POST /api/preview` (non-consuming) | **DONE** — bot route added |
| `POST /api/redeem` client | **DONE** |
| Item granting + overflow handling | **DONE** — `ItemGranting`, incl. `enchant:`/`backport:` mapping |
| Admin-entered server IP gate | **DONE** — `/server setip/show/clear`; API returns `403` and leaves the key pending |
| Contract preview/redeem client | **REMOVED** — both routes answer `410`; contracts pay Discord credits |
| In-game economy hook for contract amounts | **NOT PLANNED** — no in-game contract currency |
| Offline/retry safety | **DONE** — fails closed; nothing granted unless the API returns 200 |
| API reachable from the Minecraft server | **HOST CONFIG** — port 8080 is the panel proxy; set a reachable allocation and confirm `Redemption API listening on …` |

