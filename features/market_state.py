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
                         order_book: Optional[Dict[str, Any]] = None) -> str:
    """
    生成 Agent 可读的中文市场状态摘要。全部字段来自特征层(脚本计算)。
    """
    if not tech:
        return f"{symbol} {name}: 无可用特征数据"

    lines: List[str] = [f"{symbol} {name} 市场状态摘要 @ {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    lines.append(f"最新价:{tech.get('close', 0):.3f}  涨跌幅:{tech.get('change_pct', 0):+.2f}%")

    # 技术面
    t = tech
    ma_lines = []
    if t.get("ma5"): ma_lines.append(f"MA5={t['ma5']:.3f}")
    if t.get("ma20"): ma_lines.append(f"MA20={t['ma20']:.3f}")
    if t.get("ma60"): ma_lines.append(f"MA60={t['ma60']:.3f}")
    if ma_lines:
        align = "多头排列" if t.get("bull_align") else ("空头排列" if t.get("bear_align") else "缠绕")
        lines.append(f"均线({'/'.join(ma_lines)}, {align})")
    lines.append(
        f"MACD:dif={t.get('macd_dif', 0):.4f}/dea={t.get('macd_dea', 0):.4f}"
        f"{'金叉' if t.get('macd_gold_cross') else ''}{'死叉' if t.get('macd_dead_cross') else ''} "
        f"RSI={t.get('rsi', 0):.0f}{'(超买)' if t.get('rsi_overbought') else ''}"
        f"{'(超卖)' if t.get('rsi_oversold') else ''}")
    lines.append(
        f"动量:5日{t.get('momentum_5d', 0):+.2%}/20日{t.get('momentum_20d', 0):+.2%}/"
        f"60日{t.get('momentum_60d', 0):+.2%}  波动率20d={t.get('volatility_20d', 0):.1%} "
        f"60日最大回撤={t.get('max_drawdown_60d', 0):.1%}")
    lines.append(
        f"量比={t.get('volume_ratio', 0):.2f} 5日均额={t.get('amount_ma5', 0) / 1e4:.0f}万 "
        f"20日均额={t.get('amount_ma20', 0) / 1e4:.0f}万 "
        f"VWAP={t.get('vwap', 0):.3f}{'(价在VWAP上)' if t.get('price_above_vwap') else '(价在VWAP下)'}")
    if t.get("breakout_20d"):
        lines.append("信号:突破20日高点!")
    if t.get("breakdown_20d"):
        lines.append("信号:跌破20日低点!")
    lines.append(
        f"支撑={t.get('support_20d', 0):.3f} 压力={t.get('resistance_20d', 0):.3f} "
        f"{'接近压力' if t.get('near_resistance') else ''}{'接近支撑' if t.get('near_support') else ''}")

    # ETF 专项
    if etf:
        lines.append(
            f"ETF:溢价率={etf.get('premium_rate', 0):+.2%} "
            f"IOPV偏离={etf.get('iopv_deviation', 0):+.2%} "
            f"流动性评分={etf.get('liquidity_score', 0):.0f}"
            f"{' QDII跨境' if etf.get('is_qdii') else ''}"
            f" 跟踪指数={etf.get('tracking_index', '')}")

    # 资金流
    if money_flow:
        lines.append(
            f"资金流:主力净流入={money_flow.get('main_inflow', 0) / 1e4:.0f}万 "
            f"方向={'流入' if money_flow.get('flow_direction') == 'in' else '流出'}")

    # 新闻/情绪(已由服务层预处理)
    if news_summary:
        lines.append(
            f"新闻:近{news_summary.get('count', 0)}条 "
            f"平均情绪={news_summary.get('avg_sentiment', 0):+.2f} "
            f"风险公告={news_summary.get('risk_announcements', 0)}条")
    if sentiment:
        lines.append(
            f"舆情:热度={sentiment.get('heat', 0):.0f} 平均分={sentiment.get('avg_score', 0):+.2f} "
            f"负向占比={sentiment.get('negative_ratio', 0):.0%}")

    # 市场环境
    if market_env:
        lines.append(
            f"市场环境:{market_env.get('state_desc', '')} "
            f"(指数动量20d={market_env.get('index_momentum_20d', 0):+.2%})")

    # 持仓
    if position:
        lines.append(
            f"持仓:{position.get('total_qty', 0)}股 可用{position.get('available_qty', 0)}股 "
            f"成本{position.get('cost_price', 0):.3f} "
            f"浮盈{position.get('pnl_pct', 0):+.2%}")

    # 策略信号(仅供参考)
    if strategy_signal:
        lines.append(f"策略参考信号:{strategy_signal.get('strategy_id', '')} "
                     f"→ {strategy_signal.get('signal', '')} score={strategy_signal.get('score', 0):.2f}")

    return "\n".join(lines)
