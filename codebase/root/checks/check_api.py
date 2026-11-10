"""Redemption API security gate: the admin-registered Minecraft server IP.

The Forge mod must not be able to claim a shop order until an admin enters the
server's IP in Discord, and a request from any other address must be refused
without consuming the key. This script drives the real ``RedeemAPI`` handlers
against a throwaway SQLite database with a fake request, so the gate, the new
non-consuming ``/api/preview`` route and the legacy error codes are all
observed. It never starts a socket, talks to Discord or touches the live DB.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import serverip
from api import RedeemAPI
from database import Database

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


MODELLED_IP = "203.0.113.7"
OTHER_IP = "198.51.100.9"


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, *args, **kwargs):
        self.warnings.append(args)

    def info(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.logger = FakeLogger()


class FakeRequest:
    """The tiny surface ``RedeemAPI`` reads: headers, remote and json()."""

    def __init__(self, data, remote="", headers=None, raise_json=False):
        self._data = data
        self.remote = remote or None
        self.headers = headers or {}
        self._raise = raise_json

    async def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._data


def req(data, *, remote="", token="test-token", headers=None, raise_json=False):
    header_map = {}
    if token is not None:
        header_map["Authorization"] = f"Bearer {token}"
    if headers:
        header_map.update(headers)
    return FakeRequest(data, remote=remote, headers=header_map, raise_json=raise_json)


def payload(key, uuid="069a79f4-44e9-4726-a5be-fca90e38aaf5", name="Notch"):
    return {"redeem_key": key, "player_uuid": uuid, "player_name": name}


def body(response):
    try:
        return json.loads(response.text)
    except Exception:  # pragma: no cover - defensive
        return {}


async def make_order(db, key):
    oid = await db.create_order(
        guild_id=1, redeem_key=key, buyer_id=10, treasury_type="nation",
        treasury_id=1, items_total=500, shulker_fee=0, discount=0, final_total=500,
    )
    await db.add_order_item(oid, "minecraft:diamond", "Diamond", 64, 0)
    await db.add_order_item(oid, "minecraft:stick", "Stick", 128, 0)
    return oid


async def main():
    os.environ["API_TOKEN"] = "test-token"

    # -- pure helpers -----------------------------------------------------
    check("a plain IPv4 is valid", serverip.is_valid("203.0.113.7"))
    check("ip:port is valid", serverip.is_valid("203.0.113.7:25565"))
    check("an IPv6 address is valid", serverip.is_valid("2001:db8::1"))
    check("a hostname is not a valid IP", not serverip.is_valid("mc.example.com"))
    check("a hostname is detected", serverip.looks_like_hostname("mc.example.com"))
    check("normalise strips the port",
          serverip.normalise("203.0.113.7:51234") == "203.0.113.7",
          serverip.normalise("203.0.113.7:51234"))
    check("normalise unwraps a bracketed IPv6",
          serverip.normalise("[2001:db8::1]:25565") == "2001:db8::1",
          serverip.normalise("[2001:db8::1]:25565"))
    check("normalise maps IPv4-mapped IPv6",
          serverip.normalise("::ffff:203.0.113.7") == "203.0.113.7",
          serverip.normalise("::ffff:203.0.113.7"))

    db = Database(os.path.join(tempfile.mkdtemp(), "api.db"))
    await db.connect()
    try:
        service = RedeemAPI(FakeBot(db))

        # -- no IP registered: fail closed, key untouched -----------------
        order_id = await make_order(db, "RC-AAAAAA-000001")
        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000001"), remote=MODELLED_IP))
        check("redeem is refused before any IP is registered",
              resp.status == 403 and body(resp).get("error") == "server_not_registered",
              (resp.status, body(resp)))
        check("the refused key stays pending",
              (await db.get_order(order_id))["status"] == "pending")

        # -- register the IP, but call from somewhere else ----------------
        await db.set_server_ip(1, MODELLED_IP)
        settings = await db.get_settings(1)
        check("the admin IP is stored", settings["mc_server_ip"] == MODELLED_IP,
              dict(settings))
        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000001"), remote=OTHER_IP))
        check("redeem from a different IP is refused",
              resp.status == 403 and body(resp).get("error") == "ip_not_allowed",
              (resp.status, body(resp)))
        check("the wrong-IP attempt does not consume the key",
              (await db.get_order(order_id))["status"] == "pending")

        # -- preview is non-consuming ------------------------------------
        resp = await service.handle_preview(req(payload("RC-AAAAAA-000001"), remote=MODELLED_IP))
        preview = body(resp)
        check("preview from the registered IP succeeds", resp.status == 200, resp.status)
        check("preview is flagged and lists the items",
              preview.get("preview") is True
              and {(i["key"], i["quantity"]) for i in preview.get("items", [])}
              == {("minecraft:diamond", 64), ("minecraft:stick", 128)},
              preview)
        check("preview does not consume the key",
              (await db.get_order(order_id))["status"] == "pending")

        resp = await service.handle_preview(req(payload("RC-AAAAAA-000001"), remote=OTHER_IP))
        check("preview from a different IP is refused",
              resp.status == 403 and body(resp).get("error") == "ip_not_allowed",
              (resp.status, body(resp)))

        # -- redeem from the registered IP --------------------------------
        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000001"), remote=MODELLED_IP))
        redeemed = body(resp)
        check("redeem from the registered IP succeeds", resp.status == 200, (resp.status, redeemed))
        check("the order carries the redeemed UUID",
              (await db.get_order(order_id))["redeemed_uuid"]
              == "069a79f4-44e9-4726-a5be-fca90e38aaf5")
        check("the response lists the granted items",
              {(i["key"], i["quantity"]) for i in redeemed.get("items", [])}
              == {("minecraft:diamond", 64), ("minecraft:stick", 128)}, redeemed)

        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000001"), remote=MODELLED_IP))
        check("a second redeem reports already_redeemed",
              resp.status == 409 and body(resp).get("error") == "already_redeemed",
              (resp.status, body(resp)))

        # -- X-Forwarded-For (panel proxy) is honoured --------------------
        await make_order(db, "RC-AAAAAA-000002")
        resp = await service.handle_redeem(req(
            payload("RC-AAAAAA-000002"), remote="10.0.0.5",
            headers={"X-Forwarded-For": f"{MODELLED_IP}, 10.0.0.5"},
        ))
        check("X-Forwarded-For from the proxy is used for the gate",
              resp.status == 200, (resp.status, body(resp)))

        # -- ip:port and mapped forms match -------------------------------
        await make_order(db, "RC-AAAAAA-000003")
        resp = await service.handle_redeem(req(
            payload("RC-AAAAAA-000003"), remote=f"{MODELLED_IP}:60001"
        ))
        check("a socket address carrying a port still matches",
              resp.status == 200, (resp.status, body(resp)))

        resp = await service.handle_redeem(req(
            payload("RC-AAAAAA-000003"), remote="::ffff:203.0.113.7"
        ))
        check("an IPv4-mapped address still matches", resp.status == 409, resp.status)

        # -- token / field / lookup errors still behave -------------------
        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000001"), token="wrong"))
        check("a bad token is 401", resp.status == 401, resp.status)
        resp = await service.handle_redeem(req({"redeem_key": "", "player_uuid": ""}))
        check("missing fields is 400", resp.status == 400, resp.status)
        resp = await service.handle_redeem(req(payload("RC-NOPE00-000000"), remote=MODELLED_IP))
        check("an unknown key is 404", resp.status == 404, resp.status)
        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000002"), raise_json=True))
        check("invalid JSON is 400", resp.status == 400, resp.status)

        # -- clearing the IP disables redemption again --------------------
        await db.set_server_ip(1, None)
        await make_order(db, "RC-AAAAAA-000004")
        resp = await service.handle_redeem(req(payload("RC-AAAAAA-000004"), remote=MODELLED_IP))
        check("clearing the IP disables redemption",
              resp.status == 403 and body(resp).get("error") == "server_not_registered",
              (resp.status, body(resp)))
    finally:
        await db.close()

    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())

