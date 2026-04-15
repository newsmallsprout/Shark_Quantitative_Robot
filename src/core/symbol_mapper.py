"""
币安 USDⓈ-M 永续符号 ↔ Gate.io USDT 永续（BASE/QUOTE）双向映射。

默认规则：PEPEUSDT → PEPE/USDT；Gate → 大写 BASE + /USDT。
土狗/别名通过 config 中 binance_leadlag.symbol_overrides 覆盖。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from src.core.config_manager import config_manager


def bn_usdm_to_gate(bn_symbol: str) -> str:
    """BN 合约名，如 DOGEUSDT → DOGE/USDT。"""
    s = str(bn_symbol or "").strip().upper()
    if not s.endswith("USDT"):
        return ""
    base = s[:-4]
    if not base:
        return ""
    ov = _overrides_map()
    if base in ov:
        g = ov[base]
        return g if "/" in g else f"{g}/USDT"
    return f"{base}/USDT"


def gate_to_bn_usdm(gate_symbol: str) -> str:
    """Gate BASE/USDT → BN 格式，如 DOGE/USDT → DOGEUSDT。"""
    g = str(gate_symbol or "").strip().upper()
    if "/" not in g:
        return ""
    base, quote = g.split("/", 1)
    if quote != "USDT" or not base:
        return ""
    rev = _reverse_overrides()
    if base in rev:
        return f"{rev[base]}USDT"
    return f"{base}USDT"


def _overrides_map() -> Dict[str, str]:
    try:
        cfg = config_manager.get_config().binance_leadlag
        raw = getattr(cfg, "symbol_overrides", None) or {}
        if isinstance(raw, dict):
            return {str(k).strip().upper(): str(v).strip() for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _reverse_overrides() -> Dict[str, str]:
    """Gate BASE（无斜杠）→ BN base 片段。"""
    out: Dict[str, str] = {}
    for bn_base, gate_spec in _overrides_map().items():
        gs = gate_spec.strip().upper()
        if "/" in gs:
            gb = gs.split("/", 1)[0]
        else:
            gb = gs
        if gb:
            out[gb] = bn_base
    return out


def parse_bn_usdm_base_quote(bn_symbol: str) -> Tuple[str, str]:
    s = str(bn_symbol or "").strip().upper()
    if s.endswith("USDT"):
        return s[:-4], "USDT"
    return "", ""
