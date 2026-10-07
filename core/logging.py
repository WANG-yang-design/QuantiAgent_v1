# -*- coding: utf-8 -*-
"""
统一日志系统
============
- 控制台: 带颜色、简洁
- 文件: 按天 + 单文件大小双条件滚动, 滚动后 gzip 压缩, 超期自动清理
- 分模块子目录 (system/data/agent/risk/order/audit)
- 全链路 trace_id 贯穿: 一次 Agent 工作流共用一个 trace_id
- 第三方库降噪: httpx/uvicorn.access/apscheduler 等不再刷屏(修复: 曾一天 45MB)
- 未捕获异常(主线程/子线程)统一写入 error.log
- 供 Web 使用的日志查看/清理辅助函数
"""
import contextvars
import gzip
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.config import ROOT_DIR, ensure_dirs

# 模块 -> 日志子目录
_MODULE_DIR = {
    "data": "data",      # 数据采集/质量
    "agent": "agent",    # Agent 调用
    "risk": "risk",      # 风控
    "order": "order",    # 订单执行
    "audit": "audit",    # 审计(独立保留)
}

_FORMAT = "%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s: %(message)s"

# 日志文件默认参数(可按需调整)
MAX_BYTES = 20 * 1024 * 1024          # 单文件 20MB 触发滚动
SYSTEM_RETENTION_DAYS = 30            # system/模块日志保留天数
ERROR_RETENTION_DAYS = 90             # error 日志保留天数
_COMPRESS_MIN_BYTES = 1024 * 1024     # 滚动文件超过 1MB 才压缩(小文件压缩收益低)

# 噪音库: 只保留 WARNING 及以上(修复: 原 INFO 级 HTTP 请求日志刷爆磁盘)
_NOISY_LOGGERS = (
    "httpx", "httpcore", "urllib3", "requests", "akshare", "baostock",
    "matplotlib", "PIL", "apscheduler", "uvicorn.access", "watchfiles",
    "asyncio", "multipart", "charset_normalizer", "openai",
)

# 修复: 原 threading.local 在 asyncio 并发任务间互踩(run_pool_scan 3-5路并发
# 共享同一线程, set_trace_id 相互覆盖, 审计日志 trace_id 张冠李戴)。
# contextvars 在每个任务/协程有独立上下文, 并发的 Agent 调用不再互相污染。
_trace_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None)


class TraceFilter(logging.Filter):
    """把当前上下文的 trace_id 注入日志记录。"""

    def filter(self, record):
        record.trace_id = (get_trace_id() or "-")[:16]
        return True


_trace = TraceFilter()


def set_trace_id(tid: str):
    """设置当前上下文的 trace_id (工作流入口调用, 子线程需自行调用)。"""
    _trace_var.set(tid)


def get_trace_id() -> Optional[str]:
    return _trace_var.get()


def _attach_trace(handler: logging.Handler):
    """给 handler 附加 trace_id 过滤器(必须加到 handler 上, 加 logger 无效)。"""
    if not any(isinstance(f, TraceFilter) for f in handler.filters):
        handler.addFilter(_trace)


