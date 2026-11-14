"""A/B/C policy evaluation on held-out scenarios (CASHGUARD_PLAN.md section 6.3).

    python scripts/evaluate.py            # demo company + 10 generated variants, 200 held-out paths each
    python scripts/evaluate.py --quick    # 2 variants, 60 paths, budget 60 (smoke run)

Writes frontend/public/eval.json (read by the Risk lab tab) and prints a summary table.
Synthetic data only; the numbers are frequencies under documented synthetic distributions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import CASHGUARD_DEMO_FIXTURE  # noqa: E402
from app.services.evaluation import fixture_from_store, run_evaluation  # noqa: E402
from app.workflows.seed import seed_demo  # noqa: E402
from app.workflows.store import Store  # noqa: E402


def _usd(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="small smoke run")
    ap.add_argument("--variants", type=int, default=10)
    ap.add_argument("--paths", type=int, default=200)
    ap.add_argument("--budget", type=int, default=120)
    ap.add_argument("--fixture", default=str(CASHGUARD_DEMO_FIXTURE))
    ap.add_argument("--out", default=str(ROOT / "frontend" / "public" / "eval.json"))
    args = ap.parse_args()
    if args.quick:
        args.variants, args.paths, args.budget = 2, 60, 60

    store = Store()
    seed_demo(store, args.fixture)
    base = fixture_from_store(store, "demo")
    result = run_evaluation(base, variants=args.variants, paths=args.paths, budget=args.budget,
                            progress=lambda line: print("  " + line, flush=True))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    s = result["summary"]
    print(f"\n{result['protocol']['fixtures']} fixtures x {args.paths} held-out paths, "
          f"search budget {args.budget}, {result['runtime_s']}s\n")
    print(f"{'policy':<5} {'breach':>7} {'missed':>7} {'mandatory':>9} {'cost':>9} {'worst short':>12} {'ms':>7}")
    for name, p in s["policies"].items():
        print(f"{name:<5} {p['mean_breach_rate']:>7.1%} {p['mean_missed_obligation_rate']:>7.1%} "
              f"{p['mean_missed_mandatory_rate']:>9.1%} {_usd(p['total_modeled_cost_cents']):>9} "
              f"{_usd(p['worst_shortfall_cents']):>12} {p['mean_runtime_ms']:>7.0f}")
    a, r = s["search"]["adaptive"], s["search"]["random"]
    print(f"\nsearch (equal budget): adaptive {a['unique_breaches']} unique breaching shocks, unfixable found in "
          f"{a['fixtures_with_unfixable_found']} fixtures; random {r['unique_breaches']} / "
          f"{r['fixtures_with_unfixable_found']}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
