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
import logging
import queue
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta
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
        # 修复: 原实现用 asyncio.Queue + create_task —— 非运行中事件循环里
        # create_task 的任务永不执行, 邮件静默丢失。改为独立发送线程 +
        # 线程安全队列, 不依赖任何事件循环。
        self._queue: "queue.Queue" = queue.Queue(maxsize=200)
        self._worker_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._dedup_cache: Dict[str, float] = {}
        if self.enabled:
            self._start_worker()

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
        """发送入口: 入队发送(去重检查在发送线程内完成)。
        修复: 原实现发送前就写入内存去重缓存, 发送失败(网络/SMTP异常)
        也会被当成"已发送" —— 之后重试/重新触发全部被去重吞掉,
        用户永远收不到邮件。去重移到发送成功后记录。"""
        if dedup_key:
            dedup_key = f"{self.cfg.get('sender','')}:{dedup_key}"
        self._enqueue(subject, html_body, extra_receivers, dedup_key)
        return True

    def _sent_before(self, dedup_key: str, minutes: int) -> bool:
        try:
            with get_session() as s:
                # 修复: 只按"成功发送"去重 —— FAILED 记录不得阻塞重发
                return s.query(NotificationRecord).filter(
                    NotificationRecord.dedup_key == dedup_key,
                    NotificationRecord.status == "SENT",
                    NotificationRecord.created_at >=
                    datetime.now() - timedelta(minutes=minutes),
                ).first() is not None
        except Exception:
            return False

    def _record_sent(self, dedup_key: str, ok: bool):
        # 修复: 仅发送成功才落库(失败不占去重名额)
        if not ok:
            return
        try:
            with get_session() as s:
                s.add(NotificationRecord(dedup_key=dedup_key or "",
                                         status="SENT"))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 后台发送线程(不阻塞主流程, 也不依赖事件循环)
    # ------------------------------------------------------------------
    def _start_worker(self):
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="email-sender")
        self._worker_thread.start()

    def _worker_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            subject, html, extra, dedup_key = item
            # 去重在发送线程内完成(发送成功才记录, 失败可重发)
            if dedup_key:
                with self._lock:
                    last = self._dedup_cache.get(dedup_key)
                    if last and (time.time() - last) < 1440 * 60:
                        logger.debug("邮件去重跳过: %s", subject)
                        continue
                if self._sent_before(dedup_key, 1440):
                    logger.debug("邮件持久化去重跳过: %s", subject)
                    continue
            try:
                ok = self._send_sync(subject, html, extra)
            except Exception as exc:
                logger.error("邮件发送线程异常 %s: %s", subject, exc)
                ok = False
            if ok and dedup_key:
                with self._lock:
                    self._dedup_cache[dedup_key] = time.time()
                self._record_sent(dedup_key, ok)

    def _enqueue(self, subject, html, extra, dedup_key):
        try:
            self._queue.put((subject, html, extra, dedup_key), timeout=1.0)
        except Exception as exc:
            # 队列满/线程异常: 降级为同步发送, 保证通知不丢
            logger.warning("邮件入队失败, 转为同步发送: %s", exc)
            try:
                ok = self._send_sync(subject, html, extra)
            except Exception:
                ok = False
            if dedup_key:
                self._record_sent(dedup_key, ok)


_sender: Optional[EmailSender] = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = EmailSender()
    return _sender