# ----------------------------------------------------------------------
# 滚动 handler: 按天 + 大小双条件, 滚动后压缩, 超期清理
# ----------------------------------------------------------------------
class _CompressedRotatingHandler(logging.handlers.TimedRotatingFileHandler):
    """TimedRotatingFileHandler 增强版。

    1. 除按天滚动外, 单文件超过 maxBytes 也滚动(同一天可滚动多次, 文件名加序号);
    2. 滚动后的文件 gzip 压缩(超过阈值的);
    3. 按保留天数清理旧文件(含压缩文件)。
    """

    def __init__(self, filename, when="midnight", backupCount=30,
                 encoding="utf-8", errors="replace",
                 maxBytes: int = MAX_BYTES, retention_days: int = SYSTEM_RETENTION_DAYS):
        super().__init__(filename, when=when, backupCount=backupCount,
                         encoding=encoding, delay=True, errors=errors)
        self.maxBytes = int(maxBytes or 0)
        self.retention_days = int(retention_days or 0)

    # ------------------------------------------------------------------
    def shouldRollover(self, record) -> bool:
        if super().shouldRollover(record):
            return True
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        try:
            if self.stream.tell() + len(self.format(record)) + 1 >= self.maxBytes:
                return True
        except Exception:
            pass
        return False

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        t = int(self.rolloverAt - self.interval)
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, time.localtime(t)))
        # 同一天多次滚动: 追加序号, 不覆盖上一个文件
        if os.path.exists(dfn):
            idx = 1
            while os.path.exists(f"{dfn}.{idx}"):
                idx += 1
            dfn = f"{dfn}.{idx}"
        try:
            self.rotate(self.baseFilename, dfn)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._compress(dfn)
        self._purge_old()
        if not self.delay:
            self.stream = self._open()
        new_rollover_at = self.computeRollover(t)
        while new_rollover_at <= time.time():
            new_rollover_at += self.interval
        self.rolloverAt = new_rollover_at

    # ------------------------------------------------------------------
    def _compress(self, path: str):
        try:
            if not os.path.exists(path) or os.path.getsize(path) < _COMPRESS_MIN_BYTES:
                return
            gz_path = path + ".gz"
            with open(path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            os.remove(path)
        except Exception:
            pass

    def _purge_old(self):
        if self.retention_days <= 0:
            return
        try:
            cutoff = time.time() - self.retention_days * 86400
            base = Path(self.baseFilename)
            prefix = base.name + "."
            for f in base.parent.glob(base.name + ".*"):
                try:
                    if f.name.startswith(prefix) and f.stat().st_mtime < cutoff:
                        f.unlink()
                except Exception:
                    continue
        except Exception:
            pass


def purge_all_logs(retention_days: Optional[int] = None) -> Dict[str, int]:
    """清理 logs/ 下超期滚动日志(不含当前活动日志)。

    返回 {deleted_files, deleted_bytes}。
    """
    deleted, freed = 0, 0
    now = time.time()
    for path in (ROOT_DIR / "logs").rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if ".log." not in name:
            continue
        if path.suffix == ".jsonl":
            continue
        days = retention_days if retention_days is not None else (
            ERROR_RETENTION_DAYS if name.startswith("error") else SYSTEM_RETENTION_DAYS)
        try:
            if now - path.stat().st_mtime > days * 86400:
                size = path.stat().st_size
                path.unlink()
                deleted += 1
                freed += size
        except Exception:
            continue
    return {"deleted_files": deleted, "deleted_bytes": freed}


def list_log_files() -> List[Dict[str, object]]:
    """列出可查看的日志文件(相对 logs/ 的路径)。"""
    out: List[Dict[str, object]] = []
    root = ROOT_DIR / "logs"
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("."):
            continue
        try:
            stat = path.stat()
        except Exception:
            continue
        out.append({
            "name": rel,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    out.sort(key=lambda x: str(x["modified"]), reverse=True)
    return out


def read_log_tail(name: str, lines: int = 200) -> List[str]:
    """读取日志文件末尾 N 行(仅允许 logs/ 内的文件, 防路径穿越)。"""
    root = (ROOT_DIR / "logs").resolve()
    target = (root / name).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(name)
    if target.suffix == ".gz":
        with gzip.open(target, "rt", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return [ln.rstrip("\n") for ln in content[-lines:]]
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.readlines()
    return [ln.rstrip("\n") for ln in content[-lines:]]


# ----------------------------------------------------------------------
# 初始化
# ----------------------------------------------------------------------
_session_logged = False
_session_lock = threading.Lock()


def _install_excepthooks():
    """未捕获异常统一落 error.log(修复: 子线程异常默认只打印 stderr)。"""
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("uncaught").critical(
            "未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _hook

    def _thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logging.getLogger("uncaught").critical(
            "子线程未捕获异常 (%s)", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _thread_hook


def setup_logging(level: str = "", log_dir: Optional[str] = None):
    """初始化根日志器: 控制台 + 文件(按天/按大小滚动)。

    level 为空时读取环境变量 LOG_LEVEL(默认 INFO)。
    """
    global _session_logged
    ensure_dirs()
    log_root = Path(log_dir) if log_dir else (ROOT_DIR / "logs")
    log_root.mkdir(parents=True, exist_ok=True)

    if not level:
        level = os.getenv("LOG_LEVEL", "INFO") or "INFO"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _attach_trace(console)
    root.addHandler(console)

    # 主文件 (按天滚动 + 20MB 上限)
    main = _CompressedRotatingHandler(
        log_root / "system.log", when="midnight", backupCount=SYSTEM_RETENTION_DAYS,
        maxBytes=MAX_BYTES, retention_days=SYSTEM_RETENTION_DAYS)
    main.setFormatter(logging.Formatter(_FORMAT))
    _attach_trace(main)
    root.addHandler(main)

    # 错误文件 (保留更久)
    err = _CompressedRotatingHandler(
        log_root / "error.log", when="midnight", backupCount=ERROR_RETENTION_DAYS,
        maxBytes=MAX_BYTES, retention_days=ERROR_RETENTION_DAYS)
    err.setLevel(logging.ERROR)
    err.setFormatter(logging.Formatter(_FORMAT))
    _attach_trace(err)
    root.addHandler(err)

    # 分模块文件: data/agent/risk/order/audit 各自一个日志文件。
    # 修复: 原实现只有显式调用 core.logging.get_logger() 的模块才落子目录文件,
    # 而各业务模块普遍使用标准 logging.getLogger("data.hub") 等,
    # 子目录日志文件实际从未生成("分模块日志"名不副实)。这里给每个
    # 顶层前缀挂 handler, 任何 data.*/agent.*/risk.*/order.*/audit.* 日志
    # 都会写入对应子目录文件(同时仍写入 system.log)。
    for prefix, sub in _MODULE_DIR.items():
        d = log_root / sub
        d.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger(prefix)
        for h in list(lg.handlers):
            if isinstance(h, _CompressedRotatingHandler):
                lg.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        h = _CompressedRotatingHandler(
            d / f"{sub}.log", when="midnight", backupCount=SYSTEM_RETENTION_DAYS,
            maxBytes=MAX_BYTES, retention_days=SYSTEM_RETENTION_DAYS)
        h.setFormatter(logging.Formatter(_FORMAT))
        _attach_trace(h)
        lg.addHandler(h)
        lg.propagate = True

    # 第三方库降噪
    for name in _NOISY_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = True

    logging.captureWarnings(True)

    with _session_lock:
        if not _session_logged:
            _session_logged = True
            _install_excepthooks()
            logging.getLogger("system").info(
                "=" * 64)
            logging.getLogger("system").info(
                "进程启动 pid=%s python=%s cwd=%s level=%s",
                os.getpid(), sys.version.split()[0], os.getcwd(), level.upper())


def get_logger(name: str) -> logging.Logger:
    """获取带子文件输出的 logger:
       get_logger("data.collector") → logs/data/collector.log
       get_logger("agent.technical") → logs/agent/technical.log
    """
    logger = logging.getLogger(name)
    first = name.split(".")[0]
    if first in _MODULE_DIR:
        sub = _MODULE_DIR[first]
        d = ROOT_DIR / "logs" / sub
        d.mkdir(parents=True, exist_ok=True)
        target = d / (name.split(".")[-1] + ".log")
        # 避免重复添加(同一 logger 多次 get_logger)
        if not any(isinstance(x, logging.FileHandler) and
                   getattr(x, "baseFilename", "") == str(target.resolve())
                   for x in logger.handlers):
            h = _CompressedRotatingHandler(
                target, when="midnight", backupCount=SYSTEM_RETENTION_DAYS,
                maxBytes=MAX_BYTES, retention_days=SYSTEM_RETENTION_DAYS)
            h.setFormatter(logging.Formatter(_FORMAT))
            _attach_trace(h)
            logger.addHandler(h)
    return logger


def audit_event(event_type: str, actor: str, payload: dict):
    """审计日志(写入日志 + 由 memory.audit_log 落库)。"""
    from memory.audit_log import AuditLogger
    AuditLogger.instance().log(event_type, actor, payload)


def now_str() -> str:
    from core.timeutil import now
    return now().strftime("%Y-%m-%d %H:%M:%S")
