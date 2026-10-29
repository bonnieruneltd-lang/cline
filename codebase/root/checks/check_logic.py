"""Pure-math checks: payout formulas, pricing, amount parsing, tax label."""
import sys, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config, daily, payouts, utils, contract_limits
from shop import pricing

fails = []
def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
        print(f"FAIL  {label}  {detail}")
    else:
        print(f"ok    {label}")

# --- parse_amount -----------------------------------------------------------
check("parse all", utils.parse_amount("all", 100) == 100)
check("parse half", utils.parse_amount("half", 101) == 50)
check("parse quarter", utils.parse_amount("quarter", 103) == 25)
check("parse commas", utils.parse_amount("1,500", 5000) == 1500)
check("parse underscore", utils.parse_amount("1_500", 5000) == 1500)
for bad in ("0", "-5", "abc", ""):
    try:
        utils.parse_amount(bad, 100); check(f"reject {bad!r}", False)
    except ValueError:
        check(f"reject {bad!r}", True)
try:
    utils.parse_amount("all", 0); check("all on empty", False)
except ValueError: check("all on empty", True)

# --- daily ------------------------------------------------------------------
now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
o = daily.evaluate(None, 0, None, now)
check("first daily", (o.eligible, o.multiplier_bp, o.streak, o.payout) == (True, 10000, 1, 2000), str(o))
o = daily.evaluate(now - dt.timedelta(hours=23), 1, 10000, now)
check("cooldown blocks", not o.eligible and o.retry_in == 3600, str(o))
o = daily.evaluate(now - dt.timedelta(hours=24), 1, 10000, now)
check("24h consecutive +10", o.eligible and o.multiplier_bp == 11000 and o.streak == 2, str(o))
o = daily.evaluate(now - dt.timedelta(hours=47, minutes=59), 1, 10000, now)
check("47h59 consecutive", o.eligible and o.multiplier_bp == 11000 and o.missed_days == 0, str(o))
o = daily.evaluate(now - dt.timedelta(hours=48), 1, 10000, now)
check("48h = 1 missed", o.eligible and o.multiplier_bp == 8000 and o.missed_days == 1 and o.streak == 1, str(o))
o = daily.evaluate(now - dt.timedelta(days=4), 5, 10000, now)
check("4d = 3 missed -> floor", o.multiplier_bp == 6000 and o.missed_days == 3, str(o))
o = daily.evaluate(now - dt.timedelta(hours=24), 50, 39600, now)
check("cap 400%", o.multiplier_bp == config.DAILY_BP_CAP, str(o))
o = daily.evaluate(None, 0, None, now)
check("payout at 100%", o.payout == config.DAILY_BASE)

# --- pricing ----------------------------------------------------------------
q = pricing.quote(1000, 0, "nation")
check("nation 20%", q["final_total"] == 800 and q["treasury_discount"] == 200, str(q))
q = pricing.quote(1000, 0, "kingdom")
check("kingdom 25%", q["final_total"] == 750, str(q))
q = pricing.quote(1000, 0, "individual")
check("individual 0%", q["final_total"] == 1000 and q["discount"] == 0, str(q))
q = pricing.quote(1000, 21, "nation")
check("shulker fee", q["shulker_fee"] == 3000 and q["final_total"] == (4000 - 800), str(q))
q = pricing.quote(1000, 21, "nation", force_shulker=True)
check("force shulker idempotent", q["shulker_fee"] == 3000, str(q))
q = pricing.quote(1000, 0, "kingdom", member_items_total=400)
check("member 50% + treasury on rest", q["union_discount"] == 200 and q["treasury_discount"] == 150
      and q["final_total"] == 650, str(q))
q = pricing.quote(1000, 0, "nation", member_items_total=99999)
check("member total clamped", q["union_discount"] == 500, str(q))
check("slots ceil", pricing.compute_slots([("a", 65, 64), ("b", 1, 64)]) == 3)
check("bundle expands", pricing.expand_line("bundle:iron_armor_set", 2, 1)[0][1] == 2)
check("discount individual rule", pricing.discount_percent("individual") == 0)
check("union eligible cat", pricing.is_union_eligible("Food & Crops") and not pricing.is_union_eligible("Redstone"))
check("banned pattern", pricing.is_banned("minecraft:netherite_sword", set()))
check("banned exact", pricing.is_banned("minecraft:elytra", {"minecraft:elytra"}))

# --- contract limits --------------------------------------------------------
for funds, cap in [(1_000_000, 3_000_000), (4_000_000, 10_000_000), (6_000_000, 12_000_000),
                   (12_000_000, 20_000_000), (30_000_000, 30_000_000)]:
    got = contract_limits.max_budget_for_funds(funds)
    check(f"cap {funds}", got == min(cap, config.CONTRACT_MAX_CREDITS), f"got {got} want {cap}")
check("cap 0 funds", contract_limits.max_budget_for_funds(0) == 0)
check("cap absolute", contract_limits.max_budget_for_funds(10**12) == config.CONTRACT_MAX_CREDITS)
check("budget_allowed zero", not contract_limits.budget_allowed(0, 10**9))
check("budget_allowed 3x", contract_limits.budget_allowed(3_000_000, 1_000_000))
check("budget_allowed below minimum",
      not contract_limits.budget_allowed(config.CONTRACT_MIN_CREDITS - 1, 10**9))
check("budget_allowed exact minimum with funds", contract_limits.budget_allowed(
    config.CONTRACT_MIN_CREDITS, config.CONTRACT_MIN_CREDITS))
check("budget_allowed below minimum even with leverage", not contract_limits.budget_allowed(
    config.CONTRACT_MIN_CREDITS - 1, 10**12))

# --- payouts ----------------------------------------------------------------
check("nation base", payouts.nation_payout(0, 0, 0, 0).total == config.NATION_BASE)
check("kingdom base", payouts.kingdom_payout([]).total == config.KINGDOM_BASE)
check("dues nation 1.75%", payouts.union_dues(1_000_000, "nation") == 17_500)
check("dues kingdom 2%", payouts.union_dues(1_000_000, "kingdom") == 20_000)
check("dues zero bal", payouts.union_dues(0, "nation") == 0)
check("tax label", config.union_tax_label("nation") == "1.75" and config.union_tax_label("kingdom") == "2", config.union_tax_label("kingdom"))

print("\nFAILURES:", len(fails))
for f in fails:
    print(" -", f)
raise SystemExit(1 if fails else 0)
