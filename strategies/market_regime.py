# -*- coding: utf-8 -*-
"""
市场状态识别 (确定性规则, 不依赖 LLM)
=====================================
用途: 让模拟盘轮动根据市场环境自动切换参数预设(regime switch)。

状态定义(基于基准指数日K, 默认沪深300):
  risk_on : close > MA20 > MA60 且 20日动量 >= risk_on_mom
  risk_off: close < MA20 且 20日动量 <= risk_off_mom
  其他    : neutral

防抖: 状态需连续 confirm_days 个交易日成立才切换(避免来回打脸)。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings

logger = logging.getLogger("strategy.regime")

STATES = ("risk_on", "neutral", "risk_off")


def _cfg(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(get_settings().get("strategies.live_rotation.regime_switch", {}) or {})
    # 运行时覆盖(Web 设置页写入 system_state), 优先级: 显式参数 > 运行时 > config.yaml
    try:
        from database import repository as repo
        runtime = repo.get_system_state("regime_switch_config") or {}
        if isinstance(runtime, dict):
            for k in ("enabled", "index", "ma_short", "ma_long", "risk_on_mom",
                      "risk_off_mom", "confirm_days", "sticky", "min_switch_days"):
                if k in runtime:
                    base[k] = runtime[k]
            if runtime.get("presets"):
                base["presets"] = dict(runtime["presets"])
    except Exception:
        pass
    base.update(overrides or {})
    return {
        "enabled": bool(base.get("enabled", False)),
        "index": str(base.get("index", "000300")),
        "ma_short": int(base.get("ma_short", 20)),
        "ma_long": int(base.get("ma_long", 60)),
        "risk_on_mom": float(base.get("risk_on_mom", 0.02)),
        "risk_off_mom": float(base.get("risk_off_mom", -0.03)),
        "confirm_days": max(1, int(base.get("confirm_days", 5))),
        "sticky": bool(base.get("sticky", True)),
        "min_switch_days": max(0, int(base.get("min_switch_days", 20))),
        "manual_preset": str(base.get("manual_preset", "") or ""),
        "presets": dict(base.get("presets") or {}),
    }


def runtime_config() -> Dict[str, Any]:
    """当前生效的切换配置(含运行时覆盖), 供 API/UI 展示与编辑。"""
    return _cfg()


def save_runtime_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """保存运行时覆盖到 system_state(白名单字段 + 类型校验)。"""
    from database import repository as repo
    allowed = {"enabled", "index", "ma_short", "ma_long", "risk_on_mom",
               "risk_off_mom", "confirm_days", "sticky", "min_switch_days",
               "presets", "manual_preset"}
    clean: Dict[str, Any] = {}
    for k, v in (updates or {}).items():
        if k not in allowed:
            continue
        if k == "presets":
            if not isinstance(v, dict):
                raise ValueError("presets 必须为 {state: 策略名} 字典")
            clean[k] = {str(kk): str(vv or "") for kk, vv in v.items()
                        if str(kk) in STATES}
        elif k == "enabled":
            clean[k] = bool(v)
        elif k == "sticky":
            clean[k] = bool(v)
        elif k in ("ma_short", "ma_long", "confirm_days", "min_switch_days"):
            clean[k] = max(0, int(v))
        elif k in ("risk_on_mom", "risk_off_mom"):
            clean[k] = float(v)
        elif k in ("index", "manual_preset"):
            clean[k] = str(v or "")
    def _upd(state):
        state.clear()
        state.update(clean)
    repo.update_system_state("regime_switch_config", _upd)
    return _cfg()


def clear_runtime_config() -> Dict[str, Any]:
    """清除运行时覆盖, 回到 config.yaml。"""
    from database import repository as repo
    repo.update_system_state("regime_switch_config",
                             lambda state: state.clear())
    return _cfg()


def append_history(entry: Dict[str, Any], keep: int = 100):
    """记录一次策略切换(供页面回看"当时用的是什么策略")。"""
    try:
        from database import repository as repo
        def _upd(state):
            items = list((state or {}).get("items") or [])
            items.append(entry)
            state.clear()
            state["items"] = items[-keep:]
        repo.update_system_state("regime_switch_history", _upd)
    except Exception as exc:
        logger.warning("切换历史写入失败: %s", exc)


def get_history(limit: int = 50) -> list:
    try:
        from database import repository as repo
        items = (repo.get_system_state("regime_switch_history") or {}).get("items") or []
        return list(items)[-max(1, int(limit)):]
    except Exception:
        return []


def _raw_state(closes: List[float], cfg: Dict[str, Any]) -> str:
    if len(closes) < max(cfg["ma_long"], 21):
        return "neutral"
    close = closes[-1]
    ma_s = sum(closes[-cfg["ma_short"]:]) / cfg["ma_short"]
    ma_l = sum(closes[-cfg["ma_long"]:]) / cfg["ma_long"]
    mom = closes[-1] / closes[-21] - 1 if closes[-21] > 0 else 0.0
    if close > ma_s > ma_l and mom >= cfg["risk_on_mom"]:
        return "risk_on"
    if close < ma_s and mom <= cfg["risk_off_mom"]:
        return "risk_off"
    return "neutral"


def detect_regime(overrides: Optional[Dict[str, Any]] = None,
                  asof: Optional[date] = None) -> Dict[str, Any]:
    """返回当前市场状态: {state, raw_state, close, ma_short, ma_long, mom20, asof, reason}

    重要: 只使用"已完成收盘"的日K(15:05前剔除当日盘中K线), 且按数据截止日
    冻结(同一交易日内结果不变), 避免盘中短时波动让状态来回跳。
    """
    from core.timeutil import completed_daily_bars
    cfg = _cfg(overrides)
    asof = asof or date.today()
    bars: List[Any] = []
    try:
        from database import repository as repo
        rows = repo.get_daily_bars(cfg["index"], asof - timedelta(days=200), asof)
        rows = completed_daily_bars(list(rows))
        bars = [(r.trade_date, float(r.close or 0)) for r in rows if (r.close or 0) > 0]
    except Exception as exc:
        logger.warning("市场状态读取指数失败 %s: %s", cfg["index"], exc)
    if not bars:
        try:
            from data_service.market_data_service import get_market_service
            raw = get_market_service().get_index_bars(
                cfg["index"], asof - timedelta(days=200), asof)
            raw = completed_daily_bars(list(raw))
            bars = [(b["trade_date"], float(b["close"])) for b in raw
                    if float(b.get("close") or 0) > 0]
        except Exception as exc:
            logger.warning("市场状态回源失败 %s: %s", cfg["index"], exc)
    if not bars:
        return {"state": "neutral", "raw_state": "neutral", "reason": "无指数行情, 中性处理",
                "asof": str(asof), "index": cfg["index"], "config": cfg}

    # 同一数据截止日 + 相同配置 → 复用冻结结果(日内不变)
    data_asof = str(bars[-1][0])[:10]
    sig = (f"{cfg['index']}|{cfg['ma_short']}|{cfg['ma_long']}|{cfg['risk_on_mom']}|"
           f"{cfg['risk_off_mom']}|{cfg['confirm_days']}|{int(cfg['sticky'])}|{data_asof}")
    try:
        from database import repository as repo
        frozen = repo.get_system_state("market_regime_daily") or {}
        if frozen.get("signature") == sig and frozen.get("result"):
            result = dict(frozen["result"])
            result["config"] = cfg
            return result
    except Exception:
        pass

    closes = [c for _, c in bars]
    raw = _raw_state(closes, cfg)
    # 防抖: sticky=True 时按日重建(只用历史)并保持上一状态, 与离线验证口径一致;
    # sticky=False 时 risk_off 立即生效(保护本金), risk_on 需确认(避免追高)。
    if cfg.get("sticky", True) and cfg["confirm_days"] > 1:
        start_i = max(cfg["ma_long"], 21)
        state = raw
        if len(closes) >= start_i:
            prev = "neutral"
            for i in range(start_i, len(closes) + 1):
                w = closes[:i]
                r = _raw_state(w, cfg)
                stable = all(_raw_state(w[:-k], cfg) == r
                             for k in range(1, min(cfg["confirm_days"], len(w))))
                prev = r if stable else prev
            state = prev
    else:
        state = raw
        if raw == "risk_on" and cfg["confirm_days"] > 1:
            for k in range(1, cfg["confirm_days"]):
                if len(closes) > k and _raw_state(closes[:-k], cfg) != raw:
                    state = "neutral"
                    break
    ma_s = sum(closes[-cfg["ma_short"]:]) / cfg["ma_short"] if len(closes) >= cfg["ma_short"] else 0
    ma_l = sum(closes[-cfg["ma_long"]:]) / cfg["ma_long"] if len(closes) >= cfg["ma_long"] else 0
    mom = closes[-1] / closes[-21] - 1 if len(closes) > 21 and closes[-21] > 0 else 0.0
    reason = (f"{cfg['index']} close={closes[-1]:.2f} MA{cfg['ma_short']}={ma_s:.2f} "
              f"MA{cfg['ma_long']}={ma_l:.2f} 20日动量={mom:+.2%} → {raw}"
              + (f"(防抖后{state})" if state != raw else "")
              + f" | 数据截止 {data_asof}(收盘口径, 盘中不变)")
    result = {"state": state, "raw_state": raw, "close": round(closes[-1], 4),
              "ma_short": round(ma_s, 4), "ma_long": round(ma_l, 4),
              "mom20": round(mom, 5), "asof": data_asof,
              "index": cfg["index"], "reason": reason, "config": cfg}
    # 冻结当日结果(同一数据截止日+同配置下, 日内多次调用结果一致)
    try:
        from database import repository as repo
        snapshot = {k: v for k, v in result.items() if k != "config"}
        repo.update_system_state("market_regime_daily", lambda state_: (
            state_.clear(),
            state_.update({"signature": sig, "asof": data_asof,
                           "state": state, "result": snapshot})))
    except Exception as exc:
        logger.debug("市场状态冻结写入失败: %s", exc)
    return result


def resolve_preset_for_regime(active_preset: str = "",
                              overrides: Optional[Dict[str, Any]] = None
                              ) -> Dict[str, Any]:
    """按市场状态选预设名。返回 {preset, regime, reason, mapped}。

    - regime_switch 未启用/映射缺失 → 回退 active_preset;
    - 映射到不存在的预设 → 回退 active_preset(记录原因)。
    """
    from core.config import ROOT_DIR

    cfg = _cfg(overrides)
    if not cfg["enabled"]:
        return {"preset": active_preset, "regime": None, "mapped": False,
                "manual": False, "reason": "regime_switch 未启用"}
    regime = detect_regime(cfg)
    state = regime.get("state", "neutral")

    def _preset_exists(name: str) -> bool:
        if not name:
            return False
        try:
            import json
            from core.config import ROOT_DIR
            store = json.loads((ROOT_DIR / "data" / "strategy_presets.json")
                               .read_text(encoding="utf-8")) or {}
            p = (store.get("presets") or {}).get(name) or {}
            return str(p.get("universe_mode") or "") == "dynamic_etf"
        except Exception:
            return False

    manual = str(cfg.get("manual_preset") or "")
    if manual:
        if _preset_exists(manual):
            return {"preset": manual, "regime": regime, "mapped": True,
                    "manual": True, "reason": f"手动指定策略: {manual}"}
        return {"preset": active_preset, "regime": regime, "mapped": False,
                "manual": False, "reason": f"手动指定策略 {manual} 不存在, 回退当前策略"}

    want = str(cfg["presets"].get(state) or "")
    if not want:
        return {"preset": active_preset, "regime": regime, "mapped": False,
                "manual": False, "reason": f"{state} 无映射策略, 回退当前策略"}
    if not _preset_exists(want):
        return {"preset": active_preset, "regime": regime, "mapped": False,
                "manual": False, "reason": f"映射策略 {want} 不存在或非动态池, 回退"}
    return {"preset": want, "regime": regime, "mapped": True, "manual": False,
            "reason": f"{state} → {want}"}
