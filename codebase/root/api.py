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

import serverip


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
        # Behind the hosting panel's HTTPS proxy the socket address is the
        # proxy, not the Minecraft server. That proxy sets X-Forwarded-For with
        # the real client address, so trust it by default. Set
        # API_TRUST_FORWARDED=0 when the API is exposed directly.
        self.trust_forwarded = os.getenv("API_TRUST_FORWARDED", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        self.app = web.Application()
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_post("/api/preview", self.handle_preview)
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

    # -- server-IP allow-list ---------------------------------------------
    def _client_ip(self, request: web.Request) -> str:
        """Best-effort canonical address of the caller.

        Uses ``X-Forwarded-For`` (first hop) when the panel proxy sits in front
        of us, otherwise the socket address. Both are normalised so a value
        typed in Discord matches what arrives here.
        """
        if self.trust_forwarded:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return serverip.normalise(forwarded.split(",")[0])
        return serverip.normalise(getattr(request, "remote", "") or "")

    async def _server_ip_gate(self, request: web.Request, order) -> web.Response | None:
        """Refuse the request unless it comes from the admin-registered IP.

        Returns a 403 ``web.Response`` on failure, or ``None`` when the caller is
        allowed to proceed. This never mutates the order, so a blocked attempt
        leaves the key unclaimed.
        """
        settings = await self.bot.db.get_settings(order["guild_id"])
        registered = serverip.normalise(settings["mc_server_ip"] if settings else "")
        if not registered:
            return web.json_response(
                {
                    "error": "server_not_registered",
                    "detail": (
                        "No Minecraft server IP is registered. An admin must run "
                        "/server setip in Discord before orders can be redeemed."
                    ),
                },
                status=403,
            )
        client_ip = self._client_ip(request)
        if client_ip != registered:
            self.bot.logger.warning(
                "Redemption refused for order %s from %s (registered %s)",
                order["id"], client_ip or "<unknown>", registered,
            )
            return web.json_response(
                {
                    "error": "ip_not_allowed",
                    "client_ip": client_ip,
                    "detail": "Request did not come from the registered Minecraft server IP.",
                },
                status=403,
            )
        return None

    async def _resolve_order(self, request: web.Request):
        """Auth + parse + look up + gate. Returns ``(order, data)`` or a Response."""
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
        if not redeem_key or not player_uuid:
            return web.json_response({"error": "missing_fields"}, status=400)

        order = await self.bot.db.get_order_by_key(redeem_key)
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

        gate = await self._server_ip_gate(request, order)
        if gate is not None:
            return gate
        return order, data

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_preview(self, request: web.Request) -> web.Response:
        """Return what an order would grant, without consuming the key."""
        resolved = await self._resolve_order(request)
        if isinstance(resolved, web.Response):
            return resolved
        order, _data = resolved

        items = await self.bot.db.get_order_items(order["id"])
        return web.json_response(
            {
                "preview": True,
                "order_id": order["id"],
                "redeem_key": order["redeem_key"],
                "buyer_id": order["buyer_id"],
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

    async def handle_redeem(self, request: web.Request) -> web.Response:
        resolved = await self._resolve_order(request)
        if isinstance(resolved, web.Response):
            return resolved
        order, data = resolved

        db = self.bot.db
        player_uuid = str(data.get("player_uuid", "")).strip()
        player_name = str(data.get("player_name", "")).strip()

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
