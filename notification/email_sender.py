# -*- coding: utf-8 -*-
"""
邮件发送器 (强化版)
==================
在 AAgent demo(notify/email_notify.py)基础上强化:
- SSL(465)/STARTTLS(587) 双通道
- 发送重试(指数退避, 3次)
- 去重持久化(数据库通知表, 重启不重复)
- 异步发送队列(不阻塞主流程)
- 统一 HTML 模板(卡片式, 手机友好)
"""
import asyncio
import logging
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from core.config import get_settings
from database.db_session import get_session
from database.models import NotificationRecord

logger = logging.getLogger("notification.email")


class EmailSender:
    """SMTP 邮件发送器(带重试/去重/异步队列)。"""

    def __init__(self):
        self.cfg = get_settings().section("email")
        self.enabled = str(self.cfg.get("enabled", "false")).lower() in ("true", "1", "yes")
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._worker: Optional[asyncio.Task] = None
        self._lock = threading.Lock()
        self._dedup_cache: Dict[str, float] = {}

    # ------------------------------------------------------------------
    def is_enabled(self) -> bool:
        return self.enabled

    # ------------------------------------------------------------------
    def _send_sync(self, subject: str, html_body: str, extra_receivers: Optional[List[str]] = None) -> bool:
        """同步发送一封邮件(带重试)。"""
        if not self.enabled:
            logger.debug("邮件未启用, 跳过: %s", subject)
            return False
        sender = self.cfg.get("sender", "")
        password = self.cfg.get("sender_pass", "")
        receivers = [self.cfg.get("receiver", "")] if self.cfg.get("receiver") else []
        for r in (extra_receivers or []):
            if r and r not in receivers:
                receivers.append(r)
        if not sender or not password or not receivers:
            logger.warning("邮件配置不完整, 跳过: %s", subject)
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = sender
        msg["To"] = ", ".join(receivers)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        host = self.cfg.get("smtp_host", "smtp.qq.com")
        port = int(self.cfg.get("smtp_port", 465))
        last_err = None
        for attempt in range(3):
            try:
                if port == 465:
                    ctx = ssl.create_default_context()
                    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                        s.login(sender, password)
                        s.sendmail(sender, receivers, msg.as_string())
                else:
                    with smtplib.SMTP(host, port, timeout=20) as s:
                        s.ehlo()
                        s.starttls()
                        s.login(sender, password)
                        s.sendmail(sender, receivers, msg.as_string())
                logger.info("邮件发送成功: %s → %s", subject, receivers)
                return True
            except Exception as exc:
                last_err = exc
                logger.warning("邮件发送失败(第%d次) %s: %s", attempt + 1, subject, exc)
                time.sleep(2 ** attempt)
        logger.error("邮件发送最终失败 %s: %s", subject, last_err)
        return False

    # ------------------------------------------------------------------
    def send_email(self, subject: str, html_body: str,
                   extra_receivers: Optional[List[str]] = None,
                   dedup_key: Optional[str] = None,
                   dedup_minutes: int = 30) -> bool:
        """发送入口: 去重检查 + 异步入队。"""
        if dedup_key:
            dedup_key = f"{self.cfg.get('sender','')}:{dedup_key}"
            # 内存去重
            with self._lock:
                last = self._dedup_cache.get(dedup_key)
                if last and (time.time() - last) < dedup_minutes * 60:
                    logger.debug("邮件去重跳过: %s", subject)
                    return False
                self._dedup_cache[dedup_key] = time.time()
            # 持久化去重(重启不重复)
            if self._sent_before(dedup_key, dedup_minutes):
                logger.debug("邮件持久化去重跳过: %s", subject)
                return False
        # 异步发送
        self._enqueue(subject, html_body, extra_receivers, dedup_key)
        return True

    def _sent_before(self, dedup_key: str, minutes: int) -> bool:
        try:
            with get_session() as s:
                return s.query(NotificationRecord).filter(
                    NotificationRecord.dedup_key == dedup_key,
                    NotificationRecord.created_at >=
                    datetime.now() - __import__("datetime").timedelta(minutes=minutes),
                ).first() is not None
        except Exception:
            return False

    def _record_sent(self, dedup_key: str, ok: bool):
        try:
            with get_session() as s:
                s.add(NotificationRecord(dedup_key=dedup_key or "", status="SENT" if ok else "FAILED"))
        except Exception:
            pass

    def _enqueue(self, subject, html, extra, dedup_key):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._worker_send(subject, html, extra, dedup_key))
            else:
                asyncio.run(self._worker_send(subject, html, extra, dedup_key))
        except Exception as exc:
            logger.warning("邮件入队失败, 转为同步发送: %s", exc)
            ok = self._send_sync(subject, html, extra)
            if dedup_key:
                self._record_sent(dedup_key, ok)

    async def _worker_send(self, subject, html, extra, dedup_key):
        ok = await asyncio.to_thread(self._send_sync, subject, html, extra)
        if dedup_key:
            self._record_sent(dedup_key, ok)


_sender: Optional[EmailSender] = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = EmailSender()
    return _sender
