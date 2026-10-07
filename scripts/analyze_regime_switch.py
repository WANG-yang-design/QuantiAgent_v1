# -*- coding: utf-8 -*-
"""
市场状态自适应切换的离线验证
============================
输入: data/backtest_grid/v4/results_{A_fix,B_fix}.jsonl (修复候选池后的3年曲线)
      + 策略映射(候选名)
方法:
  1. 用与实盘相同的规则(000300 的 MA20/MA60 + 20日动量 + 非对称防抖)重建历史状态序列;
  2. 固定映射切换: 每日按状态持有对应候选的当日收益, 拼接为组合净值;
  3. 对照: 各静态候选、63日滚动择优、沪深300基准;
  4. 输出 data/backtest_grid/v4/analysis/REGIME_SWITCH_REPORT.md
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
V4 = ROOT / "data" / "backtest_grid" / "v4"
OUT = V4 / "analysis"


def load_rows(paths: List[Path]) -> Dict[str, dict]:
    rows: Dict[str, dict] = {}
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("error") or not r.get("equity_curve"):
                continue
            rows[r["candidate"]] = r
    return rows


def index_closes(start: date, end: date, symbol: str = "000300"):
    from database import repository as repo
    bars = repo.get_daily_bars(symbol, start - timedelta(days=200), end)
    out = [(str(b.trade_date)[:10], float(b.close or 0)) for b in bars
           if (b.close or 0) > 0]
    if not out:
        return []
    return out


def regime_series(closes: List[tuple], ma_s: int = 20, ma_l: int = 60,
                  on_mom: float = 0.02, off_mom: float = -0.03,
                  confirm_days: int = 2, sticky: bool = False) -> Dict[str, str]:
    """按日重建状态(只用当日及之前的数据)。

    sticky=True: 状态需连续 confirm_days 日成立才切换, 否则保持上一状态(防抖,
    避免风险状态单日反复横跳); sticky=False: 原规则(risk_off立即, risk_on确认)。
    """
    vals = [c for _, c in closes]
    dates = [d for d, _ in closes]
    out: Dict[str, str] = {}
    prev = "neutral"
    for i in range(len(vals)):
        if i + 1 < max(ma_l, 21):
            out[dates[i]] = "neutral"
            continue
        window = vals[:i + 1]

        def raw(w):
            if len(w) < max(ma_l, 21):
                return "neutral"
            close = w[-1]
            ma_sv = sum(w[-ma_s:]) / ma_s
            ma_lv = sum(w[-ma_l:]) / ma_l
            mom = w[-1] / w[-21] - 1 if w[-21] > 0 else 0.0
            if close > ma_sv > ma_lv and mom >= on_mom:
                return "risk_on"
            if close < ma_sv and mom <= off_mom:
                return "risk_off"
            return "neutral"

        r = raw(window)
        if sticky:
            stable = True
            for k in range(1, confirm_days):
                if len(window) > k and raw(window[:-k]) != r:
                    stable = False
                    break
            state = r if stable else prev
        else:
            state = r
            if r == "risk_on" and confirm_days > 1:
                for k in range(1, confirm_days):
                    if len(window) > k and raw(window[:-k]) != r:
                        state = "neutral"
                        break
        out[dates[i]] = state
        prev = state
    return out


def curve_returns(curve: List[float]) -> List[float]:
    return [curve[i] / curve[i - 1] - 1 if curve[i - 1] > 0 else 0.0
            for i in range(1, len(curve))]


def shift_states(states: Dict[str, str], dates: List[str], lag: int) -> Dict[str, str]:
    """滞后 lag 个交易日: 第T日使用的状态由 T-lag 收盘数据决定(实盘14:40口径)。"""
    if lag <= 0:
        return states
    out: Dict[str, str] = {}
    for i, d in enumerate(dates):
        out[d] = states.get(dates[i - lag], "neutral") if i >= lag else "neutral"
    return out


def metrics(curve: List[float]) -> Dict[str, float]:
    if len(curve) < 3:
        return {}
    rets = curve_returns(curve)
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    peak, mdd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return {
        "total_return": curve[-1] / curve[0] - 1,
        "annual_return": (curve[-1] / curve[0]) ** (252 / max(len(rets), 1)) - 1,
        "sharpe": (mean / sd * math.sqrt(252)) if sd > 0 else 0.0,
        "max_drawdown": -mdd,
    }


def simulate_fixed(state_by_date: Dict[str, str], mapping: Dict[str, str],
                   rows: Dict[str, dict], min_hold_days: int = 0) -> Dict[str, Any]:
    # 以所有候选共有的日期为基准
    base = next(iter(rows.values()))
    dates = base["dates"]
    curves = {k: dict(zip(r["dates"], r["equity_curve"])) for k, r in rows.items()}
    port = [1.0]
    switches = []
    last_state = None
    current = mapping.get(state_by_date.get(dates[0], "neutral")) or mapping.get("neutral")
    held_since = 0
    for i in range(1, len(dates)):
        d = dates[i]
        state = state_by_date.get(d, "neutral")
        want = mapping.get(state) or mapping.get("neutral")
        held_since += 1
        if want != current and (min_hold_days <= 0 or held_since >= min_hold_days):
            switches.append({"date": d, "state": state, "from": current, "target": want})
            current = want
            held_since = 0
        c = curves[current]
        prev_v, cur_v = c.get(dates[i - 1]), c.get(d)
        if prev_v and cur_v and prev_v > 0:
            port.append(port[-1] * cur_v / prev_v)
        else:
            port.append(port[-1])
        last_state = state
    return {"equity": port, "dates": dates[1:], "switches": switches}


def simulate_rolling(state_by_date: Dict[str, str], rows: Dict[str, dict],
                     pool: List[str], window: int = 63) -> Dict[str, Any]:
    """每 window 日在池内用近期表现择优(不使用未来)。"""
    base = rows[pool[0]]
    dates = base["dates"]
    curves = {k: dict(zip(rows[k]["dates"], rows[k]["equity_curve"])) for k in pool}
    port = [1.0]
    picks = []
    for start_i in range(1, len(dates), window):
        seg_dates = dates[max(0, start_i - window):start_i]
        best, best_score = pool[0], -1e18
        for k in pool:
            c = curves[k]
            vals = [c[d] for d in seg_dates if d in c]
            if len(vals) < 20:
                continue
            m = metrics(vals)
            score = m["sharpe"] + 2 * m["max_drawdown"]
            if score > best_score:
                best_score, best = score, k
        picks.append({"date": dates[start_i], "pick": best, "score": round(best_score, 3)})
        for i in range(start_i, min(start_i + window, len(dates))):
            c = curves[best]
            pv, cv = c.get(dates[i - 1]), c.get(dates[i])
            port.append(port[-1] * (cv / pv) if pv and cv and pv > 0 else port[-1])
    return {"equity": port, "dates": dates[:len(port)], "picks": picks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+",
                    default=[str(V4 / "results_A_fix.jsonl"),
                             str(V4 / "results_B_fix.jsonl")])
    ap.add_argument("--map", required=True,
                    help='候选名映射: "risk_on=X,neutral=Y,risk_off=Z"')
    ap.add_argument("--pool", default="", help="滚动择优池(逗号分隔候选名)")
    ap.add_argument("--confirm", type=int, default=2)
    ap.add_argument("--sticky", action="store_true",
                    help="防抖模式: 状态需连续confirm日成立才切换(否则保持)")
    ap.add_argument("--min-hold", type=int, default=0,
                    help="两次切换之间的最小交易日数")
    ap.add_argument("--sweep", action="store_true", help="参数扫描并自动选优")
    ap.add_argument("--lag", type=int, default=1,
                    help="状态滞后交易日数(实盘14:40用T-1收盘, 默认1)")
    args = ap.parse_args()

    mapping = {}
    for part in args.map.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            mapping[k.strip()] = v.strip()
    pool = [x.strip() for x in args.pool.split(",") if x.strip()]

    rows = load_rows([Path(p) for p in args.results])
    print("候选曲线:", len(rows))
    missing = [v for v in mapping.values() if v not in rows]
    if missing:
        raise SystemExit(f"映射候选不存在: {missing}")

    any_row = next(iter(rows.values()))
    start = date.fromisoformat(any_row["dates"][0])
    end = date.fromisoformat(any_row["dates"][-1])
    closes = index_closes(start, end)
    ma_s, ma_l = 20, 60
    on_mom, off_mom = 0.02, -0.03

    if args.sweep:
        print(f"\n== 参数扫描(离线段, 状态滞后{args.lag}个交易日) ==")
        print("%-28s %9s %9s %8s %9s %7s" % ("rule", "return", "annual", "sharpe", "maxDD", "switches"))
        results = []
        base_states = regime_series(closes, confirm_days=2, sticky=False)
        state_dates = [d for d, _ in closes]
        for confirm in (2, 5, 8):
            for sticky in (False, True):
                for min_hold in (0, 10, 20):
                    st = shift_states(regime_series(closes, confirm_days=confirm, sticky=sticky),
                                      state_dates, args.lag)
                    sim = simulate_fixed(st, mapping, {k: rows[k] for k in mapping.values()},
                                         min_hold_days=min_hold)
                    m = metrics(sim["equity"])
                    results.append((confirm, sticky, min_hold, m, len(sim["switches"])))
                    print("%-28s %+8.2f%% %+8.2f%% %8.2f %8.2f%% %7d" % (
                        f"confirm={confirm} sticky={sticky} hold={min_hold}",
                        m["total_return"] * 100, m["annual_return"] * 100,
                        m["sharpe"], m["max_drawdown"] * 100, len(sim["switches"])))
        # 选择: 夏普优先, 回撤次要
        best = max(results, key=lambda x: (round(x[3]["sharpe"], 3), x[3]["total_return"]))
        print(f"\n推荐离线规则: confirm={best[0]} sticky={best[1]} min_hold={best[2]}")
        states = shift_states(regime_series(closes, confirm_days=best[0], sticky=best[1]),
                              state_dates, args.lag)
        dist = {s: sum(1 for v in states.values() if v == s) for s in
                ("risk_on", "neutral", "risk_off")}
        fixed = simulate_fixed(states, mapping, {k: rows[k] for k in mapping.values()},
                               min_hold_days=best[2])
        confirm_used, sticky_used, min_hold_used = best[0], best[1], best[2]
    else:
        state_dates = [d for d, _ in closes]
        states = shift_states(
            regime_series(closes, confirm_days=args.confirm, sticky=args.sticky),
            state_dates, args.lag)
        fixed = simulate_fixed(states, mapping, {k: rows[k] for k in mapping.values()},
                               min_hold_days=args.min_hold)
        confirm_used, sticky_used, min_hold_used = args.confirm, args.sticky, args.min_hold
    dist = {s: sum(1 for v in states.values() if v == s) for s in
            ("risk_on", "neutral", "risk_off")}
    print("状态分布:", dist)
    fixed_m = metrics(fixed["equity"])
    static = {k: metrics(v["equity_curve"]) for k, v in rows.items() if k in mapping.values()}
    rolling = simulate_rolling(states, rows, pool) if pool else None
    rolling_m = metrics(rolling["equity"]) if rolling else None

    # 基准
    bench_ret = None
    if closes:
        cmap = dict(closes)
        first = closes[0][1]
        bench_ret = cmap[max(cmap)] / first - 1 if first else None

    L: List[str] = []
    A = L.append
    A("# 市场状态自适应切换 · 离线验证报告")
    A("")
    A(f"- 区间: {start} ~ {end} ({len(any_row['dates'])} 个交易日), 基准指数 000300")
    A(f"- 状态分布(日): {dist}")
    rule_desc = (f"MA{ma_s}/MA{ma_l} + 20日动量(≥{on_mom:+.0%}进攻/≤{off_mom:+.0%}防守), "
                 f"confirm={confirm_used}, sticky={sticky_used}, 最小切换间隔="
                 f"{min_hold_used}个交易日")
    A(f"- 规则: {rule_desc}; 映射 {mapping}")
    A("")
    A("## 结果")
    A("")
    A("| 方案 | 总收益 | 年化 | 夏普 | 最大回撤 |")
    A("|---|---|---|---|---|")
    A(f"| **状态切换组合** | {fixed_m['total_return']*100:+.2f}% | "
      f"{fixed_m['annual_return']*100:+.2f}% | {fixed_m['sharpe']:.2f} | "
      f"{fixed_m['max_drawdown']*100:.2f}% |")
    if rolling_m:
        A(f"| 63日滚动择优(池内) | {rolling_m['total_return']*100:+.2f}% | "
          f"{rolling_m['annual_return']*100:+.2f}% | {rolling_m['sharpe']:.2f} | "
          f"{rolling_m['max_drawdown']*100:.2f}% |")
    for k, m in static.items():
        A(f"| 静态: {k} | {m['total_return']*100:+.2f}% | {m['annual_return']*100:+.2f}% | "
          f"{m['sharpe']:.2f} | {m['max_drawdown']*100:.2f}% |")
    if bench_ret is not None:
        A(f"| 沪深300 | {bench_ret*100:+.2f}% | - | - | - |")
    A("")
    A("## 切换记录")
    A("")
    A("| 日期 | 状态 | 持有策略 |")
    A("|---|---|---|")
    for s in fixed["switches"]:
        A(f"| {s['date']} | {s['state']} | {s['target']} |")
    if rolling:
        A("")
        A("## 滚动择优选择")
        A("")
        A("| 日期 | 选中 | 近63日得分 |")
        A("|---|---|---|")
        for p in rolling["picks"]:
            A(f"| {p['date']} | {p['pick']} | {p['score']} |")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "REGIME_SWITCH_REPORT.md"
    path.write_text("\n".join(L), encoding="utf-8")
    print("报告:", path)
    print("状态切换:", {k: round(v, 4) for k, v in fixed_m.items()})
    for k, m in static.items():
        print("  静态", k, {kk: round(vv, 4) for kk, vv in m.items()})


if __name__ == "__main__":
    main()
