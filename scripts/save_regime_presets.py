# -*- coding: utf-8 -*-
"""把三套状态策略(进攻/稳健/防守)从回测结果保存为命名预设 V4-切换-*。

用法: python -m scripts.save_regime_presets --results r1.jsonl r2.jsonl \
         --candidates R-REG-进攻,R-REG-稳健,R-REG-防守
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRESETS = ROOT / "data" / "strategy_presets.json"
OUT_NAMES = {"进攻": "V4-切换-进攻", "稳健": "V4-切换-稳健", "防守": "V4-切换-防守"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--candidates", default="R-REG-进攻,R-REG-稳健,R-REG-防守")
    args = ap.parse_args()

    from strategies.rotation_executor import resolve_rotation_params
    rows = {}
    for p in args.results:
        f = Path(p)
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[str(r.get("candidate"))] = r

    store = json.loads(PRESETS.read_text(encoding="utf-8")) if PRESETS.exists() else {}
    store.setdefault("presets", {})
    for cand in [x.strip() for x in args.candidates.split(",") if x.strip()]:
        r = rows.get(cand)
        if not r:
            print("MISS", cand)
            continue
        label = cand.split("-")[-1]
        name = OUT_NAMES.get(label, f"V4-切换-{label}")
        params = {k: v for k, v in (r.get("params") or {}).items()
                  if not str(k).startswith("_")}
        params = {k: v for k, v in resolve_rotation_params(
            params, use_live_preset=False).items() if not str(k).startswith("_")}
        params["universe_mode"] = "dynamic_etf"
        store["presets"][name] = params
        print(f"saved {name} <- {cand}: ret={r['total_return']*100:+.2f}% "
              f"dd={r['max_drawdown']*100:.2f}% sharpe={r['sharpe']:.2f}")
    PRESETS.write_text(json.dumps(store, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print("presets total:", len(store["presets"]))


if __name__ == "__main__":
    main()
