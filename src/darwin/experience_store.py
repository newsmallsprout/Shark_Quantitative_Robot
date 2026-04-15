"""
经验库：每笔开仓 / 平仓追加 JSONL，供 Darwin Researcher / L3 批进化注入上下文。
与 autopsy 单文件 JSON 并存：autopsy 求全量复盘，本库求时间序检索与 LLM 摘要。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from src.utils.logger import log

_LOCK = threading.Lock()


def _cfg_paths() -> tuple[str, int]:
    from src.core.config_manager import config_manager

    d = config_manager.get_config().darwin
    path = getattr(d, "experience_log_path", None) or "data/darwin/experience.jsonl"
    tail = max(1, int(getattr(d, "experience_tail_lines", 80) or 80))
    return path, tail


def _darwin_on() -> bool:
    try:
        from src.core.config_manager import config_manager

        return bool(config_manager.get_config().darwin.enabled)
    except Exception:
        return False


def append_experience_record(record: Dict[str, Any]) -> None:
    """线程安全追加一行 JSON（失败静默，不阻塞交易）。"""
    if not _darwin_on():
        return
    path, _ = _cfg_paths()
    row = dict(record)
    row.setdefault("ts", time.time())
    row.setdefault("schema", "darwin.experience.v1")
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        log.warning(f"[Darwin/Exp] append failed: {e}")


def append_from_autopsy(autopsy: Dict[str, Any]) -> None:
    """从 trade_autopsy.v2 压一条紧凑经验（平仓）。"""
    if not isinstance(autopsy, dict):
        return
    pnl = autopsy.get("pnl") or {}
    append_experience_record(
        {
            "kind": "close",
            "symbol": autopsy.get("symbol"),
            "side": autopsy.get("side"),
            "realized_net": pnl.get("realized_net"),
            "exit_reason": (autopsy.get("exit") or {}).get("reason"),
            "duration_sec": autopsy.get("duration_sec"),
            "trading_mode": (autopsy.get("exit") or {}).get("trading_mode"),
            "strategy": (autopsy.get("entry_snapshot") or {}).get("strategy"),
        }
    )


def append_order_open_event(
    *,
    symbol: str,
    side: str,
    price: float,
    contracts: float,
    leverage: int,
    margin_mode: str,
    entry_context: Optional[Dict[str, Any]] = None,
) -> None:
    """新开仓一笔经验（开仓）。"""
    try:
        from src.core.config_manager import config_manager

        if not config_manager.get_config().darwin.enabled:
            return
        if not getattr(config_manager.get_config().darwin, "learn_on_order_open", True):
            return
    except Exception:
        return

    ect = dict(entry_context or {})
    slim = {
        "strategy": ect.get("strategy"),
        "trading_mode": ect.get("trading_mode"),
        "predator_matrix": bool(ect.get("predator_matrix")),
        "berserker": bool(ect.get("berserker")),
    }
    append_experience_record(
        {
            "kind": "open",
            "symbol": symbol,
            "side": side,
            "price": float(price),
            "contracts": float(contracts),
            "leverage": int(leverage),
            "margin_mode": margin_mode,
            "entry_hint": slim,
        }
    )


def tail_experience_text(max_lines: Optional[int] = None) -> str:
    """最近 N 行原文（供 LLM）；无文件或失败返回空串。"""
    if not _darwin_on():
        return ""
    path, default_n = _cfg_paths()
    n = int(max_lines) if max_lines is not None else default_n
    try:
        if not os.path.isfile(path):
            return ""
        with _LOCK:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        tail = lines[-n:] if len(lines) > n else lines
        return "".join(tail).strip()
    except OSError as e:
        log.warning(f"[Darwin/Exp] tail read failed: {e}")
        return ""


def tail_experience_parsed(max_lines: Optional[int] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in tail_experience_text(max_lines).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
