# -*- coding: utf-8 -*-
"""Point-in-time two-stage ETF universe selection.

Stage 1 intentionally uses only eligibility and tradability information known
at the selection date. Momentum is excluded here and remains Stage 2 in the
rotation strategy, preventing the same alpha signal from selecting and ranking
the universe twice.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Set

from core.config import get_settings
from database import repository as repo

SELECTOR_VERSION = "etf_pool_v1"


def dynamic_pool_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(get_settings().get("universe.dynamic_etf_pool", {}) or {})
    base.update(overrides or {})
    cfg = {
        "enabled": bool(base.get("enabled", True)),
        "refresh": str(base.get("refresh", "monthly")),
        "max_candidates": int(base.get("max_candidates", 40)),
        "min_candidates": int(base.get("min_candidates", 8)),
        "min_listing_days": int(base.get("min_listing_days", 60)),
        "liquidity_window": int(base.get("liquidity_window", 20)),
        "min_avg_amount": float(base.get("min_avg_amount", 30_000_000)),
        "max_annualized_volatility": float(
            base.get("max_annualized_volatility", 0.80)),
        "min_price": float(base.get("min_price", 0.20)),
        "max_per_theme": int(base.get("max_per_theme", 2)),
        "exclude_qdii": bool(base.get("exclude_qdii", False)),
        "coverage_window": int(base.get("coverage_window", 60)),
        "min_recent_coverage": float(base.get("min_recent_coverage", 0.95)),
    }
    if cfg["refresh"] not in ("weekly", "monthly"):
        raise ValueError("dynamic_etf_pool.refresh 仅支持 weekly/monthly")
    if not 2 <= cfg["max_candidates"] <= 200:
        raise ValueError("dynamic_etf_pool.max_candidates 应在 2~200")
    if not 1 <= cfg["min_candidates"] <= cfg["max_candidates"]:
        raise ValueError("dynamic_etf_pool.min_candidates 必须小于候选池上限")
    return cfg


def _theme_key(symbol: str, name: str, tracking_index: str) -> str:
    raw = (tracking_index or name or symbol).upper()
    raw = re.sub(r"ETF|交易型开放式指数|联接|基金|增强|发起式|[A-Z]类", "", raw)
    raw = re.sub(r"华夏|易方达|南方|国泰|华泰柏瑞|广发|富国|鹏华|嘉实|银华|华宝|永赢", "", raw)
    raw = re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", raw)
    return raw[:40] or symbol


def _annualized_vol(closes: List[float]) -> float:
    returns = [closes[i] / closes[i - 1] - 1
               for i in range(1, len(closes)) if closes[i - 1] > 0]
    return pstdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0


def _manual_overrides() -> tuple[Set[str], Set[str]]:
    pinned: Set[str] = set()
    excluded: Set[str] = set()
    for item in repo.get_watchlist():
        cats = set(item.get("categories") or [])
        if "pool_pin" in cats and item.get("asset_type") == "etf":
            pinned.add(str(item["symbol"]))
        if "pool_exclude" in cats:
            excluded.add(str(item["symbol"]))
    return pinned, excluded


@dataclass
class SelectionResult:
    snapshot: Dict[str, Any]
    rejected: Dict[str, str]


class DynamicEtfUniverseSelector:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dynamic_pool_config(config)

    def select(self, bars_by_symbol: Dict[str, List[dict]], asof_date: date,
               effective_date: date, source_mode: str = "paper",
               persist: bool = True, snapshot_salt: str = "",
               apply_manual_overrides: bool = True) -> SelectionResult:
        """Select using bars no later than ``asof_date`` and persist the audit."""
        cfg = self.config
        metadata = repo.get_etf_metadata(list(bars_by_symbol))
        symbol_rows = repo.get_symbol_metadata(list(bars_by_symbol))
        pinned, excluded = (_manual_overrides()
                            if apply_manual_overrides else (set(), set()))
        ranked: List[Dict[str, Any]] = []
        rejected: Dict[str, str] = {}
        coverage_window = cfg["coverage_window"]

        # The union of observed dates is the point-in-time trading calendar.
        observed_dates = sorted({
            b["trade_date"] for bars in bars_by_symbol.values() for b in bars
            if b.get("trade_date") and b["trade_date"] <= asof_date
        })
        data_asof_date = observed_dates[-1] if observed_dates else asof_date
        expected_recent = set(observed_dates[-coverage_window:])

        for symbol, raw_bars in bars_by_symbol.items():
            if symbol in excluded:
                rejected[symbol] = "人工排除"
                continue
            row = symbol_rows.get(symbol)
            if row is None or row.get("asset_type") != "etf":
                rejected[symbol] = "非ETF"
                continue
            bars = sorted((b for b in raw_bars
                           if b.get("trade_date") and b["trade_date"] <= asof_date),
                          key=lambda b: b["trade_date"])
            is_pinned = symbol in pinned
            if len(bars) < min(20, cfg["min_listing_days"]):
                rejected[symbol] = "基础历史行情不足20日"
                continue
            if len(bars) < cfg["min_listing_days"] and not is_pinned:
                rejected[symbol] = f"上市交易日不足{cfg['min_listing_days']}日"
                continue
            recent_dates = {b["trade_date"] for b in bars if b["trade_date"] in expected_recent}
            coverage = (len(recent_dates) / len(expected_recent)
                        if expected_recent else 0.0)
            if coverage < cfg["min_recent_coverage"] and not is_pinned:
                rejected[symbol] = f"近期行情覆盖率{coverage:.1%}"
                continue
            liq_bars = bars[-cfg["liquidity_window"]:]
            amounts = [float(b.get("amount") or 0) for b in liq_bars]
            avg_amount = mean(amounts) if amounts else 0.0
            closes = [float(b.get("close") or 0) for b in bars[-max(21, coverage_window):]]
            latest_price = closes[-1] if closes else 0.0
            volatility = _annualized_vol(closes)
            meta = metadata.get(symbol, {})
            if cfg["exclude_qdii"] and meta.get("is_qdii") and not is_pinned:
                rejected[symbol] = "QDII已排除"
                continue
            if avg_amount < cfg["min_avg_amount"] and not is_pinned:
                rejected[symbol] = f"{cfg['liquidity_window']}日均成交额不足"
                continue
            if latest_price < cfg["min_price"] and not is_pinned:
                rejected[symbol] = "价格过低"
                continue
            if volatility > cfg["max_annualized_volatility"] and not is_pinned:
                rejected[symbol] = "年化波动率过高"
                continue
            name = str(meta.get("name") or row.get("name") or "")
            theme = _theme_key(symbol, name, str(meta.get("tracking_index") or ""))
            ranked.append({
                "symbol": symbol, "name": name, "theme": theme,
                "avg_amount": round(avg_amount, 2),
                "annualized_volatility": round(volatility, 6),
                "recent_coverage": round(coverage, 6),
                "first_bar_date": str(bars[0]["trade_date"]),
                "last_bar_date": str(bars[-1]["trade_date"]),
                "pinned": is_pinned,
                "selection_reason": "人工固定" if is_pinned else "上市时长/流动性/完整度/波动率合格",
            })

        # Pinned first, then stable liquidity ranking; cap duplicate themes.
        ranked.sort(key=lambda x: (bool(x["pinned"]), x["avg_amount"], x["symbol"]),
                    reverse=True)
        selected: List[Dict[str, Any]] = []
        theme_counts: Dict[str, int] = {}
        for item in ranked:
            theme = item["theme"]
            if (not item["pinned"] and
                    theme_counts.get(theme, 0) >= cfg["max_per_theme"]):
                rejected[item["symbol"]] = f"同主题最多{cfg['max_per_theme']}只"
                continue
            if len(selected) >= cfg["max_candidates"] and not item["pinned"]:
                rejected[item["symbol"]] = "候选池名额已满"
                continue
            item = {**item, "rank": len(selected) + 1}
            selected.append(item)
            theme_counts[theme] = theme_counts.get(theme, 0) + 1

        if len(selected) < cfg["min_candidates"]:
            raise ValueError(
                f"动态ETF池仅筛得 {len(selected)} 只，低于最低要求 {cfg['min_candidates']}；"
                "请先补齐ETF历史行情/上市日期，或调整选池阈值")
        audit = {
            "selector_version": SELECTOR_VERSION,
            "source_mode": source_mode,
            "asof_date": str(data_asof_date),
            "effective_date": str(effective_date),
            "members": selected,
            "config": cfg,
        }
        raw = json.dumps(audit, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        snapshot_hash = hashlib.sha256(raw).hexdigest()
        snapshot_id = "ETFPOOL-" + hashlib.sha256(
            (snapshot_hash + snapshot_salt).encode("utf-8")).hexdigest()[:24].upper()
        payload = {
            "snapshot_id": snapshot_id,
            "selector_version": SELECTOR_VERSION,
            "source_mode": source_mode,
            "asof_date": data_asof_date,
            "effective_date": effective_date,
            "candidate_count": len(selected),
            "members_json": selected,
            "config_json": cfg,
            "snapshot_hash": snapshot_hash,
        }
        snapshot = repo.save_etf_universe_snapshot(payload) if persist else {
            **payload, "asof_date": str(data_asof_date),
            "effective_date": str(effective_date), "members": selected,
            "config": cfg,
        }
        return SelectionResult(snapshot=snapshot, rejected=rejected)


class HistoricalUniverseProvider:
    """Monthly/weekly point-in-time provider for the rotation signal function."""
    def __init__(self, selector: DynamicEtfUniverseSelector,
                 source_mode: str = "backtest", snapshot_salt: str = ""):
        self.selector = selector
        self.source_mode = source_mode
        self.snapshot_salt = snapshot_salt
        self.current_period: Optional[str] = None
        self.current_symbols: Set[str] = set()
        self.snapshots: List[Dict[str, Any]] = []

    def _period(self, d: date) -> str:
        if self.selector.config["refresh"] == "weekly":
            year, week, _ = d.isocalendar()
            return f"{year}-W{week:02d}"
        return f"{d.year:04d}-{d.month:02d}"

    def __call__(self, d: date, asof: Dict[str, List[dict]]) -> Set[str]:
        period = self._period(d)
        if period != self.current_period:
            # Effective today, based strictly on data known before today.
            known = {s: [b for b in bars if b.get("trade_date") < d]
                     for s, bars in asof.items()}
            result = self.selector.select(
                known, asof_date=d - timedelta(days=1), effective_date=d,
                source_mode=self.source_mode, persist=True,
                snapshot_salt=f"{self.snapshot_salt}:{period}",
                apply_manual_overrides=False)
            self.current_symbols = {
                str(x["symbol"]) for x in result.snapshot.get("members", [])}
            self.snapshots.append(result.snapshot)
            self.current_period = period
        return set(self.current_symbols)


def latest_paper_universe(on_date: date) -> Optional[Dict[str, Any]]:
    return repo.get_latest_etf_universe_snapshot(on_date, "paper")


def paper_snapshot_is_current(snapshot: Optional[Dict[str, Any]], on_date: date,
                              config: Optional[Dict[str, Any]] = None) -> bool:
    if not snapshot:
        return False
    cfg = dynamic_pool_config(config)
    effective = date.fromisoformat(str(snapshot["effective_date"])[:10])
    if cfg["refresh"] == "weekly":
        return effective.isocalendar()[:2] == on_date.isocalendar()[:2]
    return (effective.year, effective.month) == (on_date.year, on_date.month)


def sync_snapshot_to_watchlist(snapshot: Dict[str, Any]) -> None:
    """Expose current automatic members on the monitor page without deleting user rows."""
    members = {str(x["symbol"]): x for x in snapshot.get("members", [])}
    existing = repo.get_watchlist()
    for symbol, item in members.items():
        repo.upsert_watch_item(symbol, str(item.get("name") or ""), "etf",
                               categories=["dynamic"], enabled=True, priority=8)
    for item in existing:
        cats = list(item.get("categories") or [])
        if "dynamic" in cats and item["symbol"] not in members:
            # Keep the row: user state may be added concurrently and an
            # automatically-created orphan is harmless when it has no flags.
            repo.set_watch_category_flag(item["symbol"], "dynamic", False)


def build_current_paper_snapshot(effective_date: date,
                                 config: Optional[Dict[str, Any]] = None,
                                 force: bool = False) -> Dict[str, Any]:
    """Build today's pool from locally persisted bars through the prior day."""
    cfg = dynamic_pool_config(config)
    latest = latest_paper_universe(effective_date)
    if latest and not force:
        if paper_snapshot_is_current(latest, effective_date, cfg):
            sync_snapshot_to_watchlist(latest)
            return latest
    selector = DynamicEtfUniverseSelector(cfg)
    asof_date = effective_date - timedelta(days=1)
    symbols = repo.get_etf_history_symbols(asof_date, min_bars=cfg["min_listing_days"])
    bars_by_symbol = {
        s: [
            {"symbol": b.symbol, "trade_date": b.trade_date, "open": b.open,
             "high": b.high, "low": b.low, "close": b.close,
             "volume": b.volume, "amount": b.amount}
            for b in repo.get_daily_bars(
                s, asof_date - timedelta(days=max(400, cfg["coverage_window"] * 3)),
                asof_date)
        ]
        for s in symbols
    }
    result = selector.select(
        bars_by_symbol, asof_date=asof_date, effective_date=effective_date,
        source_mode="paper", persist=True,
        snapshot_salt=effective_date.isoformat())
    sync_snapshot_to_watchlist(result.snapshot)
    return result.snapshot
