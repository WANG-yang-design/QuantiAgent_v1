# -*- coding: utf-8 -*-
"""
通知服务: 9 类邮件(文档 15.1)
==============================
1. 盘前报告  2. 盘中风险提醒  3. 交易计划  4. 模拟成交确认
5. 异常告警  6. 收盘复盘  7. 周报  8. 月报  9. 回测报告
模板: HTML 卡片式(手机友好), 复用 AAgent demo 的样式经验。
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from notification.email_sender import get_email_sender

logger = logging.getLogger("notification.service")

_CSS = """
body{margin:0;padding:12px;background:#f2f2f7;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}
.wrap{max-width:520px;margin:auto}
.header{color:#fff;border-radius:10px 10px 0 0;padding:14px 16px}
.header h2{margin:0;font-size:18px}.header p{margin:4px 0 0;font-size:12px;opacity:.85}
.card{background:#fff;border-radius:10px;margin:10px 0;padding:14px 16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f0f0f0;font-size:13px}
.row:last-child{border-bottom:none}.label{color:#888}.val{font-weight:600;color:#222;text-align:right}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:700;margin:6px 0}
.badge.BUY{background:#d4edda;color:#1c7a3e}.badge.SELL{background:#f8d7da;color:#c0392b}
.badge.HOLD{background:#fff3cd;color:#856404}.badge.APPROVE{background:#d4edda;color:#1c7a3e}
.badge.REJECT{background:#f8d7da;color:#c0392b}.badge.CONFIRM_REQUIRED{background:#fff3cd;color:#856404}
.reason{background:#f9f9f9;border-radius:6px;padding:8px 10px;margin-top:8px;font-size:12px;color:#444;line-height:1.6}
.footer{text-align:center;font-size:11px;color:#aaa;padding:8px 0 4px}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0}
.stat-box{background:#f9f9f9;border-radius:8px;padding:10px;text-align:center}
.stat-num{font-size:20px;font-weight:700;color:#111}.stat-lbl{font-size:11px;color:#888;margin-top:2px}
"""


def _wrap(title: str, subtitle: str, body: str, color: str = "#1c7a3e") -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style></head><body><div class="wrap">
<div class="header" style="background:{color}"><h2>{title}</h2><p>{subtitle}</p></div>
{body}
<div class="footer">此邮件由多Agent量化交易系统自动发出, 仅供参考, 不构成投资建议。</div>
</div></body></html>"""


def _card(rows: List[tuple], badge: str = "", reason: str = "") -> str:
    rows_html = "".join(
        f'<div class="row"><span class="label">{k}</span><span class="val">{v}</span></div>'
        for k, v in rows)
    badge_html = f'<div><span class="badge {badge}">{badge}</span></div>' if badge else ""
    reason_html = f'<div class="reason">{reason}</div>' if reason else ""
    return f'<div class="card">{badge_html}{rows_html}{reason_html}</div>'


class NotificationService:
    """通知服务: 邮件类型分发。"""

    def __init__(self):
        self.mail = get_email_sender()

    # ------------------------------------------------------------------
    @staticmethod
    def _plan_source(plan: Dict[str, Any]) -> str:
        """订单/计划来源: 持仓风控巡检(PLAN-PM-) vs Agent决策。
        修复: 用户在邮件里分不清操作是谁发起的。"""
        plan_id = str(plan.get("plan_id", ""))
        if plan_id.startswith("PLAN-PM") or plan_id.startswith("PLAN-POS"):
            return "持仓风控巡检(硬止损/止盈/降仓)"
        return "Agent决策链路"

    @staticmethod
    def _confirm_links(confirm_id: str) -> tuple:
        """人工确认邮件按钮链接(带HMAC签名, 点开即确认, 无需登录Web)。
        修复: 原实现写死 127.0.0.1 —— 手机/其他设备打开邮件点确认会
        打到自己设备上。改用 config.yaml web.public_base_url(可配局域网IP)。"""
        import hashlib
        import hmac as _hmac
        from core.config import get_settings
        token = get_settings().get("web.admin_token", "quantiagent-admin")
        web = get_settings().section("web")
        base = str(web.get("public_base_url", "") or
                   f"http://127.0.0.1:{int(web.get('port', 8080))}").rstrip("/")

        def link(decision: str) -> str:
            sig = _hmac.new(token.encode(), f"{confirm_id}:{decision}".encode(),
                            hashlib.sha256).hexdigest()[:16]
            return f"{base}/api/confirmations/{confirm_id}/email?decision={decision}&sig={sig}"
        return link("approve"), link("reject")

    # ---------------- 1. 交易计划邮件 (文档15.2 全字段) ----------------
    def send_trade_plan_email(self, plan: Dict[str, Any],
                              risk: Optional[Dict[str, Any]] = None,
                              confirm_id: str = "", reason: str = ""):
        risk = risk or {}
        source = self._plan_source(plan)
        rows = [
            ("交易动作", plan.get("action", "")),
            ("标的代码", plan.get("symbol", "")),
            ("标的名称", plan.get("name", "") or "-"),
            ("计划数量", f"{plan.get('estimated_quantity', 0)} 份"),
            ("计划价格", f"{plan.get('limit_price', '市价')}"),
            ("预计金额", f"¥{plan.get('order_amount', 0):,.0f}"),
            ("操作来源", source),
            ("风控结论", f"{risk.get('risk_decision', '')} ({risk.get('risk_level', '')})"),
            ("是否需要人工确认", "是" if risk.get("risk_decision") in
             ("CONFIRM_REQUIRED", "REDUCE") else "否"),
        ]
        # 需要人工确认的原因(修复: 原邮件看不到为什么需要确认)
        need_confirm = risk.get("risk_decision") in ("CONFIRM_REQUIRED", "REDUCE")
        confirm_reason = reason or risk.get("blocked_reason", "") or ""
        extra = ""
        if need_confirm and confirm_id:
            approve_url, reject_url = self._confirm_links(confirm_id)
            extra = f"""
<div class="card">
  <div class="reason" style="border-left:3px solid #f59f00;margin-bottom:10px">
    <b>为什么需要人工确认:</b><br/>{confirm_reason}</div>
  <div style="display:flex;gap:8px">
    <a href="{approve_url}" style="flex:1;text-align:center;padding:10px;border-radius:8px;
       background:#1c7a3e;color:#fff;text-decoration:none;font-weight:700;font-size:14px">✅ 批准交易</a>
    <a href="{reject_url}" style="flex:1;text-align:center;padding:10px;border-radius:8px;
       background:#c0392b;color:#fff;text-decoration:none;font-weight:700;font-size:14px">❌ 拒绝交易</a>
  </div>
  <div style="font-size:11px;color:#999;margin-top:6px">点击按钮后立即生效, 无需登录网页。若已在网页处理过, 再次点击将提示"已处理"。
  若2分钟内无人处理, 系统将按轮动策略方向自动执行(策略给买入信号→自动批准; 否则自动拒绝)。</div>
</div>"""
        elif confirm_reason:
            extra = f'<div class="reason" style="border-left:3px solid #f59f00"><b>原因:</b> {confirm_reason}</div>'
        body = _wrap("📋 交易计划", datetime.now().strftime("%Y-%m-%d %H:%M"),
                     _card(rows, str(plan.get("action", "")),
                           "；".join(str(x) for x in (plan.get("reasons") or [])[:6])) + extra)
        self.mail.send_email(
            f"【交易计划】{source.split('(')[0]} {plan.get('action')} "
            f"{plan.get('symbol', '')} {plan.get('name', '')}",
            body, dedup_key=f"plan:{plan.get('plan_id', '')}", dedup_minutes=1440)

    # ---------------- 2. 盘中风险提醒 ----------------
    def send_risk_alert_email(self, alerts: List[Dict[str, Any]]):
        if not alerts:
            return
        cards = "".join(
            _card([
                ("标的", f"{a.get('symbol', '')} {a.get('name', '')}"),
                ("操作来源", "持仓风控巡检"),
                ("风险", a.get("risk", "")),
                ("触发时间", a.get("time", datetime.now().strftime("%H:%M"))),
            ], "REJECT", a.get("detail", "")) for a in alerts)
        body = _wrap("⚠️ 盘中风险提醒", f"{len(alerts)} 条 · {datetime.now():%Y-%m-%d %H:%M}",
                     cards, color="#e67e22")
        # 修复: 去重键用批次摘要(全部标的+时间窗), 原实现只用第一个标的,
        # 不同标的的多条告警会被错误合并去重吞掉
        batch_key = ",".join(sorted({str(a.get("symbol", "")) for a in alerts}))
        self.mail.send_email(f"【量化风控】盘中风险提醒 {len(alerts)}条",
                             body,
                             dedup_key=f"risk:{batch_key}:{datetime.now():%Y%m%d%H}",
                             dedup_minutes=60)

    # ---------------- 3. 模拟成交确认 ----------------
    def send_trade_confirmation_email(self, trade: Dict[str, Any],
                                      account: Dict[str, Any],
                                      source: str = "Agent决策"):
        pnl = trade.get("pnl")
        rows = [
            ("成交方向", trade.get("side", "")),
            ("标的代码", trade.get("symbol", "")),
            ("标的名称", trade.get("name", "") or "-"),
            ("成交价", f"{trade.get('price', 0):.3f}"),
            ("成交数量", f"{trade.get('qty', 0)}"),
            ("手续费", f"¥{trade.get('fee', 0):.2f}"),
            ("已实现盈亏", f"{pnl:+,.2f}" if pnl is not None else "-"),
            ("操作来源", source),
            ("成交时间", str(trade.get("trade_time", ""))[:19]),
            ("总资产", f"¥{account.get('total_asset', 0):,.0f}"),
            ("累计盈亏", f"¥{account.get('total_pnl', 0):+,.2f}"),
        ]
        body = _wrap("✅ 模拟成交确认", datetime.now().strftime("%Y-%m-%d %H:%M"),
                     _card(rows, str(trade.get("side", ""))))
        self.mail.send_email(f"【模拟成交】{source.split('(')[0]} {trade.get('side')} "
                             f"{trade.get('symbol', '')} {trade.get('qty', 0)}份",
                             body, dedup_key=f"trade:{trade.get('trade_id', '')}", dedup_minutes=1440)

    # ---------------- 4. 异常告警 ----------------
    def send_alert_email(self, subject: str, detail: str, level: str = "WARN"):
        body = _wrap("🚨 系统异常告警", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     _card([("级别", level), ("详情", detail)]), color="#c0392b")
        self.mail.send_email(f"【量化告警】{subject}", body,
                             dedup_key=f"alert:{subject}", dedup_minutes=120)

    # ---------------- 5. 收盘复盘日报 ----------------
    def send_daily_report_email(self, stats: Dict[str, Any], review: Dict[str, Any],
                                report_path: str = ""):
        day_pnl = float(stats.get("day_pnl", 0) or 0)
        color = "#1c7a3e" if day_pnl >= 0 else "#c0392b"
        stat_html = f"""
<div class="stat-grid">
  <div class="stat-box"><div class="stat-num">{stats.get('trade_count', 0)}</div><div class="stat-lbl">成交笔数</div></div>
  <div class="stat-box"><div class="stat-num" style="color:{color}">{day_pnl:+,.0f}</div><div class="stat-lbl">当日盈亏</div></div>
  <div class="stat-box"><div class="stat-num">{stats.get('total_asset', 0):,.0f}</div><div class="stat-lbl">总资产</div></div>
  <div class="stat-box"><div class="stat-num">{stats.get('fee_total', 0):.0f}</div><div class="stat-lbl">手续费</div></div>
</div>"""
        review_html = f'<div class="reason">📝 {review.get("review_summary", "")}</div>' if review else ""
        body = _wrap("📊 每日收盘复盘", f"{stats.get('date', '')} · 收盘",
                     stat_html + review_html, color="#2980b9")
        self.mail.send_email(f"【量化日报】{stats.get('date', '')} 复盘 "
                             f"盈亏{day_pnl:+,.0f}元",
                             body, dedup_key=f"daily:{stats.get('date', '')}", dedup_minutes=1440)

    # ---------------- 6. 回测报告 ----------------
    def send_backtest_report_email(self, metrics: Dict[str, Any]):
        m = metrics
        rows = [
            ("总收益", f"{m.get('total_return', 0):+.2%}"),
            ("年化收益", f"{m.get('annual_return', 0):+.2%}"),
            ("最大回撤", f"{m.get('max_drawdown', 0):.2%}"),
            ("夏普比率", f"{m.get('sharpe', 0):.2f}"),
            ("卡玛比率", f"{m.get('calmar', 0):.2f}"),
            ("胜率", f"{m.get('win_rate', 0):.1%}"),
            ("交易次数", f"{m.get('trade_count', 0)}"),
            ("超额收益", f"{m.get('benchmark', {}).get('excess_return', 0):+.2%}"),
        ]
        body = _wrap("🧪 回测报告", f"run_id: {m.get('run_id', '')}",
                     _card(rows))
        self.mail.send_email(f"【回测报告】总收益{m.get('total_return', 0):+.2%} "
                             f"回撤{m.get('max_drawdown', 0):.2%}",
                             body, dedup_key=f"backtest:{m.get('run_id', '')}", dedup_minutes=1440)

    # ---------------- 7. 盘前报告 ----------------
    def send_premarket_email(self, universe: List[Dict[str, Any]]):
        if not universe:
            return
        rows = [(f"{u.get('symbol')} {u.get('name', '')}", f"关注: {u.get('reason', '-')}")
                for u in universe[:15]]
        body = _wrap("🌅 盘前报告", datetime.now().strftime("%Y-%m-%d %H:%M"),
                     _card(rows))
        self.mail.send_email("【量化盘前】今日关注标的", body,
                             dedup_key="premarket", dedup_minutes=1440)


_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    global _service
    if _service is None:
        _service = NotificationService()
    return _service
