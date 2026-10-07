# -*- coding: utf-8 -*-
"""
历史行情回填工具 (腾讯 ifzq 直连, 前复权)
==========================================
用途: 把日K历史一次性补齐, 解决"回测行情大量缺失/数据停在几周前"。

用法:
  python main.py backfill                    # 监控池+持仓+成交额Top150+指数
  python main.py backfill --all              # 全部已登记ETF(约1500只, 较慢)
  python main.py backfill --top 300 --days 900
  python main.py backfill --symbols 510300 159915

设计: 绕过 hub 的按类别限流(逐只2秒), 直接调用腾讯客户端并发拉取,
经数据质量校验后按 (symbol,date,source) 幂等落库。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from database import repository as repo

logger = logging.getLogger("data.backfill")

# 基准指数(收益曲线/市场诊断依赖)
INDEX_SYMBOLS = ["000300", "000905", "000001", "399006"]


def _resolve_symbols(all_universe: bool, top: int,
                     extra: Optional[List[str]] = None) -> Dict[str, str]:
    """返回 {symbol: asset_type}。"""
    out: Dict[str, str] = {}
    if all_universe:
        for s in repo.get_universe("etf"):
            out[s.symbol] = "etf"
        for s in repo.get_universe("stock"):
            out[s.symbol] = "stock"
    else:
        for item in repo.get_watchlist(enabled_only=True):
            out[str(item["symbol"])] = str(item.get("asset_type") or "etf")
        for p in repo.get_positions():
            out.setdefault(p.symbol, "etf")
        try:
            from data_service.market_data_service import get_market_service
            spot = get_market_service().get_etf_spot()
            ranked = sorted(spot, key=lambda x: x.get("amount", 0) or 0,
                            reverse=True)[:max(1, top)]
            for s in ranked:
                out.setdefault(str(s.get("symbol")), "etf")
        except Exception as exc:
            logger.warning("成交额Top列表获取失败(仅回填监控池/持仓): %s", exc)
    for s in (extra or []):
        out.setdefault(str(s), "etf")
    for s in INDEX_SYMBOLS:
        out.setdefault(s, "index")
    return out


def backfill_history(days: int = 700, top: int = 150, all_universe: bool = False,
                     symbols: Optional[List[str]] = None, workers: int = 2,
                     min_interval: float = 0.15) -> Dict[str, Any]:
    """回填历史日K。返回统计结果。

    数据源链: 新浪(1023根, 稳定) → 腾讯 ifzq(前复权, 高频会被反爬) → baostock。
    """
    from data_service.data_quality import ALLOWED_QUALITY, get_quality_checker
    from data_sources.sina_client import SinaClient
    from data_sources.tencent_client import TencentClient

    targets = _resolve_symbols(all_universe, top, symbols)
    end = date.today()
    start = end - timedelta(days=max(60, int(days)))
    sources = []
    for name, client in (("sina", SinaClient()), ("tencent", TencentClient())):
        sources.append((name, client))
    try:
        from data_sources.baostock_client import BaostockClient
        sources.append(("baostock", BaostockClient()))
    except Exception:
        pass
    qc = get_quality_checker()

    stats = {"total": len(targets), "ok": 0, "failed": 0, "skipped": 0,
             "bars": 0, "by_source": {}, "errors": [],
             "started": str(start), "ended": str(end)}
    t0 = time.time()

    def _one(symbol: str, asset_type: str):
        last_err = "无可用数据源"
        for name, client in sources:
            try:
                if asset_type == "index":
                    bars = client.get_index_bars(symbol, start, end)
                else:
                    bars = client.get_daily_bars(symbol, start, end, asset_type)
                if not bars:
                    last_err = f"{name}: 空结果"
                    continue
                rep = qc.check_daily_bars(symbol, bars)
                if rep.status not in ALLOWED_QUALITY:
                    last_err = f"{name}: 质量不合格 {rep.status}"
                    continue
                for b in bars:
                    b["quality_status"] = rep.status
                repo.upsert_daily_bars(bars)
                return symbol, len(bars), name, None
            except Exception as exc:  # noqa: BLE001
                last_err = f"{name}: {str(exc)[:120]}"
                continue
        return symbol, 0, "", last_err

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = {ex.submit(_one, s, t): s for s, t in targets.items()}
        done = 0
        for fut in as_completed(futures):
            symbol, n, src, err = fut.result()
            done += 1
            if err:
                stats["failed"] += 1
                if len(stats["errors"]) < 30:
                    stats["errors"].append(f"{symbol}: {err}")
            else:
                stats["ok"] += 1
                stats["bars"] += n
                stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
            if min_interval > 0:
                time.sleep(min_interval / max(1, workers))
            if done % 50 == 0 or done == stats["total"]:
                logger.info("回填进度 %d/%d (成功%d 失败%d, 共%d根K线, %.0fs)",
                            done, stats["total"], stats["ok"], stats["failed"],
                            stats["bars"], time.time() - t0)

    stats["elapsed_seconds"] = round(time.time() - t0, 1)
    logger.info("历史回填完成: %s", {k: v for k, v in stats.items() if k != "errors"})
    return stats
