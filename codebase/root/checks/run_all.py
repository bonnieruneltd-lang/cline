"""Run every check in this directory and summarise.

    python3 checks/run_all.py

Each check is a standalone script that builds throwaway SQLite databases in a
temp directory; none of them touch the live database, Discord or the network.
The suite has 19 scripts. A script passes only when it exits 0 **and** prints a
recognised summary (FAILURES/VERDICT/ALL OK); an exception printed without a
summary is a failure, not a pass. Run this before uploading and again against a
pulled copy of the deployed tree (see AGENTS.md, "Deploying").
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORDER = [
    "check_logic.py",
    "check_database.py",
    "check_transactions.py",
    "check_contracts.py",
    "check_transfers.py",
    "check_full_transfers.py",
    "check_bank.py",
    "check_tax_cycle.py",
    "check_factions.py",
    "check_coinflip.py",
    "check_allin.py",
    "check_casino.py",
    "check_fishing.py",
    "check_poker.py",
    "check_gilbert.py",
    "check_daily_sync.py",
    "check_cogs.py",
    "check_creation_race.py",
    "check_runner.py",
]
FAILURES = re.compile(r"^FAILURES:\s*(\d+)", re.MULTILINE)
VERDICT = re.compile(r"^VERDICT:\s*(\S+)", re.MULTILINE)


def summarise(name: str, out: str, code: int) -> tuple[bool, str]:
    fails = FAILURES.search(out)
    verdict = VERDICT.search(out)
    if fails:
        bad = int(fails.group(1))
        return bad == 0 and code == 0, f"{bad} failure(s)"
    if verdict:
        ok = verdict.group(1) == "ok"
        return ok and code == 0, f"verdict {verdict.group(1)}"
    if "ALL OK" in out:
        return code == 0, "ALL OK"
    # Exit 0 with no summary is exactly how an unexpected exception used to
    # masquerade as a pass. Never infer success from the exit code alone.
    return False, f"exit {code} (no summary line)"


def main() -> int:
    results = []
    for name in ORDER:
        path = HERE / name
        if not path.exists():
            results.append((name, False, "missing"))
            continue
        proc = subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True, timeout=600
        )
        ok, note = summarise(name, proc.stdout + proc.stderr, proc.returncode)
        results.append((name, ok, note))
        print(f"{'PASS' if ok else 'FAIL':4}  {name:26} {note}")

    bad = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    if bad:
        print("failing:", ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
