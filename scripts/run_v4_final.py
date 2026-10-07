# -*- coding: utf-8 -*-
"""一键链式执行 GRIDV4 最终版(含债券/货币双过滤)：
A → B → C → 状态策略3组 → 统计分析 → 切换预设保存 → 切换验证(滞后1日)。

后台运行:
  Start-Process python -ArgumentList '-m','scripts.run_v4_final' ...
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V4 = ROOT / "data" / "backtest_grid" / "v4"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
LOG = Path(r"C:\Users\25898\AppData\Local\Temp\opencode\v4_final_chain.log")


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        try:
            print(line.encode("gbk", errors="replace").decode("gbk"), flush=True)
        except Exception:
            pass


def run(args, stage: str):
    """执行子步骤, 输出实时写入 v4_stage_<stage>.log(不捕获到内存)。"""
    log("RUN " + " ".join(args[1:]))
    stage_log = LOG.parent / f"v4_stage_{stage}.log"
    with open(stage_log, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"{' '.join(args)}\n")
        rc = subprocess.call([PY] + args, cwd=str(ROOT), stdout=fh,
                             stderr=subprocess.STDOUT)
    if rc != 0:
        log(f"FAILED rc={rc} (详见 {stage_log})")
        raise SystemExit(1)
    log(f"OK [{stage}]")


def make_regime_candidates():
    """从当前 V4-切换-* 预设生成三组待复核候选(R-REG-*)。"""
    store = json.loads((ROOT / "data" / "strategy_presets.json")
                       .read_text(encoding="utf-8")) or {}
    presets = store.get("presets") or {}
    out = []
    for label, name in (("进攻", "V4-切换-进攻"), ("稳健", "V4-切换-稳健"),
                        ("防守", "V4-切换-防守")):
        p = dict(presets.get(name) or {})
        p.pop("universe_mode", None)
        if p:
            out.append({"name": f"R-REG-{label}", "params": p, "stage": "REG"})
    path = V4 / "candidates_REG.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"regime candidates: {[x['name'] for x in out]}")


def main():
    log("=" * 70)
    log("GRIDV4 final chain start")
    A = V4 / "results_A_final.jsonl"
    B = V4 / "results_B_final.jsonl"
    C = V4 / "results_C_final.jsonl"
    R = V4 / "results_R2026_vol.jsonl"          # 已完成的2026复核(可复用)
    REG = V4 / "results_REG.jsonl"
    pairs = [
        (V4 / "candidates_A.json", A),
        (V4 / "candidates_B_final.json", B),
        (V4 / "candidates_C_final.json", C),
    ]
    # 阶段A(如已完成则跳过)
    if not A.exists() or sum(1 for _ in A.open(encoding="utf-8")) < 230:
        run(["-m", "scripts.grid_search_v4", "run", "--candidates",
             str(V4 / "candidates_A.json"), "--results", str(A),
             "--workers", "8", "--resume"], "A")
    run(["-m", "scripts.grid_search_v4", "gen-b", "--results", str(A),
         "--out", str(V4 / "candidates_B_final.json")], "genB")
    run(["-m", "scripts.grid_search_v4", "run", "--candidates",
         str(V4 / "candidates_B_final.json"), "--results", str(B),
         "--workers", "6", "--resume"], "B")
    run(["-m", "scripts.grid_search_v4", "gen-c", "--results", str(A), str(B),
         "--out", str(V4 / "candidates_C_final.json")], "genC")
    run(["-m", "scripts.grid_search_v4", "run", "--candidates",
         str(V4 / "candidates_C_final.json"), "--results", str(C),
         "--workers", "6", "--resume"], "C")
    make_regime_candidates()
    run(["-m", "scripts.grid_search_v4", "run", "--candidates",
         str(V4 / "candidates_REG.json"), "--results", str(REG),
         "--workers", "4", "--resume"], "REG")
    run(["-m", "scripts.analyze_grid_v4", "--results", str(A), str(B), str(C)],
        "analyze")
    run(["-m", "scripts.save_regime_presets", "--results", str(REG),
         "--candidates", "R-REG-进攻,R-REG-稳健,R-REG-防守"], "presets")
    run(["-m", "scripts.analyze_regime_switch", "--results", str(A), str(B),
         str(C), str(REG),
         "--map", ("risk_on=R-REG-进攻,neutral=R-REG-稳健,risk_off=R-REG-防守"),
         "--pool", "R-REG-进攻,R-REG-稳健,R-REG-防守",
         "--lag", "1", "--sweep"], "regime")
    log("ALL DONE")


if __name__ == "__main__":
    main()
