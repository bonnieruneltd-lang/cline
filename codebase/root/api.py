"""
api.py
======
HTTP redemption API for the Minecraft Forge mod.

The bot exposes a small aiohttp server so the Forge mod can redeem shop orders
in-game. Requests are authenticated with a shared bearer token (``API_TOKEN``).
No Minecraft account linking is used: the redeem key is the credential, and the
player UUID is only recorded after a successful redemption.
"""
from __future__ import annotations

import os

from aiohttp import web


class RedeemAPI:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.token = os.getenv("API_TOKEN", "").strip()
        self.host = os.getenv("API_HOST", "0.0.0.0").strip()
        try:
            self.port = int(os.getenv("API_PORT", "8080").strip())
        except ValueError:
            self.port = 8080

        self._runner: web.AppRunner | None = None
        self.app = web.Application()
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_post("/api/redeem", self.handle_redeem)
        self.app.router.add_post("/api/contract/preview", self.handle_contract_preview)
        self.app.router.add_post("/api/contract/redeem", self.handle_contract_redeem)

    # -- lifecycle ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def start(self) -> None:
        if not self.enabled:
            self.bot.logger.warning("API_TOKEN not set; redemption API disabled.")
            return
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await site.start()
        self.bot.logger.info("Redemption API listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- handlers ----------------------------------------------------------
    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            header = header[7:]
        return bool(self.token) and header == self.token

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_redeem(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "invalid_json"}, status=400)

        redeem_key = str(data.get("redeem_key", "")).strip().upper()
        player_uuid = str(data.get("player_uuid", "")).strip()
        player_name = str(data.get("player_name", "")).strip()

        if not redeem_key or not player_uuid:
            return web.json_response({"error": "missing_fields"}, status=400)

        db = self.bot.db
        order = await db.get_order_by_key(redeem_key)
        if order is None:
            return web.json_response({"error": "not_found"}, status=404)

        if order["status"] == "redeemed":
            return web.json_response(
                {
                    "error": "already_redeemed",
                    "order_id": order["id"],
                    "redeemed_uuid": order["redeemed_uuid"],
                },
                status=409,
            )

        items = await db.get_order_items(order["id"])
        if not await db.redeem_order(order["id"], player_uuid):
            # Lost the race with another redemption; report who actually got it.
            fresh = await db.get_order(order["id"])
            return web.json_response(
                {
                    "error": "already_redeemed",
                    "order_id": order["id"],
                    "redeemed_uuid": fresh["redeemed_uuid"] if fresh else order["redeemed_uuid"],
                },
                status=409,
            )

        return web.json_response(
            {
                "order_id": order["id"],
                "redeem_key": order["redeem_key"],
                "buyer_id": order["buyer_id"],
                "player_uuid": player_uuid,
                "player_name": player_name,
                "shulker_fee": order["shulker_fee"],
                "items": [
                    {
                        "key": it["item_key"],
                        "display_name": it["display_name"],
                        "quantity": it["quantity"],
                    }
                    for it in items
                ],
            }
        )

    # -- Workers' Union contract keys -------------------------------------
    # Contracts are paid in Discord credits only. There is no in-game currency:
    # the 25% upfront and 75% completion tranches are claimed with
    # /contract claim (or the Claim Credits button) and land in the treasury of
    # the account named on the contract. Old keys are inert; both routes stay
    # registered so an outdated client gets an explicit answer, not a 404.
    async def handle_contract_preview(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(
            {
                "error": "no_in_game_currency",
                "detail": "Union contracts are paid as Discord credits. Use /contract claim.",
            },
            status=410,
        )

    async def handle_contract_redeem(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(
            {
                "error": "no_in_game_currency",
                "detail": "Union contracts are paid as Discord credits. Use /contract claim.",
            },
            status=410,
        )
