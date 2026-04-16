"""
虚拟墙钟：事件回放时把 time.time() 映射到 K 线事件时间，
使 BetaNeutralHF / paper_engine 等依赖 time.time() 的冷却与持仓时钟与历史对齐。

用法：
  from src.core import event_replay_time as ert
  ert.activate()
  try:
      for ts in ...:
          ert.set_virtual(ts)
          await engine.process_ws_tick(...)
  finally:
      ert.deactivate()
"""

from __future__ import annotations

import time as _real_time
from typing import Optional

_virtual_ts: Optional[float] = None
_original_time = _real_time.time


def set_virtual(ts: float) -> None:
    global _virtual_ts
    _virtual_ts = float(ts)


def clear_virtual() -> None:
    global _virtual_ts
    _virtual_ts = None


def _patched_time() -> float:
    if _virtual_ts is not None:
        return float(_virtual_ts)
    return float(_original_time())


def activate() -> None:
    """将全局 time.time 替换为虚拟时钟（可嵌套调用方须对称 deactivate）。"""
    import time as _mod

    if getattr(_mod, "_shark_replay_saved_time", None) is None:
        _mod._shark_replay_saved_time = _mod.time
    _mod.time = _patched_time  # type: ignore[assignment]


def deactivate() -> None:
    import time as _mod

    saved = getattr(_mod, "_shark_replay_saved_time", None)
    if saved is not None:
        _mod.time = saved  # type: ignore[assignment]
        delattr(_mod, "_shark_replay_saved_time")
    clear_virtual()
