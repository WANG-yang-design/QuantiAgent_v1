# -*- coding: utf-8 -*-
"""
市场状态摘要构建器
==================
核心思想(文档第四条原则): 大模型只处理"摘要后的状态"。
原始K线/盘口/新闻 → 脚本计算特征 → 生成紧凑文本摘要 → 才交给 Agent。
这同时控制 token 成本并提高输出稳定性。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional


def build_market_summary(symbol: str, name: str, tech: Dict[str, Any],
                         etf: Optional[Dict[str, Any]] = None,
                         quote: Optional[Dict[str, Any]] = None,
                         money_flow: Optional[Dict[str, Any]] = None,
                         news_summary: Optional[Dict[str, Any]] = None,
                         sentiment: Optional[Dict[str, Any]] = None,
                         market_env: Optional[Dict[str, Any]] = None,
                         position: Optional[Dict[str, Any]] = None,
                         strategy_signal: Optional[Dict[str, Any]] = None,
                         order_book: Optional[Dict[str, Any]] = None,
                         intraday: Optional[Dict[str, Any]] = None) -> str:
    """
    生成 Agent 可读的中文市场状态摘要。全部字段来自特征层(脚本计算)。
    """
    if not tech:
        return f"{symbol} {name}: 无可用特征数据"

    lines: List[str] = [f"{symbol} {name} 市场状态摘要 @ {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    lines.append(f"最新价:{tech.get('close', 0):.3f}  涨跌幅:{tech.get('change_pct', 0):+.2f}%")

    # 数值安全: 字段可能为 None/NaN(修复: 原实现 None/1e4 直接 TypeError 崩溃)
    def _f(v, default=0.0):
        try:
            x = float(v)
            return x if x == x else default
        except (TypeError, ValueError):
            return default

    # 技术面
    t = tech
    ma_lines = []
    if t.get("ma5"): ma_lines.append(f"MA5={_f(t['ma5']):.3f}")
    if t.get("ma20"): ma_lines.append(f"MA20={_f(t['ma20']):.3f}")
    if t.get("ma60"): ma_lines.append(f"MA60={_f(t['ma60']):.3f}")
    if ma_lines:
        align = "多头排列" if t.get("bull_align") else ("空头排列" if t.get("bear_align") else "缠绕")
        lines.append(f"均线({'/'.join(ma_lines)}, {align})")
    lines.append(
        f"MACD:dif={_f(t.get('macd_dif')):.4f}/dea={_f(t.get('macd_dea')):.4f}"
        f"{'金叉' if t.get('macd_gold_cross') else ''}{'死叉' if t.get('macd_dead_cross') else ''} "
        f"RSI={_f(t.get('rsi')):.0f}{'(超买)' if t.get('rsi_overbought') else ''}"
        f"{'(超卖)' if t.get('rsi_oversold') else ''}")
    lines.append(
        f"动量:5日{_f(t.get('momentum_5d')):+.2%}/20日{_f(t.get('momentum_20d')):+.2%}/"
        f"60日{_f(t.get('momentum_60d')):+.2%}  波动率20d={_f(t.get('volatility_20d')):.1%} "
        f"60日最大回撤={_f(t.get('max_drawdown_60d')):.1%}")
    lines.append(
        f"量比={_f(t.get('volume_ratio')):.2f} 5日均额={_f(t.get('amount_ma5')) / 1e4:.0f}万 "
        f"20日均额={_f(t.get('amount_ma20')) / 1e4:.0f}万 "
        f"VWAP={_f(t.get('vwap')):.3f}{'(价在VWAP上)' if t.get('price_above_vwap') else '(价在VWAP下)'}")
    if t.get("breakout_20d"):
        lines.append("信号:突破20日高点!")
    if t.get("breakdown_20d"):
        lines.append("信号:跌破20日低点!")
    lines.append(
        f"支撑={_f(t.get('support_20d')):.3f} 压力={_f(t.get('resistance_20d')):.3f} "
        f"{'接近压力' if t.get('near_resistance') else ''}{'接近支撑' if t.get('near_support') else ''}")

    # ETF 专项
    if etf:
        lines.append(
            f"ETF:溢价率={_f(etf.get('premium_rate')):+.2%} "
            f"IOPV偏离={_f(etf.get('iopv_deviation')):+.2%} "
            f"流动性评分={_f(etf.get('liquidity_score')):.0f}"
            f"{' QDII跨境' if etf.get('is_qdii') else ''}"
            f" 跟踪指数={etf.get('tracking_index', '')}")

    # 资金流
    if money_flow:
        lines.append(
            f"资金流:主力净流入={_f(money_flow.get('main_inflow')) / 1e4:.0f}万 "
            f"方向={'流入' if money_flow.get('flow_direction') == 'in' else '流出'}")

    # 新闻/情绪(已由服务层预处理)
    if news_summary:
        lines.append(
            f"新闻:近{news_summary.get('count', 0)}条 "
            f"平均情绪={_f(news_summary.get('avg_sentiment')):+.2f} "
            f"风险公告={news_summary.get('risk_announcements', 0)}条")
    if sentiment:
        lines.append(
            f"舆情:热度={_f(sentiment.get('heat')):.0f} 平均分={_f(sentiment.get('avg_score')):+.2f} "
            f"负向占比={_f(sentiment.get('negative_ratio')):.0%}")

    # 市场环境
    if market_env:
        lines.append(
            f"市场环境:{market_env.get('state_desc', '')} "
            f"(指数动量20d={_f(market_env.get('index_momentum_20d')):+.2%})")

    # 持仓
    if position:
        lines.append(
            f"持仓:{position.get('total_qty', 0)}股 可用{position.get('available_qty', 0)}股 "
            f"成本{position.get('cost_price', 0):.3f} "
            f"浮盈{position.get('pnl_pct', 0):+.2%}")

    # 策略信号(仅供参考, 修复: 展示完整依据让 Agent 可复核策略排名逻辑)
    if strategy_signal:
        lines.append(
            f"策略参考信号:{strategy_signal.get('signal', '')} "
            f"score={strategy_signal.get('score', 0):.2f} "
            f"- {strategy_signal.get('reason', '')[:80]}")

    # 当日分时(修复: 原来只喂日K特征, AI 看不到盘中走势)
    if intraday and intraday.get("available"):
        lines.append(
            f"分时:今日{intraday.get('point_count', 0)}分钟 "
            f"涨跌{_f(intraday.get('day_change_pct')):+.2f}% "
            f"振幅{_f(intraday.get('intraday_amplitude_pct')):.2f}% "
            f"最高{_f(intraday.get('intraday_high')):.3f}/最低{_f(intraday.get('intraday_low')):.3f}")
        lines.append(
            f"分时均价VWAP={_f(intraday.get('vwap')):.3f} "
            f"现价偏离均价{_f(intraday.get('price_vs_vwap_pct')):+.2f}% "
            f"上午{_f(intraday.get('morning_change_pct')):+.2f}% "
            f"下午{_f(intraday.get('afternoon_change_pct')):+.2f}%")
        lines.append(
            f"尾盘强弱:末30分钟量/前30分钟量={_f(intraday.get('tail_volume_ratio')):+.2f} "
            f"末分钟量比均值={_f(intraday.get('last_minute_volume_ratio')):.2f} "
            f"分时形态:{intraday.get('trend', '')}")

    return "\n".join(lines)
