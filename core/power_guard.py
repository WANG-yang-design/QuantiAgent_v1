# -*- coding: utf-8 -*-
"""
电源守卫 (Windows 防休眠)
========================
问题: 电脑"睡眠"会挂起所有进程(调度器/Web 全停), 用户反馈"息屏后就不工作了"。
真相: 显示器关闭(息屏)不影响运行; 但 Windows 自动进入"睡眠"会把整个系统冻结。

本模块在 Web/调度器启动时调用 SetThreadExecutionState, 告诉 Windows
"本程序在运行, 禁止自动睡眠"(只阻止自动睡眠, 不阻止手动睡眠/关机/息屏)。
"""
import logging
import threading
import time

logger = logging.getLogger("core.power")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040

_guard_thread = None


def _win32_set(flags: int) -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def prevent_sleep():
    """阻止系统自动睡眠(持续生效; 手动睡眠/关机/息屏不受影响)。"""
    global _guard_thread
    if not _win32_set(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED):
        # 非 Windows: 无需处理
        return
    logger.info("已启用防自动睡眠(SetThreadExecutionState): 系统不会自动休眠")

    # 部分系统/服务场景下该标志需要周期性重申, 后台线程每60秒刷一次
    def _keep():
        while True:
            time.sleep(60)
            try:
                _win32_set(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
            except Exception:
                pass

    if _guard_thread is None or not _guard_thread.is_alive():
        _guard_thread = threading.Thread(target=_keep, daemon=True,
                                         name="power-guard")
        _guard_thread.start()


def allow_sleep():
    """撤销防睡眠(程序退出前调用; 一般不需要, 进程退出自动清除)。"""
    _win32_set(_ES_CONTINUOUS)
