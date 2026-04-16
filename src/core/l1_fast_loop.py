"""
L1 极速执行层：纯内存 + WS，无 HTTP（发单走 REST 在引擎外）。
滚动 CVD、1m 价格条估算 ATR、ZMQ 可覆写运行时阈值。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.core.config_manager import config_manager
from src.utils.logger import log

# L2 经 ZMQ 写入；主进程读
_runtime: Dict[str, Any] = {
    "halt_trading": False,
    "cvd_burst_mult": None,
    "cvd_stop_mult": None,
    "min_atr_bps": None,
    "position_scale": 1.0,
}


def apply_l1_tuning(data: Dict[str, Any]) -> None:
    """应用经 IPC 下发的 L1 调参 dict；可选键 halt_trading, cvd_burst_mult, cvd_stop_mult, min_atr_bps, position_scale"""
    for k in ("halt_trading", "cvd_burst_mult", "cvd_stop_mult", "min_atr_bps", "position_scale"):
        if k in data:
            _runtime[k] = data[k]
    log.info(f"[L1] Tuning applied: {_runtime}")


def _eff(key: str, default: float) -> float:
    v = _runtime.get(key)
    if v is None:
        return float(default)
    return float(v)


def _eff_bool_halt() -> bool:
    return bool(_runtime.get("halt_trading", False))


def _eff_position_scale() -> float:
    v = _runtime.get("position_scale")
    if v is None:
        return 1.0
    return max(float(v), 0.0)


def _trade_price(tr: Dict[str, Any]) -> float:
    for k in ("price", "fill_price", "deal_price"):
        v = tr.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _price_bucket_key(price: float) -> str:
    """稳定字典键；极小价多保留小数。"""
    if price <= 0:
        return ""
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.0001:
        return f"{price:.6f}"
    return f"{price:.8f}"


class _SymState:
    __slots__ = (
        "trades",
        "vwap_pts",
        "bar_bucket",
        "bar_h",
        "bar_l",
        "bar_o",
        "bar_c",
        "bars",
        "fp_bars",
    )

    def __init__(self) -> None:
        self.trades: Deque[Tuple[float, float]] = deque()
        # (ts, price, abs_qty) 用于滚动成交 VWAP；与 trades 同步时间窗裁剪
        self.vwap_pts: Deque[Tuple[float, float, float]] = deque(maxlen=16384)
        self.bar_bucket: Optional[int] = None
        self.bar_h = 0.0
        self.bar_l = 0.0
        self.bar_o = 0.0
        self.bar_c = 0.0
        self.bars: Deque[Tuple[float, float, float, float, float]] = deque(maxlen=32)
        # 足迹：每分钟一根，dict[价键] -> [主买量, 主卖量]
        self.fp_bars: Deque[Tuple[int, Dict[str, List[float]]]] = deque(maxlen=120)

    def prune_trades(self, now: float, max_age: float = 65.0) -> None:
        while self.trades and now - self.trades[0][0] > max_age:
            self.trades.popleft()
        while self.vwap_pts and now - self.vwap_pts[0][0] > max_age:
            self.vwap_pts.popleft()

    def add_vwap_point(self, ts: float, px: float, vol: float) -> None:
        if px <= 0 or vol <= 0:
            return
        self.vwap_pts.append((ts, float(px), float(vol)))

    def add_trade(self, ts: float, signed_qty: float) -> None:
        self.trades.append((ts, signed_qty))
        self.prune_trades(ts)

    def cvd_net(self, window: float, now: float) -> float:
        self.prune_trades(now)
        return sum(q for t, q in self.trades if now - t <= window)

    def on_price(self, ts: float, px: float) -> None:
        if px <= 0:
            return
        bucket = int(ts // 60)
        if self.bar_bucket is None or bucket != self.bar_bucket:
            if self.bar_bucket is not None and self.bar_c > 0:
                self.bars.append(
                    (float(self.bar_bucket * 60), self.bar_o, self.bar_h, self.bar_l, self.bar_c)
                )
            self.bar_bucket = bucket
            self.bar_o = px
            self.bar_h = px
            self.bar_l = px
            self.bar_c = px
        else:
            self.bar_h = max(self.bar_h, px)
            self.bar_l = min(self.bar_l, px)
            self.bar_c = px

    def add_footprint(self, ts: float, price: float, signed_qty: float) -> None:
        if price <= 0 or abs(signed_qty) <= 0:
            return
        bucket = int(ts // 60) * 60
        pk = _price_bucket_key(price)
        if not pk:
            return
        if not self.fp_bars or self.fp_bars[-1][0] != bucket:
            self.fp_bars.append((bucket, {}))
        _, lev = self.fp_bars[-1]
        row = lev.get(pk)
        if row is None:
            row = [0.0, 0.0]
            lev[pk] = row
        if signed_qty >= 0:
            row[0] += signed_qty
        else:
            row[1] += abs(signed_qty)


_states: Dict[str, _SymState] = {}

# WS footprint 增量：按 symbol 节流；换分钟桶时必发，避免漏 bar_closed。
_FP_WS_THROTTLE_SEC = 0.22
_fp_ws_last_tail_t: Dict[str, int] = {}
_fp_ws_last_push_mono: Dict[str, float] = {}


def _st(sym: str) -> _SymState:
    if sym not in _states:
        _states[sym] = _SymState()
    return _states[sym]


def parse_gate_trade(tr: Dict[str, Any]) -> Tuple[float, float]:
    """返回 (ts秒, 有符号成交量 买+卖-)"""
    ts = tr.get("create_time") or tr.get("time") or 0
    try:
        tsf = float(ts)
    except (TypeError, ValueError):
        tsf = 0.0
    if tsf > 1e12:
        tsf /= 1000.0
    if tsf <= 0:
        tsf = time.time()

    raw = tr.get("size", tr.get("amount", 0))
    try:
        sz = float(raw)
    except (TypeError, ValueError):
        sz = 0.0

    side = str(tr.get("side", "") or tr.get("type", "")).lower()
    if side in ("sell", "ask", "short"):
        return tsf, -abs(sz)
    if side in ("buy", "bid", "long"):
        return tsf, abs(sz)
    return tsf, sz


def ingest_trades(symbol: str, trades: List[Dict[str, Any]]) -> None:
    st = _st(symbol)
    for tr in trades or []:
        if not isinstance(tr, dict):
            continue
        ts, dq = parse_gate_trade(tr)
        if abs(dq) > 0:
            st.add_trade(ts, dq)
            px = _trade_price(tr)
            if px > 0:
                st.add_vwap_point(ts, px, abs(dq))
                st.add_footprint(ts, px, dq)


def on_ticker_price(symbol: str, ts: float, last: float) -> None:
    _st(symbol).on_price(ts, last)
    try:
        from src.core.risk_engine import risk_engine as _re

        _re.record_ticker_for_10m_volatility(symbol, ts, last)
    except Exception:
        pass


def rolling_trade_vwap(symbol: str, window: float = 60.0, now: Optional[float] = None) -> Optional[float]:
    """过去 window 秒内 Σ(p·|q|)/Σ|q|；无成交则 None。"""
    now = now or time.time()
    st = _st(symbol)
    st.prune_trades(now)
    num = 0.0
    den = 0.0
    for ts, px, vol in st.vwap_pts:
        if now - ts <= window:
            num += px * vol
            den += vol
    if den <= 1e-18:
        return None
    return num / den


def trade_flow_gross_contracts(symbol: str, window: float, now: Optional[float] = None) -> float:
    """窗口内主动买+卖合约张数（绝对值之和），用于判断成交流是否足够可信。"""
    now = now or time.time()
    st = _st(symbol)
    st.prune_trades(now)
    return float(sum(abs(q) for t, q in st.trades if now - t <= window))


def trade_flow_imbalance(symbol: str, window: float = 10.0, now: Optional[float] = None) -> float:
    """
    成交流失衡度（VPIN/CVD 的归一化形态）：(V_buy − V_sell) / (V_buy + V_sell)，∈[-1,1]。
    与盘口 OBI 互补：反映真实成交倾斜，减轻假挂单 (Spoofing) 干扰。
    """
    now = now or time.time()
    st = _st(symbol)
    st.prune_trades(now)
    vb = 0.0
    vs = 0.0
    for t, q in st.trades:
        if now - t > window:
            continue
        if q > 0:
            vb += q
        else:
            vs += abs(q)
    tot = vb + vs
    if tot <= 1e-18:
        return 0.0
    return float(vb - vs) / float(tot)


def taker_sell_volume(symbol: str, window: float, now: Optional[float] = None) -> float:
    now = now or time.time()
    st = _st(symbol)
    st.prune_trades(now)
    return sum(abs(q) for t, q in st.trades if now - t <= window and q < 0)


def taker_buy_volume(symbol: str, window: float, now: Optional[float] = None) -> float:
    now = now or time.time()
    st = _st(symbol)
    st.prune_trades(now)
    return sum(q for t, q in st.trades if now - t <= window and q > 0)


def taker_sell_exhausted(
    symbol: str,
    burst_sec: float,
    baseline_sec: float,
    ratio: float,
    min_baseline_vol: float,
    now: Optional[float] = None,
) -> bool:
    """近 burst_sec 内主动卖量是否低于 baseline 均速 × ratio。"""
    now = now or time.time()
    v_burst = taker_sell_volume(symbol, burst_sec, now)
    v_base = taker_sell_volume(symbol, baseline_sec, now)
    if v_base < min_baseline_vol:
        return False
    expected = (v_base / baseline_sec) * burst_sec
    return expected > 1e-12 and v_burst < ratio * expected


def taker_buy_exhausted(
    symbol: str,
    burst_sec: float,
    baseline_sec: float,
    ratio: float,
    min_baseline_vol: float,
    now: Optional[float] = None,
) -> bool:
    now = now or time.time()
    v_burst = taker_buy_volume(symbol, burst_sec, now)
    v_base = taker_buy_volume(symbol, baseline_sec, now)
    if v_base < min_baseline_vol:
        return False
    expected = (v_base / baseline_sec) * burst_sec
    return expected > 1e-12 and v_burst < ratio * expected


def ohlc_1m_closes(symbol: str, max_bars: int = 32) -> List[float]:
    """Completed 1m bar closes (oldest → newest), capped by internal deque length."""
    st = _st(symbol)
    bars = list(st.bars)
    if max_bars > 0:
        bars = bars[-max_bars:]
    return [float(c) for (_t, _o, _h, _l, c) in bars if c and c > 0]


def atr_1m_bps(symbol: str) -> float:
    """最近若干根已完成 1m 棒的平均 (H-L)/C 用 bps 表示。"""
    st = _st(symbol)
    bars = list(st.bars)
    if len(bars) < 3:
        return 0.0
    s = 0.0
    n = 0
    for _t, o, h, l, c in bars[-14:]:
        if c <= 0:
            continue
        s += (h - l) / c
        n += 1
    if n == 0:
        return 0.0
    return (s / n) * 10000.0


def cvd_metrics(symbol: str, now: Optional[float] = None) -> Tuple[float, float, float]:
    now = now or time.time()
    st = _st(symbol)
    c10 = st.cvd_net(10.0, now)
    c60 = st.cvd_net(60.0, now)
    baseline = max(abs(c60) / 6.0, 1e-8)
    return c10, c60, baseline


def _cvd_block(symbol: str, now: float) -> Dict[str, Any]:
    c10, c60, baseline = cvd_metrics(symbol, now)
    return {
        "window_sec_10": 10,
        "net_10s": round(c10, 8),
        "window_sec_60": 60,
        "net_60s": round(c60, 8),
        "baseline_10s": round(baseline, 8),
    }


def _export_levels_for_bar(
    lev: Dict[str, List[float]], max_levels_per_bar: int
) -> List[Dict[str, float]]:
    rows: List[Tuple[str, float, float, float, float]] = []
    for pk, (bv, sv) in lev.items():
        try:
            pxf = float(pk)
        except (TypeError, ValueError):
            continue
        tot = bv + sv
        if tot <= 0:
            continue
        rows.append((pk, pxf, float(bv), float(sv), tot))
    rows.sort(key=lambda x: x[1], reverse=True)
    if len(rows) > max_levels_per_bar:
        rows.sort(key=lambda x: -x[4])
        rows = rows[:max_levels_per_bar]
        rows.sort(key=lambda x: x[1], reverse=True)
    levels: List[Dict[str, float]] = []
    for _pk, pxf, bv, sv, _tot in rows:
        levels.append(
            {
                "price": round(pxf, 10),
                "buy": round(bv, 8),
                "sell": round(sv, 8),
                "delta": round(bv - sv, 8),
            }
        )
    return levels


def obi_opposes_long(obi: float, max_opp: float) -> bool:
    """OBI 过低视为卖盘墙阻挡做多。"""
    return obi < max_opp


def obi_opposes_short(obi: float, min_opp: float) -> bool:
    return obi > min_opp


def footprint_snapshot(
    symbol: str,
    *,
    max_bars: int = 90,
    max_levels_per_bar: int = 48,
) -> Dict[str, Any]:
    """
    shark.footprint.v1 — 供 REST/WS：分钟足迹柱 + CVD。
    levels 按价格从高到低；每柱按成交总量截断 top N 档位。
    """
    now = time.time()
    st = _st(symbol)
    raw_bars = list(st.fp_bars)[-max(1, max_bars) :]
    out_bars: List[Dict[str, Any]] = []
    for t0, lev in raw_bars:
        out_bars.append({"t": int(t0), "levels": _export_levels_for_bar(lev, max_levels_per_bar)})

    return {
        "schema": "shark.footprint.v1",
        "symbol": symbol,
        "generated_at": now,
        "cvd": _cvd_block(symbol, now),
        "bars": out_bars,
    }


def footprint_ws_delta_maybe(
    symbol: str,
    *,
    max_levels_per_bar: int = 48,
) -> Optional[Dict[str, Any]]:
    """
    shark.footprint.delta.v1 — WS 增量：当前分钟 tail + 换桶时上一根 bar_closed + 全量 cvd 块。
    同桶内约 220ms 节流；新桶立即推送。状态仅在返回非 None 时前进，避免漏 bar_closed。
    """
    st = _st(symbol)
    if not st.fp_bars:
        return None
    now = time.time()
    mono = time.monotonic()
    tail_t, tail_lev = st.fp_bars[-1]
    prev_t = _fp_ws_last_tail_t.get(symbol)
    first = prev_t is None
    bucket_rolled = not first and int(prev_t) != int(tail_t)

    if not first and not bucket_rolled:
        if mono - _fp_ws_last_push_mono.get(symbol, 0.0) < _FP_WS_THROTTLE_SEC:
            return None

    bar_closed: Optional[Dict[str, Any]] = None
    if bucket_rolled and prev_t is not None:
        pt = int(prev_t)
        for bt, blev in st.fp_bars:
            if int(bt) == pt:
                bar_closed = {"t": pt, "levels": _export_levels_for_bar(blev, max_levels_per_bar)}
                break

    out: Dict[str, Any] = {
        "schema": "shark.footprint.delta.v1",
        "symbol": symbol,
        "seq": int(now * 1000),
        "generated_at": now,
        "cvd": _cvd_block(symbol, now),
        "tail": {"t": int(tail_t), "levels": _export_levels_for_bar(tail_lev, max_levels_per_bar)},
    }
    if bar_closed is not None:
        out["bar_closed"] = bar_closed

    _fp_ws_last_tail_t[symbol] = int(tail_t)
    _fp_ws_last_push_mono[symbol] = mono
    return out


def cvd_compact(symbol: str) -> Dict[str, float]:
    """WS 轻量字段。"""
    now = time.time()
    c10, c60, bl = cvd_metrics(symbol, now)
    return {
        "net_10s": round(c10, 6),
        "net_60s": round(c60, 6),
        "baseline_10s": round(bl, 6),
    }


def reset_buffers_for_replay() -> None:
    """清空 L1 滚动缓冲，供事件驱动回放冷启动。"""
    _states.clear()
    _fp_ws_last_tail_t.clear()
    _fp_ws_last_push_mono.clear()
