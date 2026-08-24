# -*- coding: utf-8 -*-
"""Reproducible rotation-strategy grid search on the enabled tradable watchlist.

The full BacktestEngine result of every candidate is persisted to the normal
backtest tables.  A compact experiment summary is also written under data/ so
the ranking formula and Top-N can be audited without loading large snapshots.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.data_replayer import DataReplayer
from backtest.engine import BacktestEngine
from core.ids import gen_backtest_id
from database import repository as repo
from strategies.rotation_executor import (
    build_rotation_signal_fn,
    resolve_rotation_params,
)


def _watch_universe() -> tuple[List[str], Dict[str, str], Dict[str, str]]:
    rows = repo.get_watchlist(enabled_only=True)
    tradable = [r for r in rows if r.get("asset_type") in ("etf", "stock")]
    symbols = [str(r["symbol"]) for r in tradable]
    asset_types = {str(r["symbol"]): str(r["asset_type"]) for r in tradable}
    names = {str(r["symbol"]): str(r.get("name") or "") for r in tradable}
    if len(symbols) < 2:
        raise RuntimeError("监控列表中可交易的 ETF/股票不足 2 只")
    return symbols, asset_types, names


def _dynamic_etf_master(end: date) -> tuple[List[str], Dict[str, str], Dict[str, str]]:
    """Locally reproducible ETF mother set; period membership is selected later."""
    symbols = repo.get_etf_history_symbols(end, min_bars=20)
    if len(symbols) < 2:
        raise RuntimeError("本地ETF历史母池不足 2 只，请先运行ETF历史补库任务")
    metadata = repo.get_symbol_metadata(symbols)
    asset_types = {symbol: "etf" for symbol in symbols}
    names = {symbol: str((metadata.get(symbol) or {}).get("name") or "")
             for symbol in symbols}
    return symbols, asset_types, names


def _load_saved_presets(universe_mode: str) -> Dict[str, Dict[str, Any]]:
    path = ROOT / "data" / "strategy_presets.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    # 固定监控池参数不能混入动态 ETF 池实验；没有显式版本标签的旧策略
    # 一律视为 legacy_manual，防止错误地把旧 TopN 当作新池候选。
    expected = "dynamic_etf" if universe_mode == "dynamic_etf" else "manual"
    return {
        str(k): dict(v) for k, v in (data.get("presets") or {}).items()
        if str((v or {}).get("universe_mode", "legacy_manual")) == expected
    }


def _grid_candidates() -> Iterable[tuple[str, Dict[str, Any]]]:
    """96-point balanced grid across the main timing and risk dimensions."""
    stop_profiles = {
        "tight": (0.08, 0.06, 0.04),
        "patient": (0.12, 0.10, 0.08),
    }
    target_by_n = {3: 0.25, 4: 0.21, 5: 0.17}
    for top_n in (3, 4, 5):
        for mom_window in (10, 15, 20, 30):
            for market_filter in (False, True):
                for interval in (1, 3):
                    for stop_name, stops in stop_profiles.items():
                        hard, trailing, activation = stops
                        key = (
                            f"G-N{top_n}-M{mom_window}-F{int(market_filter)}-"
                            f"R{interval}-{stop_name}"
                        )
                        yield key, {
                            "top_n": top_n,
                            "mom_window": mom_window,
                            "min_amount": 30_000_000,
                            "max_vol": 0.55,
                            "target_weight": target_by_n[top_n],
                            "max_total_position": 0.85,
                            "rebalance_threshold": 0.15,
                            "hard_stop_pct": hard,
                            "trailing_stop_pct": trailing,
                            "trailing_stop_activation": activation,
                            "market_filter": market_filter,
                            "market_exit_threshold": -0.03,
                            "market_enter_threshold": -0.01,
                            "cool_down_days": 5,
                            "rebalance_interval_days": interval,
                            "min_order_amount": 500,
                            "warmup_days": 20,
                            "min_hold_days": 3,
                            "hold_buffer": 2,
                            "max_buy_momentum": 0.25,
                            "min_momentum": 0.01,
                            "initial_ratio": 0.50,
                            "bottom_ratio": 0.0,
                            "fresh_stop_mult": 1.5,
                            "require_above_ma20": True,
                            "trend_ma_window": 20,
                            "max_distance_from_ma20": 0.15,
                            "low_rebound_bonus": 0.015,
                            "low_rebound_from_low_pct": 0.10,
                        }


def _saved_only_candidates() -> Iterable[tuple[str, Dict[str, Any]]]:
    for name, params in _load_saved_presets("manual").items():
        yield f"P-{name}", params


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _compact_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    equity = [_safe_float(x) for x in (metrics.get("equity_curve") or [])]
    midpoint = max(1, (len(equity) - 1) // 2) if equity else 0
    first_half = equity[midpoint] / equity[0] - 1 if midpoint and equity[0] else 0.0
    second_half = equity[-1] / equity[midpoint] - 1 if midpoint and equity[midpoint] else 0.0
    daily = [equity[i] / equity[i - 1] - 1
             for i in range(1, len(equity)) if equity[i - 1] > 0]
    monthly = list((metrics.get("monthly_returns") or {}).values())
    positive_month_ratio = (
        sum(1 for x in monthly if _safe_float(x) > 0) / len(monthly)
        if monthly else 0.0
    )
    position = [_safe_float(x) for x in (metrics.get("position_curve") or [])]
    avg_exposure = (
        sum(min(max(p / e, 0.0), 1.5) for p, e in zip(position, equity) if e > 0)
        / max(sum(1 for e in equity[:len(position)] if e > 0), 1)
    )
    benchmark = metrics.get("benchmark") or {}
    return {
        "run_id": metrics.get("run_id"),
        "total_return": _safe_float(metrics.get("total_return")),
        "annual_return": _safe_float(metrics.get("annual_return")),
        "max_drawdown": _safe_float(metrics.get("max_drawdown")),
        "sharpe": _safe_float(metrics.get("sharpe")),
        "calmar": _safe_float(metrics.get("calmar")),
        "win_rate": metrics.get("win_rate"),
        "profit_factor": metrics.get("profit_factor"),
        "turnover": _safe_float(metrics.get("turnover")),
        "trade_count": int(metrics.get("trade_count") or 0),
        "closed_trade_count": int(metrics.get("closed_trade_count") or 0),
        "avg_hold_days": _safe_float(metrics.get("avg_hold_days")),
        "max_consecutive_loss": int(metrics.get("max_consecutive_loss") or 0),
        "fee_total": _safe_float(metrics.get("fee_total")),
        "slippage_total": _safe_float(metrics.get("slippage_total")),
        "benchmark_return": _safe_float(benchmark.get("benchmark_return")),
        "excess_return": _safe_float(benchmark.get("excess_return")),
        "first_half_return": round(first_half, 6),
        "second_half_return": round(second_half, 6),
        "half_return_floor": round(min(first_half, second_half), 6),
        "positive_month_ratio": round(positive_month_ratio, 6),
        "max_daily_jump": round(max((abs(x) for x in daily), default=0.0), 6),
        "avg_exposure": round(avg_exposure, 6),
        "data_snapshot_hash": metrics.get("data_snapshot_hash", ""),
        "adjusted_symbols": sorted(
            sym for sym, report in (metrics.get("data_coverage") or {}).items()
            if (report or {}).get("price_adjustments")
        ),
        "exit_reason_stats": metrics.get("exit_reason_stats") or {},
    }


def _percentile(values: List[float], value: float) -> float:
    if len(values) <= 1:
        return 1.0
    ordered = sorted(values)
    below = sum(1 for x in ordered if x < value)
    equal = sum(1 for x in ordered if x == value)
    return (below + (equal - 1) / 2) / (len(ordered) - 1)


def _rank(results: List[Dict[str, Any]]) -> None:
    eligible = [r for r in results
                if not r.get("error")
                and r.get("closed_trade_count", 0) >= 12
                and r.get("max_drawdown", -1) >= -0.30
                and r.get("max_daily_jump", 1) <= 0.15]
    fields = {
        "sharpe": [_safe_float(r.get("sharpe")) for r in eligible],
        "calmar": [_safe_float(r.get("calmar")) for r in eligible],
        "annual_return": [_safe_float(r.get("annual_return")) for r in eligible],
        "half_return_floor": [_safe_float(r.get("half_return_floor")) for r in eligible],
        "drawdown_quality": [-abs(_safe_float(r.get("max_drawdown"))) for r in eligible],
        "positive_month_ratio": [_safe_float(r.get("positive_month_ratio")) for r in eligible],
        "profit_factor": [min(_safe_float(r.get("profit_factor")), 5.0) for r in eligible],
        "turnover_quality": [-_safe_float(r.get("turnover")) for r in eligible],
    }
    weights = {
        "sharpe": 0.22,
        "calmar": 0.18,
        "annual_return": 0.16,
        "half_return_floor": 0.16,
        "drawdown_quality": 0.12,
        "positive_month_ratio": 0.08,
        "profit_factor": 0.05,
        "turnover_quality": 0.03,
    }
    for r in results:
        if r not in eligible:
            r["eligible"] = False
            r["robust_score"] = None
            continue
        score = 0.0
        values = {
            "sharpe": _safe_float(r.get("sharpe")),
            "calmar": _safe_float(r.get("calmar")),
            "annual_return": _safe_float(r.get("annual_return")),
            "half_return_floor": _safe_float(r.get("half_return_floor")),
            "drawdown_quality": -abs(_safe_float(r.get("max_drawdown"))),
            "positive_month_ratio": _safe_float(r.get("positive_month_ratio")),
            "profit_factor": min(_safe_float(r.get("profit_factor")), 5.0),
            "turnover_quality": -_safe_float(r.get("turnover")),
        }
        for field, weight in weights.items():
            score += weight * _percentile(fields[field], values[field])
        r["eligible"] = True
        r["robust_score"] = round(score * 100, 4)
    results.sort(key=lambda r: (
        r.get("robust_score") is not None,
        r.get("robust_score") or -1,
        r.get("total_return") or -1,
    ), reverse=True)
    for idx, row in enumerate((r for r in results if r.get("eligible")), 1):
        row["rank"] = idx


def run(args: argparse.Namespace) -> Path:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if args.universe_mode == "dynamic_etf":
        symbols, asset_types, names = _dynamic_etf_master(end)
    else:
        symbols, asset_types, names = _watch_universe()
    presets = _load_saved_presets(args.universe_mode)
    candidates: List[tuple[str, str, Dict[str, Any]]] = [
        (f"P-{name}", "saved_preset", params) for name, params in presets.items()
    ]
    if not args.saved_only:
        candidates.extend((name, "grid", params) for name, params in _grid_candidates())

    print(f"Universe mode: {args.universe_mode}", flush=True)
    print(f"Mother universe ({len(symbols)}): {', '.join(symbols)}", flush=True)
    print(f"Candidates: {len(presets)} saved + {len(candidates) - len(presets)} grid", flush=True)

    # One DataReplayer and one feature cache freeze all candidates to identical
    # inputs and avoid N repeated remote downloads/indicator calculations.
    replayer = DataReplayer(
        symbols, asset_types=asset_types, min_coverage=args.min_coverage,
        online_fill=args.universe_mode != "dynamic_etf")
    feature_cache: Dict[Any, Dict[str, Any]] = {}
    results: List[Dict[str, Any]] = []
    snapshot_hash = ""
    for idx, (candidate_name, family, raw_params) in enumerate(candidates, 1):
        run_id = gen_backtest_id()
        resolved = resolve_rotation_params(raw_params, use_live_preset=False)
        resolved["universe_mode"] = args.universe_mode
        engine = BacktestEngine(
            start, end, initial_cash=args.initial_cash,
            mode="daily", use_agents=False,
            name=f"{args.experiment}:{candidate_name}"[:64],
            run_id=run_id, asset_type="etf", asset_types=asset_types,
        )
        engine.params = resolved
        engine.name_map = names
        universe_provider = None
        if args.universe_mode == "dynamic_etf":
            from strategies.dynamic_etf_universe import (
                DynamicEtfUniverseSelector,
                HistoricalUniverseProvider,
            )
            universe_provider = HistoricalUniverseProvider(
                DynamicEtfUniverseSelector(), source_mode="grid",
                snapshot_salt=run_id)
        signal_fn = build_rotation_signal_fn(
            initial_cash=args.initial_cash,
            params=resolved,
            use_live_preset=False,
            feature_cache=feature_cache,
            universe_provider=universe_provider,
        )
        try:
            metrics = engine.run_daily(replayer, signal_fn)
            compact = _compact_metrics(metrics)
            compact.update({
                "candidate": candidate_name,
                "family": family,
                "params": resolved,
                "universe_mode": args.universe_mode,
            })
            snapshot_hash = snapshot_hash or compact["data_snapshot_hash"]
            if snapshot_hash != compact["data_snapshot_hash"]:
                raise RuntimeError("候选策略使用了不同的行情快照")
            results.append(compact)
            print(
                f"[{idx:03d}/{len(candidates)}] {candidate_name:<31} "
                f"ret={compact['total_return']:+.2%} dd={compact['max_drawdown']:.2%} "
                f"sharpe={compact['sharpe']:.2f} trades={compact['trade_count']}",
                flush=True,
            )
        except Exception as exc:
            repo.update_backtest_run(run_id, "FAILED")
            results.append({
                "run_id": run_id, "candidate": candidate_name,
                "family": family, "params": resolved, "error": str(exc),
            })
            print(f"[{idx:03d}/{len(candidates)}] {candidate_name} FAILED: {exc}", flush=True)

    _rank(results)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = ROOT / "data" / f"strategy_grid_{args.experiment}_{stamp}.json"
    payload = {
        "experiment": args.experiment,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": args.start, "end": args.end},
        "initial_cash": args.initial_cash,
        "universe_mode": args.universe_mode,
        "symbols": [{"symbol": s, "name": names.get(s, ""),
                     "asset_type": asset_types[s]} for s in symbols],
        "saved_preset_count": len(presets),
        "grid_count": len(candidates) - len(presets),
        "snapshot_hash": snapshot_hash,
        "ranking": {
            "eligibility": "closed trades >= 12, drawdown >= -30%, max daily equity jump <= 15%",
            "weights": {
                "sharpe": 0.22, "calmar": 0.18, "annual_return": 0.16,
                "half_return_floor": 0.16, "drawdown_quality": 0.12,
                "positive_month_ratio": 0.08, "profit_factor": 0.05,
                "low_turnover": 0.03,
            },
        },
        "top5": [r for r in results if r.get("eligible")][:5],
        "results": results,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Summary: {output}", flush=True)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-07-01")
    parser.add_argument("--end", default="2026-08-12")
    parser.add_argument("--initial-cash", type=float, default=20_000.0)
    parser.add_argument("--min-coverage", type=float, default=0.98)
    parser.add_argument("--experiment", default="WATCH26_V1")
    parser.add_argument("--universe-mode", choices=("dynamic_etf", "manual"),
                        default="dynamic_etf")
    parser.add_argument("--saved-only", action="store_true",
                        help="只回测已保存策略，不追加96组网格")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
