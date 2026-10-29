"""The check runner must never turn an exception or a bare exit into a pass."""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("run_all", HERE / "run_all.py")
assert SPEC and SPEC.loader
RUN_ALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN_ALL)

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


check("FAILURES: 0 passes", RUN_ALL.summarise("x", "FAILURES: 0\n", 0)[0] is True)
check("FAILURES: 1 fails", RUN_ALL.summarise("x", "FAILURES: 1\n", 0)[0] is False)
check("VERDICT: ok passes", RUN_ALL.summarise("x", "VERDICT: ok\n", 0)[0] is True)
check("VERDICT: BUG fails", RUN_ALL.summarise("x", "VERDICT: BUG\n", 0)[0] is False)
check("ALL OK passes", RUN_ALL.summarise("x", "ALL OK\n", 0)[0] is True)
check("zero exit without a summary fails",
      RUN_ALL.summarise("x", "", 0)[0] is False)
check("ERROR without a summary fails",
      RUN_ALL.summarise("x", "ERROR: RuntimeError boom\n", 0)[0] is False)
check("summary with a non-zero exit fails",
      RUN_ALL.summarise("x", "FAILURES: 0\n", 1)[0] is False)

print("\nFAILURES:", len(fails))
for failure in fails:
    print(" -", failure)
raise SystemExit(1 if fails else 0)
