"""Process-wide, low-latency quote snapshot shared by Web pages and valuation."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Iterable

from core.config import get_settings
from data_sources.sina_client import SinaClient
from data_sources.tencent_client import TencentClient

_INDEX_TX = {"000001": "sh000001", "000300": "sh000300",
             "000905": "sh000905", "399006": "sz399006"}


class LiveQuoteService:
    def __init__(self, tencent: TencentClient | None = None,
                 sina: SinaClient | None = None,
                 quote_diff_threshold: float | None = None):
        self._tencent = tencent or TencentClient()
        self._sina = sina or SinaClient()
        cfg = get_settings().section("data_sources")
        self._quote_diff_threshold = float(
            quote_diff_threshold if quote_diff_threshold is not None
            else cfg.get("cross_source", {}).get("quote_diff_pct", 0.005))
        self._stale_fallback_seconds = float(
            cfg.get("freshness", {}).get("realtime_quote", 60))
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._updated_monotonic: Dict[str, float] = {}
        self._last_error = ""

    @staticmethod
    def _quote_time(quote: Dict[str, Any]) -> datetime:
        value = quote.get("quote_time")
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
            except ValueError:
                pass
        return datetime.min

    def _consensus(self, symbol: str,
                   source_quotes: Dict[str, Dict[str, Any]]
                   ) -> tuple[Dict[str, Any] | None, str]:
        valid = {
            source: dict(quote) for source, quote in source_quotes.items()
            if float((quote or {}).get("latest_price", 0) or 0) > 0
        }
        if not valid:
            return None, f"{symbol}: all public quote sources failed"

        prices = {source: float(q["latest_price"])
                  for source, q in valid.items()}
        price_spread = 0.0
        if len(prices) >= 2:
            price_spread = (max(prices.values()) - min(prices.values())) / max(
                min(prices.values()), 1e-12)
            if price_spread > self._quote_diff_threshold:
                detail = ", ".join(f"{k}={v:.6f}" for k, v in sorted(prices.items()))
                return None, (
                    f"{symbol}: quote conflict {price_spread:.3%} exceeds "
                    f"{self._quote_diff_threshold:.3%} ({detail})")

        # Prefer the freshest exchange timestamp; Tencent wins exact-time ties
        # because it also carries five-level order-book fields.
        selected_source, selected = max(
            valid.items(),
            key=lambda item: (self._quote_time(item[1]), item[0] == "tencent"))
        selected = dict(selected)
        verified = sorted(valid)
        selected.update({
            "source": "+".join(verified),
            "selected_source": selected_source,
            "verified_sources": len(verified),
            "source_prices": prices,
            "price_spread_pct": round(price_spread, 8),
            "quality_status": "VALID" if len(verified) >= 2 else "SINGLE_SOURCE",
        })
        return selected, ""

    def _fetch_regular(self, symbols: list[str]) -> tuple[Dict[str, Dict[str, Any]], list[str]]:
        by_source: Dict[str, Dict[str, Dict[str, Any]]] = {}
        errors: list[str] = []
        calls = {
            "tencent": lambda: self._tencent.get_realtime_quotes_batch(symbols),
            "sina": lambda: self._sina.get_realtime_quotes_batch(symbols),
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(call): source for source, call in calls.items()}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    by_source[source] = future.result() or {}
                except Exception as exc:
                    errors.append(f"{source}: {exc}")
        accepted: Dict[str, Dict[str, Any]] = {}
        for symbol in symbols:
            quote, error = self._consensus(symbol, {
                source: quotes[symbol]
                for source, quotes in by_source.items() if symbol in quotes
            })
            if quote is not None:
                accepted[symbol] = quote
            if error:
                errors.append(error)
        return accepted, errors

    def _fetch_index(self, symbol: str) -> tuple[Dict[str, Any] | None, str]:
        code = _INDEX_TX[symbol]
        quotes: Dict[str, Dict[str, Any]] = {}
        errors = []
        for source, call in (
            ("tencent", lambda: self._tencent.get_realtime_quote(
                symbol, "index", tx_code=code)),
            ("sina", lambda: self._sina.get_realtime_quote(
                symbol, "index", sina_code=code)),
        ):
            try:
                quotes[source] = call()
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        quote, conflict = self._consensus(symbol, quotes)
        if conflict:
            errors.append(conflict)
        return quote, "; ".join(errors)

    @staticmethod
    def _symbols(symbols: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(str(s or "").strip().upper() for s in symbols if str(s or "").strip()))

    def get_quotes(self, symbols: Iterable[str], max_age: float = 2.0) -> Dict[str, Dict[str, Any]]:
        wanted = self._symbols(symbols)
        if not wanted:
            return {}
        now = time.monotonic()
        with self._lock:
            missing = [s for s in wanted if now - self._updated_monotonic.get(s, 0) > max_age]
            if not missing:
                return {s: dict(self._quotes[s]) for s in wanted if s in self._quotes}

        # Collapse simultaneous page/account/heartbeat requests into one upstream call.
        with self._fetch_lock:
            now = time.monotonic()
            with self._lock:
                missing = [s for s in wanted if now - self._updated_monotonic.get(s, 0) > max_age]
            if missing:
                try:
                    regular = [s for s in missing if s not in _INDEX_TX]
                    indexes = [s for s in missing if s in _INDEX_TX]
                    fresh, errors = self._fetch_regular(regular)
                    for symbol in indexes:
                        quote, error = self._fetch_index(symbol)
                        if quote is not None:
                            fresh[symbol] = quote
                        if error:
                            errors.append(error)
                    fetched_at = time.monotonic()
                    with self._lock:
                        for symbol, quote in fresh.items():
                            self._quotes[symbol] = dict(quote)
                            self._updated_monotonic[symbol] = fetched_at
                        self._last_error = "; ".join(errors)
                except Exception as exc:
                    with self._lock:
                        self._last_error = str(exc)
        with self._lock:
            now = time.monotonic()
            return {
                s: dict(self._quotes[s]) for s in wanted
                if s in self._quotes and
                now - self._updated_monotonic.get(s, 0) <= self._stale_fallback_seconds
            }

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error


_instance: LiveQuoteService | None = None
_instance_lock = threading.Lock()


def get_live_quote_service() -> LiveQuoteService:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LiveQuoteService()
    return _instance
